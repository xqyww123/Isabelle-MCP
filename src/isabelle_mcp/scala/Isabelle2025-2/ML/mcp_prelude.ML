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

val mcp_prelude_version = "1";


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

(* At --use time the message channel does not exist yet: Output.system_message is still
   Output_Primitives.ignore_outputs (output_primitives.ML:49), i.e. silently dropped.  Print to
   stdout instead -- Language_Server captures raw prover output during startup, so this line is
   visible exactly when it matters (a prelude that fails to compile). *)
val _ = TextIO.print ("Isabelle_MCP: prelude " ^ mcp_prelude_version ^ " loaded\n");
