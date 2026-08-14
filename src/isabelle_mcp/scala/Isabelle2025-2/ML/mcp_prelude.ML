(*  Title:      Isabelle-MCP/scala/ML/mcp_prelude.ML

    ML injected into the prover by "isabelle mcp_server" via ML_Process use_prelude,
    i.e. Poly/ML "--use" AFTER the session heap is loaded but BEFORE
    "Isabelle_Process.init ()" starts the protocol loop.

    Plain SML only: this is compiled by the raw Poly/ML compiler, so no Isabelle
    antiquotations and no cartouches.

    Poly/ML treats a failing "--use" as FATAL, so a broken prelude kills the prover
    outright rather than degrading.  Language_Server guards the file's existence and
    surfaces the compiler output; keep this file small and dependency-free.

    See docs/CANCELLATION.md for the full design and its validation.
*)

val mcp_prelude_version = "3";


(* Global cancellation of all document execution, using only the PUBLIC Isabelle/ML API,
   so that Isabelle-MCP needs no patched Isabelle distribution.

     * Execution.discontinue () clears the current execution id.  Command.exec registers an
       exec via "Execution.running execution_id exec_id groups", which fails once the ids no
       longer match -- so any exec that has not started yet never starts.  This is the barrier,
       and it runs FIRST, before anything that could raise.

     * Execution.cancel exec_id cancels every Future group registered for that exec.
       Execution.fork prepends the subgroup of each forked proof to that list, and Task_Queue
       registers every task under all of its ancestor groups, so cancelling reaches the running
       threads of forked proofs too.

   The exec ids are supplied by the Scala side from Document.State.execs, which spans all
   nodes.  An id ML does not know is a silent no-op: Execution.cancel -> peek -> exec_groups
   returns [] for an unknown id. *)

val _ =
  Protocol_Command.define "Isabelle_MCP.cancel_execution"
    (fn args =>
      let
        (* barrier first: this cannot fail, and it alone stops everything not yet started *)
        val _ = Execution.discontinue ();
        val exec_ids = maps (map Document_ID.parse o space_explode ",") args;
        val _ = List.app Execution.cancel exec_ids;
      in
        Output.system_message
          ("Isabelle_MCP.cancel_execution: discontinued, cancelled " ^
            string_of_int (length exec_ids) ^ " execs")
      end);


(* Startup probe.  A protocol command that ML does not know is NOT fatal -- the loop turns it
   into a system message and carries on -- so a missing prelude would otherwise degrade
   silently, reporting successful cancellations that never happened.  Language_Server issues
   this ping after startup and refuses to serve if no pong comes back. *)

val _ =
  Protocol_Command.define "Isabelle_MCP.ping"
    (fn _ =>
      Output.protocol_message
        [("function", "isabelle_mcp_pong")] [[XML.Text mcp_prelude_version]]);

(* Position-explicit queries: the proof state and find_theorems at one command, read straight
   out of the document state -- no caret move, no overlay, no document update at all.

     Isabelle_MCP.proof_state    id node_name command_id
     Isabelle_MCP.find_theorems  id node_name command_id limit allow_dups query
     Isabelle_MCP.cancel_query   id

   Every request answers with EXACTLY ONE protocol message carrying EXACTLY ONE chunk:

     properties  ("function", "isabelle_mcp_query_result") -- must be first, or
                 Protocol_Handlers.invoke does not dispatch (protocol_handlers.scala:44-49);
                 ("id", id); ("status", status); and, when status is "ok",
                 ("comment", "true") and/or ("forked", "true").
     chunk       the markup-wrapped result strings for "ok", the error text for "failed",
                 empty otherwise.  Prover.Protocol_Output.text throws on anything but a
                 single chunk (prover.scala:56-59).

   Statuses: ok, undefined, unfinished, interrupted, no_proof_state, no_context, failed,
   cancelled, crashed.  ML cannot map a line to a command, so nothing here mentions a
   position: Scala resolves the command id from its snapshot and renders the agent-facing
   sentence for each status. *)

val mcp_query_function = "isabelle_mcp_query_result";

(* Request table.  Removing a request's entry IS the permission to reply, so whoever removes
   it first -- the worker, or Isabelle_MCP.cancel_query -- replies and the other stays
   silent.  Exactly one reply per request, including when the fork is cancelled before it
   ever runs (Future.forks then never enters the job body at all). *)

val mcp_queries =
  Synchronized.var "Isabelle_MCP.queries" (Symtab.empty: Future.group Symtab.table);

fun mcp_take id =
  Synchronized.change_result mcp_queries
    (fn tab => (Symtab.lookup tab id, Symtab.delete_safe id tab));

fun mcp_reply id status flags body =
  Output.protocol_message
    ([("function", mcp_query_function), ("id", id), ("status", status)] @ flags) [body];

fun mcp_finish id status flags body =
  (case mcp_take id of
    NONE => ()
  | SOME _ => mcp_reply id status flags body);

fun mcp_failed id exn =
  (case Exn.capture_body (fn () => Runtime.exn_message exn) of
    Exn.Res msg => mcp_finish id "failed" [] (XML.blob [msg])
  | Exn.Exn _ => mcp_finish id "crashed" [] []);


(* Resolving a command to its final Toplevel.state.  Everything here runs on the protocol
   thread and is cheap; the failure modes are the classification of QUERY_TOOLS_UPGRADE.md
   section 5.3.  Document.command_exec RAISES for an unknown node or command id rather than
   returning NONE (document.ML:480-484), and any edit to a file re-creates every command id
   in it, so that is a routine path, not a corner. *)

datatype mcp_command =
    MCP_State of Toplevel.state * Properties.T   (*state, plus the notes to send with it*)
  | MCP_Bad of string * string;                  (*status, message*)

fun mcp_resolve state node_name command_id =
  (case Exn.capture_body (fn () => Document.command_exec state node_name command_id) of
    Exn.Exn _ => MCP_Bad ("undefined", "")
  | Exn.Res NONE => MCP_Bad ("undefined", "")
  | Exn.Res (SOME (eval, _)) =>
      if not (Command.eval_finished eval) then MCP_Bad ("unfinished", "")
      else
        (*eval_result_state forces a lazy value: "Unfinished lazy" while the transition is
          still running, "Interrupt" once a cancelled one has been assigned strictly
          (lazy.ML:72-77, :105-113)*)
        (case Exn.capture_body (fn () => Command.eval_result_state eval) of
          Exn.Res st =>
            let
              val notes =
                (if Toplevel.is_ignored (Command.eval_result_command eval)
                 then [("comment", "true")] else []) @
                (if null (Execution.snapshot [Command.eval_exec_id eval]) then []
                 else [("forked", "true")]);
            in MCP_State (st, notes) end
        | Exn.Exn (Fail "Unfinished lazy") => MCP_Bad ("unfinished", "")
        | Exn.Exn (Fail "Interrupt") => MCP_Bad ("interrupted", "")
        | Exn.Exn exn => MCP_Bad ("failed", Runtime.exn_message exn)));

fun mcp_bad id (status, msg) =
  mcp_finish id status [] (if msg = "" then [] else XML.blob [msg]);


(* The expensive half, in a forked task.  interrupts = false keeps the REPLY out of reach of
   an asynchronous interrupt -- Future.forks applies the job's attributes to the whole body,
   work and reply alike (future.ML:441-455) -- while Future.interruptible_task lets a cancel
   reach the work.  This is Build.build_session's pattern (build.ML:107-118).  Urgent
   priority so the reply does not queue behind a busy document's proofs, as
   query_operation.ML:49 does for the same reason.  The group is ours, not NONE: a fresh
   root group would be unreachable from Isabelle_MCP.cancel_query. *)

fun mcp_fork id group notes work =
  ignore (Future.forks
    {name = "Isabelle_MCP.query", group = SOME group, deps = [],
      pri = Task_Queue.urgent_pri, interrupts = false}
    [fn () =>
      (case Exn.capture_body (*sic!*) (Future.interruptible_task work) of
        Exn.Res ss => mcp_finish id "ok" notes (XML.blob ss)
      | Exn.Exn exn =>
          if Exn.is_interrupt exn then mcp_finish id "cancelled" [] []
          else mcp_failed id exn)]);

(* Register before running anything that can raise, so that every later failure is still
   reported against the request id (an unregistered id can never be replied to). *)

fun mcp_define name body =
  Protocol_Command.define name
    (fn args =>
      let
        val id = (case args of arg :: _ => arg | [] => "");
        val group = Future.new_group NONE;
        val _ = Synchronized.change mcp_queries (Symtab.update (id, group));
      in
        (case Exn.capture_body (fn () => body id group args) of
          Exn.Res () => ()
        | Exn.Exn exn => mcp_failed id exn)
      end);


val _ =
  mcp_define "Isabelle_MCP.proof_state"
    (fn id => fn group => fn args =>
      (case args of
        [_, node_name, command_id] =>
          (case mcp_resolve (Document.state ()) node_name (Document_ID.parse command_id) of
            MCP_Bad bad => mcp_bad id bad
          | MCP_State (st, notes) =>
              if not (Toplevel.is_proof st) then mcp_finish id "no_proof_state" [] []
              else
                mcp_fork id group notes (fn () =>
                  Toplevel.pretty_state st
                  |> Pretty.chunks
                  |> Pretty.strings_of
                  |> Markup.markup_strings Markup.state))
      | _ => mcp_bad id ("failed", "Isabelle_MCP.proof_state: bad arguments")));

(* allow_dups keeps its existing inverted meaning: rem_dups holds iff the argument is exactly
   the string "false" (find_theorems.ML:542-544).  The state of a non-proof command is a
   perfectly good search context -- Find_Theorems.proof_state falls back to Proof.init -- but
   the pristine toplevel, which is where "end" leaves you, has no context at all. *)

val _ =
  mcp_define "Isabelle_MCP.find_theorems"
    (fn id => fn group => fn args =>
      (case args of
        [_, node_name, command_id, limit, allow_dups, query] =>
          (case mcp_resolve (Document.state ()) node_name (Document_ID.parse command_id) of
            MCP_Bad bad => mcp_bad id bad
          | MCP_State (st, notes) =>
              if not (can Toplevel.context_of st) then mcp_finish id "no_context" [] []
              else
                mcp_fork id group notes (fn () =>
                  Pretty.strings_of
                    (Find_Theorems.pretty_theorems (Find_Theorems.proof_state st)
                      (Int.fromString limit) (allow_dups = "false")
                      (Find_Theorems.read_query Position.none query))
                  |> Markup.markup_strings Markup.writeln))
      | _ => mcp_bad id ("failed", "Isabelle_MCP.find_theorems: bad arguments")));

(* Parity with the overlay the caret cycle used to remove on every exit path, not a new
   capability: Execution.cancel cannot reach a task forked from a protocol command, because
   that task hangs off our own root group rather than off any exec. *)

val _ =
  Protocol_Command.define "Isabelle_MCP.cancel_query"
    (fn args =>
      let val id = (case args of arg :: _ => arg | [] => "") in
        (case mcp_take id of
          NONE => ()
        | SOME group => (Future.cancel_group group; mcp_reply id "cancelled" [] []))
      end);


(* ML debugger support (docs/archive/DEBUGGER_DESIGN.md sections 4.10 and 7.3).

   Reaching PolyML.DebuggerInterface needs one extra step: the raw global namespace this file
   compiles in carries only the four-entry PolyML stub that ML_Bootstrap.thy installs during
   Pure's bootstrap.  The full binding survives in exactly one place -- theory ML_Bootstrap's
   own ML environment.  ML_write_global is FALSE in that theory's final context (measured
   2026-08-14; the design doc had assumed true), so the config is forced back to true for
   this one compilation: reads then resolve PolyML to the surviving full structure, and the
   write of "structure Isabelle_MCP_PolyML = PolyML" lands in the raw global namespace,
   where the rest of this file can see it.

   A failure here is FATAL to the prover (--use semantics), which is deliberate: a heap where
   the re-exposure does not work must fail loudly at startup, not degrade into a debugger
   whose locals printer cannot compile.  Probe 11bis pins this on every launchable heap. *)

val _ =
  Context.setmp_generic_context
    (SOME (Context.Theory (Thy_Info.get_theory "ML_Bootstrap")
      |> Config.put_generic ML_Env.ML_write_global true))
    (fn () =>
      ML_Context.eval ML_Compiler.flags Position.none
        (ML_Lex.read "structure Isabelle_MCP_PolyML = PolyML")) ();


(* The eval wrapper.  Everything the Python side sends to Debugger.eval is embedded in
   Isabelle_MCP.debug_eval (Time.fromSeconds N) (fn () => <expression>), which

     1. registers the running thread, keyed by the debugger's own thread-name string,
        BEFORE the expression runs -- a runaway thread no longer reads its input queue, so
        anything sent later would queue forever.  The abort flag lives inside this entry:
        created with it, dead with it, so a stale abort can never touch a later evaluation.
     2. enforces the deadline as a raw Event_Timer interrupt, physical time, UNSCALED --
        Timeout.apply would multiply by timeout_scale and drift from the Scala backstop.
     3. watches the abort flag, set by the Isabelle_MCP.debug_abort protocol command.

   Both the deadline and the abort arrive as thread interrupts; ONE outermost classifier
   decides what leaves the wrapper: deadline expired or abort set -> ordinary Fail (the
   debugger's error_wrapper prints it and the thread STAYS at the breakpoint); anything
   else -> re-raised, so a genuine cancellation still kills the command.  Ties break toward
   the ordinary exception; that swallows a coalesced genuine cancellation, safely: cancelled
   Future groups are retried by the scheduler every cycle, so the swallowed cancellation is
   re-delivered at the debugger loop's next input wait.

   The body runs under explicit private_interrupts: Poly/ML's asynchronous interrupt is
   delivered only once and nothing in the debugger loop re-arms it; setting the attribute
   re-arms delivery, else the second runaway on the same thread would be unkillable.

   Every exit path drains pending interrupts (expose_interrupt_result) and classifies the
   drained result by the same rule -- an undrained late interrupt would be delivered at the
   loop's next input wait, outside any wrapper, and kill the command after a successful
   evaluation; but a genuine cancellation arriving just after the body completes must still
   be re-raised, not eaten into a success. *)

structure Isabelle_MCP =
struct

type debug_eval_entry =
  {abort: bool Unsynchronized.ref, thread: Isabelle_Thread.T, deadline: Time.time};

val debug_evals =
  Synchronized.var "Isabelle_MCP.debug_evals" (Symtab.empty: debug_eval_entry Symtab.table);

fun debug_eval timeout body =
  Thread_Attributes.with_attributes Thread_Attributes.no_interrupts (fn _ =>
    let
      val self = Isabelle_Thread.self ();
      val thread_name = Isabelle_Thread.print self;
      val abort = Unsynchronized.ref false;
      val deadline = Time.+ (Time.now (), timeout);
      val _ =
        Synchronized.change debug_evals
          (Symtab.update (thread_name, {abort = abort, thread = self, deadline = deadline}));
      val request =
        Event_Timer.request {physical = true} deadline
          (fn () => Isabelle_Thread.interrupt_thread self);
      fun ours () = ! abort orelse Time.>= (Time.now (), deadline);
      fun fail () =
        raise Fail
          (if ! abort then "Isabelle_MCP.debug_eval: ABORTED"
           else "Isabelle_MCP.debug_eval: TIMEOUT");
      val result =
        Exn.capture_body (fn () =>
          Thread_Attributes.with_attributes Thread_Attributes.private_interrupts
            (fn _ => body ()));
      val _ = Event_Timer.cancel request;
      (* deregistration: the same Synchronized var the abort sender mutates under, so the
         two are mutually excluded -- after this line no abort interrupt can be sent *)
      val _ = Synchronized.change debug_evals (Symtab.delete_safe thread_name);
      val drained = Isabelle_Thread.expose_interrupt_result ();
    in
      (case result of
        Exn.Exn exn =>
          if Exn.is_interrupt exn then
            (if ours () then fail () else Exn.reraise exn)
          else
            (case drained of
              Exn.Exn drained_exn =>
                if ours () then Exn.reraise exn else Exn.reraise drained_exn
            | Exn.Res _ => Exn.reraise exn)
      | Exn.Res res =>
          (case drained of
            Exn.Exn drained_exn => if ours () then res else Exn.reraise drained_exn
          | Exn.Res _ => res))
    end);


(* The locals printer (design section 4.10): all local variables of one stack frame, through
   the same primitives the stock print_vals verb uses -- debugState on the stopped thread is
   the loop-entry capture, since the evaluation running this code is uninstrumented -- but
   called per value inside OUR envelope, which is what makes a per-value bound possible at
   all.  No system printer is replaced or reinstalled.

   Each value gets its own 5 s bound as a raw Event_Timer plus elapsed checks against the one
   envelope, never a nested Timeout.apply: a nested inner timer that fires can drain the
   outer deadline's pending interrupt and swallow it, leaving the outer guard fictitious.
   A value that cannot print in time renders as "<printing timed out>" (no type -- type
   layout can itself be slow) while the rest still print; the outer deadline and abort take
   precedence over the per-value verdict on every path. *)

val debug_locals_value_bound = Time.fromSeconds 5;

fun debug_locals frame_index =
  let
    val self = Isabelle_Thread.self ();
    val thread_name = Isabelle_Thread.print self;
    fun outer_ours () =
      (case Symtab.lookup (Synchronized.value debug_evals) thread_name of
        NONE => false
      | SOME {abort, deadline, ...} => ! abort orelse Time.>= (Time.now (), deadline));
    val stack =
      Isabelle_MCP_PolyML.DebuggerInterface.debugState
        (Isabelle_Thread.get_thread self);
    val _ =
      if frame_index < 0 orelse frame_index >= length stack then
        raise Fail ("Isabelle_MCP.debug_locals: no stack frame " ^ string_of_int frame_index)
      else ();
    val st = List.nth (stack, frame_index);
    val space = Isabelle_MCP_PolyML.DebuggerInterface.debugNameSpace st;
    val depth = FixedInt.fromInt (ML_Print_Depth.get_print_depth ());
    fun print_value (name, v) =
      Thread_Attributes.with_attributes Thread_Attributes.no_interrupts (fn atts =>
        let
          val value_deadline = Time.+ (Time.now (), debug_locals_value_bound);
          val request =
            Event_Timer.request {physical = true} value_deadline
              (fn () => Isabelle_Thread.interrupt_thread self);
          val result =
            Exn.capture_body (fn () =>
              Thread_Attributes.with_attributes atts (fn _ =>
                Pretty.from_ML
                  (Isabelle_MCP_PolyML.NameSpace.Values.printWithType
                    (v, depth, SOME space))));
          val was_timeout = not (Event_Timer.cancel request);
          val drained = Isabelle_Thread.expose_interrupt_result ();
          fun timed_out () = Pretty.str ("val " ^ name ^ " = <printing timed out>");
        in
          (case result of
            Exn.Res p =>
              (case drained of
                Exn.Res _ => p
              | Exn.Exn drained_exn =>
                  if outer_ours () then Exn.reraise drained_exn
                  else if was_timeout then p   (*the per-value timer's own late interrupt*)
                  else Exn.reraise drained_exn)  (*genuine cancellation*)
          | Exn.Exn exn =>
              if not (Exn.is_interrupt exn) then Exn.reraise exn  (*printer error*)
              else if outer_ours () then Exn.reraise exn  (*outer deadline/abort: escalate*)
              else if was_timeout orelse Time.>= (Time.now (), value_deadline) then timed_out ()
              else Exn.reraise exn)            (*genuine cancellation*)
        end);
  in
    #allVal (Isabelle_MCP_PolyML.DebuggerInterface.debugLocalNameSpace st) ()
    |> sort_by #1
    |> map (fn (name, v) => Pretty.item [print_value (name, v)])
    |> Pretty.chunks |> Pretty.string_of |> Debugger.writeln_message
  end;

end;


(* On-demand abort of the evaluation running at a breakpoint: sets the flag inside the
   thread's registration entry and interrupts the thread.  Runs under the same Synchronized
   var as deregistration, so it can never interrupt a thread whose evaluation has already
   returned.  Unknown thread or no outstanding evaluation: a silent no-op (the Scala side
   answers from its own pending table). *)

val _ =
  Protocol_Command.define "Isabelle_MCP.debug_abort"
    (fn args =>
      let val thread_name = (case args of arg :: _ => arg | [] => "") in
        Synchronized.change Isabelle_MCP.debug_evals (fn tab =>
          ((case Symtab.lookup tab thread_name of
             NONE => ()
           | SOME {abort, thread, ...} =>
               (abort := true; Isabelle_Thread.interrupt_thread thread));
           tab))
      end);


(* At --use time there is no message channel yet, so nothing can go out over the protocol:
   Output.protocol_message is still protocol_message_undefined and RAISES Protocol_Message
   (output.ML:96, :112), and Isabelle_Process.init installs the real one only afterwards
   (isabelle_process.ML:141-142).  The plain channels do work -- init_channels has already
   pointed Output.system_message at physical stdout (output.ML:99-113) -- so TextIO.print
   here is the same destination by the shortest route.  Language_Server captures raw prover
   output during startup, which is exactly when this line matters: a prelude that fails to
   compile is fatal to the prover. *)
val _ = TextIO.print ("Isabelle_MCP: prelude " ^ mcp_prelude_version ^ " loaded\n");
