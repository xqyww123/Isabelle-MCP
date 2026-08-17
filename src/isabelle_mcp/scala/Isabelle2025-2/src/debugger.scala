/*  Title:      Isabelle-MCP/scala/src/debugger.scala

The ML debugger adapter: a thin layer over Isabelle's existing debugger API
(session.debugger) -- no debugger logic is reimplemented.  This half correlates
the prover's debugger messages with the requests that asked for them.  See
docs/archive/DEBUGGER_DESIGN.md sections 7.1 and 7.2.

Two hard constraints from the distribution shape everything here:

  * Session installs Debugger.Handler unconditionally, and a second protocol
    handler claiming debugger_state/debugger_output throws at init.  The
    consumer therefore subscribes to session.all_messages, matching cheaply on
    the two markup kinds and ignoring everything else.  Consumers run on the
    session dispatcher thread, which is also what orders the check-register-send
    step of a new request behind already-posted message callbacks -- so a hit's
    own entry debugger_state can never be mistaken for a completion of a
    request registered just after it.

  * The ML debugger loop emits EXACTLY ONE debugger_state per input, always
    after that input's output; the resume verbs emit the final one with an
    empty stack.  So: register, send the verb, treat the next debugger_state
    for that thread as end-of-output; an empty-stack one means the thread is no
    longer stopped ("resumed").  A round trip with no output before its
    debugger_state is a successful empty result, not a timeout.  Output is
    accumulated here, gated on the pending entry -- never read from
    Debugger.State's per-thread buffer, where late output from an abandoned
    request would be indistinguishable.

Once-only discipline: taking a request out of the table is the permission to
answer it (query.scala's invariant).  If the Scala backstop ever fires with the
prover-side timeout failed, the thread owes one debugger_state: a per-thread
debt counter eats exactly that many completion signals, output for an indebted
thread is discarded, and new evaluations on it are refused immediately ("busy")
until the late debugger_state arrives and clears the debt.

Every mutation of the pending/debt/threads bookkeeping runs on the session
dispatcher thread: the all_messages consumer, start_eval's check-register-send,
the backstop callback (its body is posted to the dispatcher; Event_Timer.cancel
cannot stop an already-fired closure, so the callback re-checks by Pending
serial before answering), and prover_exit.  No interleaving can steal a reply,
strand debt, or slip an evaluation past the busy fence.
*/

package isabelle.mcp


import isabelle._

import java.io.{File => JFile}


object Debugger_Adapter {
  /* Wire statuses; the agent-facing sentence for each is rendered by the Python side. */

  val OK = "ok"                // completion arrived, thread still stopped
  val RESUMED = "resumed"      // completion arrived with an empty stack
  val TIMEOUT = "timeout"      // the Scala backstop fired (prover-side mechanism failed)
  val CRASHED = "crashed"      // prover exit drained the request
  val BUSY = "busy"            // the thread has an outstanding evaluation or owes a state
  val NOT_STOPPED = "not_stopped"  // refused before sending: the thread is not stopped.
                                   // NOT "resumed", which means "input delivered, the
                                   // expression may have run" -- this one promises the
                                   // expression was never sent at all

  /* abort reply statuses */
  val NO_EVALUATION = "no_evaluation"  // nothing outstanding and no debt: settled
  val ABORTING = "aborting"            // flag command sent once; outcome arrives via the
                                       // evaluation's own reply (client retries, 4.13)

  /* toggle statuses raised before the prover is asked; the prover-side vocabulary is the
     query one (ok/undefined/unfinished/interrupted/failed/crashed) plus
     unknown_breakpoint, which it answers when the serial is not in the resolved command's
     context -- dual meaning with the Scala-side markup miss, noted in design 7.1 */
  val FILE_NOT_OPEN = "file_not_open"
  val OUTDATED = "outdated"
  val UNKNOWN_BREAKPOINT = "unknown_breakpoint"

  /* The Scala backstop runs this much behind the prover-side deadline, which is the
     mechanism that is supposed to fire; the backstop is the backstop. */
  val BACKSTOP_MARGIN: Double = 30.0

  sealed case class Pending(
    serial: Counter.ID,               // adapter-side identity for the backstop's compare
    token: String,
    respond: (String, List[(String, String)]) => Unit,
    timer: Event_Timer.Request,
    output: List[(String, String)])   // (kind, text), reversed

  /* Composed eval texts (design sections 4.9-4.10): constant shape.  The "val _" envelope
     binds nothing and so emits no result echo of its own; the agent's expression travels
     as ONE ML string literal and is compiled by the prelude INSIDE the wrapper's
     protection, where its result binding is echoed as "val it = ...". */

  def print_vals_text(frame: Int, timeout: Double): String =
    "val _ = Isabelle_MCP.debug_eval (Time.fromSeconds " + math.ceil(timeout).toInt.toString +
      ") (fn () => Isabelle_MCP.debug_locals " + frame.toString + ");"

  def eval_text(expr: String, timeout: Double): String =
    "val _ = Isabelle_MCP.debug_eval_string (Time.fromSeconds " +
      math.ceil(timeout).toInt.toString + ") " + ml_string_literal(expr) + ";"

  /* The literal encoder: Symbol.encode first (the form the prover-side lexer expects),
     then each UTF-8 byte as itself when printable ASCII, everything else -- including the
     two literal metacharacters -- as \ddd, three decimal digits.  The output is pure
     ASCII, so the Symbol.encode later applied to the whole input text is a no-op on it. */
  def ml_string_literal(s: String): String = {
    val bytes = UTF8.bytes(Symbol.encode(s))
    val result = new StringBuilder(bytes.length + 16)
    result += '"'
    for (b <- bytes) {
      val c = b.toInt & 0xFF
      if (c >= 32 && c <= 126 && c != '"' && c != '\\') result += c.toChar
      else result ++= "\\%03d".format(c)
    }
    result += '"'
    result.toString
  }
}

class Debugger_Adapter(server: Language_Server) {
  adapter =>

  import Debugger_Adapter._

  private def session: VSCode_Session = server.session
  private def channel: Channel = server.channel

  private val pending = Synchronized(Map.empty[String, Pending])   // thread name -> request
  private val debt = Synchronized(Map.empty[String, Int])          // thread name -> owed states
  private val threads =                                            // thread name -> stack
    Synchronized(Map.empty[String, List[(Properties.T, String)]])
  private val eval_counter = Counter.make()


  /* forwarded thread stacks: the full current map after every update */

  private def thread_json(name: String, stack: List[(Properties.T, String)]): JSON.T =
    JSON.Object(
      "thread" -> name,
      "stack" ->
        stack.map({ case (props, function) =>
          JSON.Object("function" -> function) ++
          JSON.optional("file" -> Position.File.unapply(props)) ++
          JSON.optional("line" -> Position.Line.unapply(props)) ++
          JSON.Object("pos" -> JSON.Object(props.map({ case (a, b) => a -> (b: JSON.T) }): _*))
        }))

  private def notify_state(): Unit = {
    val entries =
      for ((name, stack) <- threads.value.toList.sortBy(_._1))
        yield thread_json(name, stack)
    channel.write(LSP.Debugger_State_Notification(entries))
  }


  /* the all_messages consumer */

  private def decode_stack(msg: Prover.Protocol_Output): List[(Properties.T, String)] = {
    val body = Symbol.decode_yxml_failsafe(msg.text)
    import XML.Decode._
    list(pair(properties, string))(body)
  }

  private def decode_output(msg: Prover.Protocol_Output): List[(String, String)] =
    Symbol.decode_yxml_failsafe(msg.text) match {
      case List(XML.Elem(Markup(name, _), body)) => List((name, XML.content(body)))
      case _ => Nil
    }

  private def handle_state(thread_name: String, msg: Prover.Protocol_Output): Unit = {
    val stack = decode_stack(msg)
    threads.change(map =>
      if (stack.nonEmpty) map + (thread_name -> stack) else map - thread_name)

    // The state notification goes out BEFORE the completion reply: the channel is
    // ordered, so by the time a request returns, the client's thread map is current.
    notify_state()

    val completed =
      pending.change_result(map => (map.get(thread_name), map - thread_name)) match {
        case Some(p) =>
          p.timer.cancel()
          val status = if (stack.nonEmpty) OK else RESUMED
          p.respond(status, p.output.reverse)
          true
        case None =>
          // an owed completion signal from a request the backstop already answered
          debt.change_result(map =>
            map.get(thread_name) match {
              case Some(n) if n > 1 => (true, map + (thread_name -> (n - 1)))
              case Some(_) => (true, map - thread_name)
              case None => (false, map)
            })
      }
    // Bound the stock per-thread output buffer, which nothing here ever reads: drop it
    // after an answered round trip, after a debt payment (the abandoned runaway is the
    // thread whose buffer most plausibly grows huge), and when the thread resumes.
    if (completed || stack.isEmpty) session.debugger.clear_output(thread_name)
  }

  private def handle_output(thread_name: String, msg: Prover.Protocol_Output): Unit = {
    val messages = decode_output(msg)
    if (messages.nonEmpty) {
      val consumed =
        pending.change_result(map =>
          map.get(thread_name) match {
            case Some(p) =>
              (true, map + (thread_name -> p.copy(output = messages.reverse ::: p.output)))
            case None => (false, map)
          })
      if (!consumed) {
        // late output on an indebted thread belongs to an abandoned request: discard
        if (!debt.value.contains(thread_name)) {
          channel.write(LSP.Debugger_Output_Notification(thread_name, messages))
        }
      }
    }
  }

  private val consumer =
    Session.Consumer[Prover.Message](getClass.getName) {
      case msg: Prover.Protocol_Output =>
        msg.properties match {
          case Markup.Debugger_State(thread_name) => handle_state(thread_name, msg)
          case Markup.Debugger_Output(thread_name) => handle_output(thread_name, msg)
          case _ =>
        }
      case _ =>
    }


  /* prover exit: answer orphans as crashed (a functionless protocol handler, registered
     only for its exit drain) */

  private def prover_exit(): Unit = {
    val orphans = pending.change_result(map => (map, Map.empty))
    for (p <- orphans.values) { p.timer.cancel(); p.respond(CRASHED, Nil) }
    debt.change(_ => Map.empty)
    threads.change(_ => Map.empty)
    notify_state()
  }

  object Exit_Handler extends Session.Protocol_Handler {
    override def functions: Session.Protocol_Functions = Nil
    // exit arrives on the session manager thread; posting keeps prover_exit behind any
    // queued state callbacks (a ghost thread entry would otherwise survive the clear)
    override def exit(exit_state: Document.State): Unit =
      session.send_dispatcher { adapter.prover_exit() }
  }


  /* init and exit (server lifecycle) */

  def init(): Unit = {
    session.init_protocol_handler(Exit_Handler)
    session.all_messages += consumer
  }

  def exit(): Unit = {
    session.all_messages -= consumer
    session.send_dispatcher { prover_exit() }
  }

  /* Debugger.init is implicit: issued before the first debugger action (idempotent --
     the protocol command goes out only on the inactive-to-active flip), re-issued by the
     session-ready hook (session.scala calls debugger.ready() on phase Ready). */
  private def ensure_init(): Unit = session.debugger.init(adapter)


  /* Breakable sites with PROVER-TRUTH enabled-states (user decision): every listing
     resolves the states from the actual breakpoint refs, in ONE batched round trip --
     the write-only session.debugger mirror is not consulted.  Ranges and serials as
     found in the markup; the one-symbol shift correction of design section 3.3 is
     client-side.  Async like the toggle, so a wedged prover cannot block the main
     loop: the client-chosen timeout answers {status: timeout} instead. */

  def breakpoints(
    id: LSP.Id,
    file: JFile,
    range: Option[Line.Range],
    token: String,
    timeout: Double
  ): Unit = {
    ensure_init()
    def reply(status: String, open: Boolean, bps: List[(Line.Range, Long, JSON.T)]): Unit =
      channel.write(LSP.Debugger_Breakpoints.reply(id, status, open, bps))

    server.resources.get_rendering(file) match {
      case None => reply(Query.OK, false, Nil)
      case Some(rendering) =>
        // Outdated snapshot: rendering.breakpoints would return Nil, which the reply
        // below could not distinguish from a file with no breakable sites -- answer
        // the same status word the toggle uses for the identical condition, so the
        // client can retry once the pending edits are incorporated.  (An up-to-date
        // snapshot whose commands are not yet ML-compiled still lists no sites; that
        // ambiguity is inherent -- markup exists only after compilation.)
        if (rendering.snapshot.is_outdated) reply(OUTDATED, true, Nil)
        else {
          val doc = rendering.model.content.doc
          val text_range =
            (for (r <- range; tr <- doc.text_range(r)) yield tr)
              .getOrElse(rendering.model.content.text_range)
          val sites =
            for (Text.Info(info_range, (command, serial)) <- rendering.breakpoints(text_range))
              yield (doc.range(info_range), serial, command)
          if (sites.isEmpty) reply(Query.OK, true, Nil)
          else {
            def respond(result: Query.Result): Unit =
              if (result.status == Query.OK) {
                val states =
                  (for {
                    line <- split_lines(result.text)
                    entry <-
                      space_explode(' ', line) match {
                        case List(Value.Long(serial), word) => Some(serial -> word)
                        case _ => None
                      }
                  } yield entry).toMap
                reply(Query.OK, true,
                  sites.map({ case (r, serial, _) =>
                    val word = states.getOrElse(serial, UNKNOWN_BREAKPOINT)
                    val state: JSON.T = Value.Boolean.unapply(word).getOrElse(word)
                    (r, serial, state)
                  }))
              }
              else reply(result.status, true, Nil)
            val timer =
              Event_Timer.request(Time.now() + Time.seconds(timeout)) {
                for (respond_timeout <- server.query_handler.take(token))
                  respond_timeout(Query.Result(Query.TIMEOUT))
              }
            server.query_handler.register(token, result => { timer.cancel(); respond(result) })
            session.protocol_command_args("Isabelle_MCP.breakpoint_states",
              (token ::
                sites.flatMap({ case (_, serial, command) =>
                  List(rendering.model.node_name.node, command.id.toString, serial.toString)
                })).map(XML.string))
          }
        }
    }
  }

  /* Acknowledged toggle (design section 7.1): the write happens prover-side, on the real
     breakpoint ref, and the reply carries the PREVIOUS value -- session.debugger's
     write-only mirror is bypassed entirely.  The async pattern is query_at_position's:
     register the token in the server's shared Query_Handler (a second handler instance
     would throw at init on the duplicate function registration), arm a timer for the
     client-chosen timeout, send, return.  A timeout leaves the write possibly applied;
     absolute semantics make the client's retry idempotent, and only an "ok" may record
     an arming client-side. */

  def toggle_breakpoint(
    id: LSP.Id,
    file: JFile,
    serial: Long,
    state: Boolean,
    token: String,
    timeout: Double
  ): Unit = {
    ensure_init()
    def reply(status: String, was: Option[Boolean] = None): Unit =
      channel.write(LSP.Debugger_Toggle_Breakpoint.reply(id, status, was))

    server.resources.get_rendering(file) match {
      case None => reply(FILE_NOT_OPEN)
      case Some(rendering) =>
        if (rendering.snapshot.is_outdated) reply(OUTDATED)
        else {
          rendering.breakpoints(rendering.model.content.text_range)
            .collectFirst({ case Text.Info(_, (command, s)) if s == serial => command }) match {
            case None => reply(UNKNOWN_BREAKPOINT)
            case Some(command) =>
              def respond(result: Query.Result): Unit =
                reply(result.status,
                  if (result.status == Query.OK) Value.Boolean.unapply(result.text) else None)
              val timer =
                Event_Timer.request(Time.now() + Time.seconds(timeout)) {
                  for (respond_timeout <- server.query_handler.take(token))
                    respond_timeout(Query.Result(Query.TIMEOUT))
                }
              server.query_handler.register(token, result => { timer.cancel(); respond(result) })
              session.protocol_command_args("Isabelle_MCP.toggle_breakpoint",
                List(token, rendering.model.node_name.node, command.id.toString,
                  serial.toString, state.toString).map(XML.string))
          }
        }
    }
  }


  /* evaluation at a breakpoint.  The check-register-send step runs dispatcher-side; see
     the header comment.  respond writes the LSP reply; whoever takes the entry responds. */

  private def start_eval(
    id: LSP.Id,
    params: LSP.Debugger_Eval_Params,
    text: String
  ): Unit = {
    ensure_init()
    def respond(status: String, messages: List[(String, String)]): Unit =
      channel.write(LSP.debugger_result_reply(id, status, messages))

    session.send_dispatcher {
      val thread_name = params.thread
      if (debt.value.contains(thread_name) || pending.value.contains(thread_name)) {
        respond(BUSY, Nil)
      }
      else if (!threads.value.contains(thread_name)) {
        // Not stopped: sending would create the thread's input queue and poison its next
        // stop (Debugger.input queues for ANY name).  The check narrows the window, it
        // does not close it -- a thread resuming between this check and the prover's
        // dequeue still yields that scenario (design 7.4); the benign inverse (a fresh
        // hit whose state has not arrived) cannot happen to a client that acts on a
        // received hit notification, since the state callback precedes this check on
        // the same dispatcher.
        respond(NOT_STOPPED, Nil)
      }
      else {
        val serial = eval_counter()
        val timer =
          Event_Timer.request(Time.now() + Time.seconds(params.timeout + BACKSTOP_MARGIN)) {
            // Runs on the shared Timer thread; the whole body is posted to the dispatcher.
            // cancel() cannot stop an already-fired closure, so answer only if the entry
            // is still THIS request (look up, compare the serial, then remove).
            session.send_dispatcher {
              pending.value.get(thread_name) match {
                case Some(p) if p.serial == serial =>
                  pending.change(_ - thread_name)
                  debt.change(map => map + (thread_name -> (map.getOrElse(thread_name, 0) + 1)))
                  p.respond(TIMEOUT, p.output.reverse)
                case _ =>
              }
            }
          }
        pending.change(map =>
          map + (thread_name -> Pending(serial, params.token, respond, timer, Nil)))
        session.debugger.input(thread_name, "eval", params.frame.toString, "false",
          Symbol.encode(""), Symbol.encode(text))
      }
    }
  }

  def eval(id: LSP.Id, params: LSP.Debugger_Eval_Params): Unit =
    start_eval(id, params, eval_text(params.expr, params.timeout))

  def print_vals(id: LSP.Id, params: LSP.Debugger_Eval_Params): Unit =
    start_eval(id, params, print_vals_text(params.frame, params.timeout))


  /* on-demand abort of the outstanding evaluation (design section 4.13): sets the
     prelude-side flag; the evaluation then ends exactly as a deadline expiry does.
     Stateless here: send the flag command once and reply "aborting"; how the evaluation
     actually ended arrives through its own reply, and the client-side retry loop pins
     re-sends to that reply.  An indebted thread (its request already answered TIMEOUT
     by the backstop) is exactly the runaway abort exists for, so debt counts too. */

  def abort(id: LSP.Id, thread: Option[String], token: Option[String]): Unit = {
    def reply(status: String): Unit = channel.write(LSP.Debugger_Abort.reply(id, status))

    val thread_name =
      thread orElse
        token.flatMap(t =>
          pending.value.collectFirst({ case (name, p) if p.token == t => name }))
    thread_name match {
      case None => reply(NO_EVALUATION)
      case Some(name) =>
        if (!pending.value.contains(name) && !debt.value.contains(name)) reply(NO_EVALUATION)
        else {
          session.protocol_command("Isabelle_MCP.debug_abort", XML.string(name))
          reply(ABORTING)
        }
    }
  }


  /* resume/step verbs: no pending entry -- the completion is observed through the
     forwarded debugger_state notifications (empty stack = resumed; a new hit posts a
     fresh non-empty one) */

  def input(id: LSP.Id, thread_name: String, verbs: List[String]): Unit = {
    ensure_init()
    session.debugger.input(thread_name, verbs: _*)
    channel.write(LSP.Debugger_Input.reply(id))
  }
}
