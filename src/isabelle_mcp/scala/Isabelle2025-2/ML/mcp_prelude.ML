(*  Title:      Isabelle-MCP/scala/ML/mcp_prelude.ML

    ML injected into the prover by "isabelle mcp_server" via ML_Process use_prelude,
    i.e. Poly/ML "--use" AFTER the session heap is loaded but BEFORE
    "Isabelle_Process.init ()" starts the protocol loop.

    Plain SML only: this is compiled by the raw Poly/ML compiler, so no Isabelle
    antiquotations and no cartouches.

    Poly/ML treats a failing "--use" as FATAL, so a broken prelude kills the prover
    outright rather than degrading.  Language_Server guards the file's existence and
    surfaces the compiler output; keep this file small and dependency-free.

    See ISABELLE_MCP_CANCELLATION_REDESIGN_PLAN.md (and docs/CANCELLATION.md, which it
    supersedes for the cancel design) for the design and its validation.
*)

val mcp_prelude_version = "6";


(* Cancellation of document execution, using only the PUBLIC (unpatched) Isabelle/ML API,
   so that Isabelle-MCP needs no patched Isabelle distribution.

     Isabelle_MCP.cancel_evaluation serial cancel_ids probe_ids

   This is the "stanch + probe" half of a cancel request (ISABELLE_MCP_CANCELLATION_REDESIGN_
   PLAN.md section 3.1.1); the Scala side then retracts every perspective and retires each
   exec reported alive with a zero-length edit, until none is left.  Three steps, and the
   order is load-bearing:

     1. Execution.discontinue () clears the current execution id.  Command.exec registers an
        exec via "Execution.running execution_id exec_id groups", which fails once the ids no
        longer match -- so any exec that has not started yet never starts.  This cannot fail
        and runs FIRST, before anything that could raise.

     2. Probe which of probe_ids (the eval execs Scala knows on its stable version) still
        have live tasks.  Execution.snapshot answers for a whole id list with ONE flat task
        list, so a single call only says "some of these are alive"; the per-id answer comes
        from binary narrowing: a non-empty list is split in half and only non-empty halves
        are recursed into.  Attribution is by ancestry -- Task_Queue registers a task under
        every ancestor group -- so "snapshot [id]" also sees tasks forked inside the command
        body under fresh subgroups.  After discontinue the live set only shrinks, so samples
        taken at different moments stay consistent.  No cap on the number of calls: the
        protocol loop runs this inline and uninterruptibly, and the only bound is the Scala
        side's request budget, after which it terminates the prover.

     3. Execution.cancel on every id in cancel_ids (every exec Scala knows, no version
        needed).  It cancels the Future groups registered for the exec and, through the
        ancestor registration, the running threads of forked proofs too.  An id ML does not
        know is a silent no-op: Execution.cancel -> peek -> exec_groups returns [].

   Probe BEFORE cancel: a cancelled task leaves its group before a later snapshot could see
   it, and an interrupted command nobody reports is exactly the corpse the design forbids.

   The reply is ONE protocol message (function isabelle_mcp_cancel_report) with the serial,
   the number of probe calls, and -- if any step after the serial was stripped failed -- an
   "error" property; its single body chunk lists the live ids.  It goes out after every
   step, so a failure anywhere is visible in it (the Scala side treats "error" as fatal),
   and only then is the failure re-raised on the protocol thread.  On failure the whole
   probe set is reported alive: an exec nobody could examine must be retired, not forgotten.
   Only the serial is parsed outside the protected region -- a request without one cannot
   be answered at all, does nothing, and is left to the Scala side's budget.  The body chunk is always
   present, possibly empty: the Scala parser reads exactly one. *)

fun mcp_probe_alive ids =
  let
    val calls = Unsynchronized.ref 0;
    fun alive ids = (calls := ! calls + 1; not (null (Execution.snapshot ids)));
    fun narrow [] acc = acc
      | narrow [id] acc = if alive [id] then id :: acc else acc
      | narrow ids acc =
          if alive ids then
            let val (l, r) = chop (length ids div 2) ids
            in narrow l (narrow r acc) end
          else acc;
  in (narrow ids [], ! calls) end;

val _ =
  Protocol_Command.define "Isabelle_MCP.cancel_evaluation"
    (fn args =>
      (case args of
        [] => ()
      | serial :: rest =>
            let
              val result =
                Exn.capture (fn () =>
                  let
                    val _ = Execution.discontinue ();
                    val (cancel_ids, probe_ids) =
                      (case rest of
                        [cancel_arg, probe_arg] =>
                          (map Document_ID.parse (space_explode "," cancel_arg),
                            map Document_ID.parse (space_explode "," probe_arg))
                      | _ => raise Fail "Isabelle_MCP.cancel_evaluation: bad arguments");
                    val (alive, calls) =
                      (case Exn.capture mcp_probe_alive probe_ids of
                        Exn.Res res => res
                      | Exn.Exn exn =>
                          raise Fail ("probe failed: " ^ Runtime.exn_message exn));
                    val _ =
                      (case Exn.capture (List.app Execution.cancel) cancel_ids of
                        Exn.Res () => ()
                      | Exn.Exn exn =>
                          raise Fail ("cancel failed: " ^ Runtime.exn_message exn));
                  in (alive, calls) end) ();
              val (alive, calls, error) =
                (case result of
                  Exn.Res (alive, calls) => (alive, calls, [])
                | Exn.Exn exn =>
                    (* the probe set is whatever the raw argument holds, parsed leniently:
                       nothing here may raise before the report is out *)
                    let
                      val probe_ids =
                        (case rest of
                          [_, probe_arg] =>
                            map_filter (try Document_ID.parse) (space_explode "," probe_arg)
                        | _ => []);
                    in (probe_ids, 0, [("error", Runtime.exn_message exn)]) end);
              val _ =
                Output.protocol_message
                  ([("function", "isabelle_mcp_cancel_report"), ("serial", serial),
                    ("calls", Value.print_int calls)] @ error)
                  [[XML.Text (space_implode "," (map Document_ID.print alive))]];
            in Exn.release result; () end));


(* Startup probe, and the ordering probe of a cancel request.  A protocol command that ML
   does not know is NOT fatal -- the loop turns it into a system message and carries on --
   so a missing prelude would otherwise degrade silently, reporting successful cancellations
   that never happened.  Language_Server issues this ping after startup and refuses to serve
   if no pong comes back.  A cancel request sends a second ping, with its serial, right
   after the cancel command: every channel between the two sides is FIFO, so a pong that
   arrives before the cancel report proves the report lost.  The serial goes in the
   PROPERTIES, after "function" (the dispatcher reads the first property only); the body
   stays the bare version string the startup handshake compares. *)

val _ =
  Protocol_Command.define "Isabelle_MCP.ping"
    (fn args =>
      let val serial = (case args of s :: _ => s | [] => "") in
        Output.protocol_message
          [("function", "isabelle_mcp_pong"), ("serial", serial)]
          [[XML.Text mcp_prelude_version]]
      end);

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


(* The self-compiling wrapper (design section 4.9): the agent's expression arrives as ONE
   ML string literal and is compiled INSIDE the protection above, so expression content can
   neither escape the wrapper nor extend the unprotected pre-registration window -- compile
   errors, the deadline and the abort flag all classify inside debug_eval.  The explicit
   "val it = ( ... );" shell echoes the result binding through verbose = true and rejects a
   bare declaration with a parse error at the shell's own tokens.

   The OUTER text the Scala side composes ("val _ = ...;") binds nothing, and its silence
   on the wire rests on ONE guard: the debugger's evaluate also runs verbose = true and
   still flushes a writeln carrying the EMPTY string for the wildcard binding
   (ml_compiler.ML), which only the empty-message drop in Debugger.writeln_message
   (debugger.ML) keeps off the wire.  A probe pins that guard. *)

fun debug_eval_string timeout source =
  debug_eval timeout (fn () =>
    ML_Context.eval
      {environment = ML_Env.Isabelle, redirect = false, verbose = true,
       catch_all = false, debug = SOME false,
       writeln = Debugger.writeln_message, warning = Debugger.warning_message}
      Position.none
      (ML_Lex.read "val it = (" @ ML_Lex.read_source (Input.string source) @
       ML_Lex.read ");"));


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


(* Acknowledged breakpoint toggle (design section 7.1): absolute semantics, so a retry is
   idempotent.  Inline on the protocol thread -- resolving the command and flipping one ref
   is cheap -- so the "cancelled" status is unreachable here and not advertised.  The reply
   chunk carries the PREVIOUS value: only an "ok" acknowledgement may record an arming on
   the client side.  The breakpoint ref lives in the ML environment data of the defining
   command's presentation context; a serial that context does not know answers
   unknown_breakpoint. *)

val _ =
  mcp_define "Isabelle_MCP.toggle_breakpoint"
    (fn id => fn _ => fn args =>
      (case args of
        [_, node_name, command_id, serial, state] =>
          (case mcp_resolve (Document.state ()) node_name (Document_ID.parse command_id) of
            MCP_Bad bad => mcp_bad id bad
          | MCP_State (st, _) =>
              (case ML_Env.get_breakpoint (Context.Proof (Toplevel.presentation_context st))
                  (Value.parse_int serial) of
                NONE => mcp_finish id "unknown_breakpoint" [] []
              | SOME (b, _) =>
                  let val was = ! b in
                    (b := Value.parse_bool state;
                     mcp_finish id "ok" [] (XML.blob [Value.print_bool was]))
                  end))
      | _ => mcp_bad id ("failed", "Isabelle_MCP.toggle_breakpoint: bad arguments")));


(* Prover-truth breakpoint states for a listing (design section 7.1, user decision):
   Isabelle_MCP.breakpoint_states id (node_name command_id serial)* -- one batch, one
   round trip per listing.  Each triple resolves independently; a triple that cannot
   resolve carries its failure word (the toggle's vocabulary) instead of a state.  The
   reply chunk is one line per triple, "<serial> <word>", in request order. *)

val _ =
  mcp_define "Isabelle_MCP.breakpoint_states"
    (fn id => fn _ => fn args =>
      (case args of
        _ :: triple_args =>
          let
            val state = Document.state ();
            fun group_triples (a :: b :: c :: rest) = (a, b, c) :: group_triples rest
              | group_triples [] = []
              | group_triples _ =
                  raise Fail "Isabelle_MCP.breakpoint_states: ragged arguments";
            fun one (node_name, command_id, serial) =
              serial ^ " " ^
              (case mcp_resolve state node_name (Document_ID.parse command_id) of
                MCP_Bad (status, _) => status
              | MCP_State (st, _) =>
                  (case ML_Env.get_breakpoint
                      (Context.Proof (Toplevel.presentation_context st))
                      (Value.parse_int serial) of
                    NONE => "unknown_breakpoint"
                  | SOME (b, _) => Value.print_bool (! b)));
          in mcp_finish id "ok" [] (XML.blob [cat_lines (map one (group_triples triple_args))]) end
      | [] => mcp_bad id ("failed", "Isabelle_MCP.breakpoint_states: bad arguments")));


(* At --use time there is no message channel yet, so nothing can go out over the protocol:
   Output.protocol_message is still protocol_message_undefined and RAISES Protocol_Message
   (output.ML:96, :112), and Isabelle_Process.init installs the real one only afterwards
   (isabelle_process.ML:141-142).  The plain channels do work -- init_channels has already
   pointed Output.system_message at physical stdout (output.ML:99-113) -- so TextIO.print
   here is the same destination by the shortest route.  Language_Server captures raw prover
   output during startup, which is exactly when this line matters: a prelude that fails to
   compile is fatal to the prover. *)
val _ = TextIO.print ("Isabelle_MCP: prelude " ^ mcp_prelude_version ^ " loaded\n");
