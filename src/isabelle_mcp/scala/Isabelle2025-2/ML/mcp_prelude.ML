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

val mcp_prelude_version = "2";


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


(* At --use time there is no message channel yet, so nothing can go out over the protocol:
   Output.protocol_message is still protocol_message_undefined and RAISES Protocol_Message
   (output.ML:96, :112), and Isabelle_Process.init installs the real one only afterwards
   (isabelle_process.ML:141-142).  The plain channels do work -- init_channels has already
   pointed Output.system_message at physical stdout (output.ML:99-113) -- so TextIO.print
   here is the same destination by the shortest route.  Language_Server captures raw prover
   output during startup, which is exactly when this line matters: a prelude that fails to
   compile is fatal to the prover. *)
val _ = TextIO.print ("Isabelle_MCP: prelude " ^ mcp_prelude_version ^ " loaded\n");
