/*  Title:      Isabelle-MCP/scala/src/query.scala

Position-explicit queries: the proof state, and find_theorems, at one command --
read straight out of the prover's document state, with no overlay, no caret move
and no document update.

The prover half is ML/mcp_prelude.ML, which answers every request with exactly one
protocol message.  This half correlates those replies with the requests that asked
for them.  See docs/archive/QUERY_TOOLS_UPGRADE.md section 5.
*/

package isabelle.mcp


import isabelle._


object Query {
  /* the function name the prelude puts first in every reply; Protocol_Handlers
     dispatches on it and will not dispatch if it is not first */

  val RESULT_FUNCTION = "isabelle_mcp_query_result"

  /* Statuses.  The first nine are the prelude's own vocabulary; the last two are
     the ones only this side can observe.  The agent-facing sentence for each is
     rendered by the Python side, which is where all agent-facing text lives and
     is unit-tested -- and which, unlike ML, knows the file and line the question
     was asked about. */

  val OK = "ok"                          // the payload is the rendered result
  val UNDEFINED = "undefined"            // no such command in the current execution
  val UNFINISHED = "unfinished"          // still evaluating, so it has no state yet
  val INTERRUPTED = "interrupted"        // its evaluation was interrupted
  val NO_PROOF_STATE = "no_proof_state"  // not a proof operation
  val NO_CONTEXT = "no_context"          // nothing to search: past the theory's end
  val FAILED = "failed"                  // the payload is the prover's error text
  val CANCELLED = "cancelled"
  val CRASHED = "crashed"                // it could not answer and could not say why

  val NO_COMMAND = "no_command"          // the position resolves to no command at all
  val TIMEOUT = "timeout"

  sealed case class Result(
    status: String,
    comment: Boolean = false,   // the position is a comment or blank line
    forked: Boolean = false,    // the command still has work running elsewhere
    text: String = ""
  )
}

/* Correlation.  Nothing in Protocol_Command, Protocol_Handlers or Session tracks
   pending requests -- every handler keeps its own table (Scala.Handler is the model,
   scala.scala:292-351).

   Taking a request out of the table is the permission to answer it, the same
   discipline the prelude follows on its side: the prover's reply, the client's
   cancel, the timeout and the shutdown drain all race for one entry, and whoever
   loses stays silent.  An LSP request therefore gets exactly one response. */

class Query_Handler extends Session.Protocol_Handler {
  private val pending = Synchronized(Map.empty[String, Query.Result => Unit])

  def register(id: String, consume: Query.Result => Unit): Unit =
    pending.change(_ + (id -> consume))

  def take(id: String): Option[Query.Result => Unit] =
    pending.change_result(map => (map.get(id), map - id))

  private def handle_result(msg: Prover.Protocol_Output): Boolean = {
    for {
      id <- Properties.get(msg.properties, "id")
      consume <- take(id)
    } {
      // Protocol_Output.text throws unless the reply is exactly one chunk, and a
      // throw here would leave the request unanswered until its timeout.
      val text = msg.chunks match { case List(chunk) => chunk.text case _ => "" }
      consume(
        Query.Result(
          Properties.get(msg.properties, "status") getOrElse Query.CRASHED,
          comment = Properties.get(msg.properties, "comment").isDefined,
          forked = Properties.get(msg.properties, "forked").isDefined,
          text = text))
    }
    true
  }

  override def functions: Session.Protocol_Functions =
    List(Query.RESULT_FUNCTION -> handle_result)

  override def exit(exit_state: Document.State): Unit = {
    val orphans = pending.change_result(map => (map, Map.empty))
    for ((_, consume) <- orphans) consume(Query.Result(Query.CRASHED))
  }
}
