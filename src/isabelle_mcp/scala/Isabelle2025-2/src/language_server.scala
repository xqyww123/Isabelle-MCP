/*  Title:      Tools/VSCode/src/language_server.scala
    Author:     Makarius

Server for VS Code Language Server Protocol 2.0/3.0, see also
https://github.com/Microsoft/language-server-protocol
https://github.com/Microsoft/language-server-protocol/blob/master/protocol.md

PIDE protocol extensions depend on system option "vscode_pide_extensions".
*/

package isabelle.mcp


import isabelle._

import java.io.{File => JFile}

import scala.collection.mutable
import scala.annotation.tailrec


object Language_Server {
  /* The prelude version this jar was built against.

     ML/mcp_prelude.ML is not listed in build.props sources, so it is absent from the jar's
     recorded hashes and nothing else would notice an edited, stale or reverted prelude.
     That was tolerable while the two sides shared only a cancel command; they now share
     the query commands, the debugger eval wrapper and a reply format, and a skew there is
     a request that hangs with no correlatable trace.  Bump this whenever mcp_prelude.ML's
     protocol changes. */

  val prelude_version = "6"

  /* The client<->server wire version, a plain integer starting at 1, independent of the
     package version; emitted as a top-level field of the initialize reply and compared by
     the Python client before it reports a successful launch.  Bump it whenever the wire
     between the two changes (a message, a field, a reply shape). */

  val protocol_version = 1

  /* bounded wait on a promise, against a deadline; the polling idiom of await_pong, shared */

  def await_promise[A](promise: Promise[A], deadline: Time): Option[A] = {
    val step = Time.seconds(0.05)
    while (!promise.is_finished && Time.now() < deadline) step.sleep()
    if (promise.is_finished) Some(promise.join) else None
  }

  /* The two protocol args that identify a command to ML/mcp_prelude.ML's mcp_resolve: the
     node that OWNS the command, and its id.  Both are taken FROM the Command, never from a
     file model's own node -- load-bearing for `.ML` blobs: a blob's breakable / queried
     command belongs to the LOADER theory's node (the ML_file command), not the blob file's
     node, so command_exec against the blob node would raise -> "undefined".  Every site that
     sends a (node, command_id) pair to mcp_resolve routes through here, so the wrong-node
     pairing cannot recur (it was the same bug at all three send sites). */

  def command_ref(command: Command): (String, String) =
    (command.node_name.node, command.id.toString)


  /* the prelude's cancel report: one protocol message per Isabelle_MCP.cancel_evaluation,
     matched to its request by serial (see ML/mcp_prelude.ML).  "error" is set when any
     step of the protocol command failed; the prover then reports the whole probe set as
     alive. */

  sealed case class Cancel_Report(alive: List[Document_ID.Exec], calls: Int, error: Option[String])

  class Cancel_Handler extends Session.Protocol_Handler {
    private val pending = Synchronized(Map.empty[String, Promise[Cancel_Report]])

    def register(serial: String): Promise[Cancel_Report] = {
      val promise = Future.promise[Cancel_Report]
      pending.change(_ + (serial -> promise))
      promise
    }

    def forget(serial: String): Unit = pending.change(_ - serial)

    private def handle_report(msg: Prover.Protocol_Output): Boolean = {
      // parse first, take the registration last: an exception in between would leave the
      // request waiting on a promise nobody holds any more
      for (serial <- Properties.get(msg.properties, "serial")) {
        // exactly one body chunk (possibly empty); msg.text throws Malformed on anything
        // else, leaving the promise unfulfilled -- the pong then reports the loss
        val alive = space_explode(',', msg.text).filter(_.nonEmpty).map(Value.Long.parse)
        val calls = Properties.get(msg.properties, "calls").map(Value.Int.parse).getOrElse(-1)
        val report = Cancel_Report(alive, calls, Properties.get(msg.properties, "error"))
        for (promise <- pending.change_result(map => (map.get(serial), map - serial))) {
          promise.fulfill(report)
        }
      }
      true
    }

    override def functions: Session.Protocol_Functions =
      List("isabelle_mcp_cancel_report" -> handle_report)
  }


  /* proof that the injected ML prelude is live, and the ordering probe of a cancel

     An undefined protocol command is NOT fatal in ML -- the protocol loop downgrades it to a
     system message and carries on (isabelle_process.ML) -- so without this probe a prover
     without the prelude would happily accept every cancel request and cancel nothing.
     Isabelle_MCP.ping answers with an isabelle_mcp_pong protocol message carrying the
     prelude's version in its body and, when the ping carried one, the serial in its
     properties.  A pong without a serial, or with one nobody registered, fulfils the startup
     handshake -- so a new jar with an old prelude still reaches the version diagnostic. */

  class Prelude_Handler extends Session.Protocol_Handler {
    private val startup = Future.promise[String]
    private val pending = Synchronized(Map.empty[String, Promise[String]])

    def register(serial: String): Promise[String] = {
      val promise = Future.promise[String]
      pending.change(_ + (serial -> promise))
      promise
    }

    def forget(serial: String): Unit = pending.change(_ - serial)

    private def handle_pong(msg: Prover.Protocol_Output): Boolean = {
      val text = msg.text
      val taken =
        Properties.get(msg.properties, "serial").flatMap(serial =>
          pending.change_result(map => (map.get(serial), map - serial)))
      taken match {
        case Some(promise) => promise.fulfill(text)
        case None => if (!startup.is_finished) startup.fulfill(text)
      }
      true
    }

    override def functions: Session.Protocol_Functions =
      List("isabelle_mcp_pong" -> handle_pong)

    def await_pong(timeout: Time): Option[String] =
      await_promise(startup, Time.now() + timeout)
  }


  /* build session */

  def build_session(options: Options, logic: String,
    build_progress: Progress = new Progress,
    session_dirs: List[Path] = Nil,
    include_sessions: List[String] = Nil,
    session_ancestor: Option[String] = None,
    session_requirements: Boolean = false,
    session_no_build: Boolean = false,
    build_started: String => Unit = _ => (),
    build_failed: String => Unit = _ => ()
  ): Sessions.Background = {
    val session_background =
      Sessions.background(
        options, logic, dirs = session_dirs,
        include_sessions = include_sessions, session_ancestor = session_ancestor,
        session_requirements = session_requirements).check_errors

    def build(no_build: Boolean = false, progress: Progress = new Progress): Build.Results =
      Build.build(options,
        selection = Sessions.Selection.session(logic),
        build_heap = true, no_build = no_build, dirs = session_dirs,
        infos = session_background.infos,
        progress = progress)

    if (!session_no_build && !build(no_build = true).ok) {
      build_started(logic)
      if (!build(progress = build_progress).ok) build_failed(logic)
    }

    session_background
  }


  /* abstract editor operations */

  class Editor(server: Language_Server) extends isabelle.Editor {
    type Context = Unit


    /* PIDE session and document model */

    override def session: VSCode_Session = server.session
    override def flush(): Unit = session.resources.flush_input(session, server.channel)

    override def get_models(): Iterable[Document.Model] = session.resources.get_models()


    /* input from client */

    private val delay_input: Delay =
      Delay.last(server.options.seconds("vscode_input_delay"), server.channel.Error_Logger) {
        session.resources.flush_input(session, server.channel)
      }

    override def invoke(): Unit = delay_input.invoke()
    override def revoke(): Unit = delay_input.revoke()


    /* current situation */

    override def current_node(context: Unit): Option[Document.Node.Name] =
      session.resources.get_caret().map(_.model.node_name)
    override def current_node_snapshot(context: Unit): Option[Document.Snapshot] =
      session.resources.get_caret().map(caret => session.resources.snapshot(caret.model))

    override def node_snapshot(name: Document.Node.Name): Document.Snapshot = {
      session.resources.get_snapshot(name) match {
        case Some(snapshot) => snapshot
        case None => session.snapshot(name)
      }
    }

    def current_command(snapshot: Document.Snapshot): Option[Command] = {
      session.resources.get_caret() match {
        case Some(caret) if snapshot.loaded_theory_command(caret.offset).isEmpty =>
          snapshot.current_command(caret.node_name, caret.offset)
        case _ => None
      }
    }
    override def current_command(context: Unit, snapshot: Document.Snapshot): Option[Command] =
      current_command(snapshot)


    /* output messages */

    override def output_state(): Boolean =
      session.resources.options.bool("editor_output_state")


    /* overlays */

    override def node_overlays(name: Document.Node.Name): Document.Node.Overlays =
      session.resources.node_overlays(name)

    override def insert_overlay(command: Command, fn: String, args: List[String]): Unit =
      session.resources.insert_overlay(command, fn, args)

    override def remove_overlay(command: Command, fn: String, args: List[String]): Unit =
      session.resources.remove_overlay(command, fn, args)


    /* hyperlinks */

    override def hyperlink_command(
      snapshot: Document.Snapshot,
      id: Document_ID.Generic,
      offset: Symbol.Offset = 0,
      focus: Boolean = false,
    ): Option[Hyperlink] = {
      if (snapshot.is_outdated) None
      else
        snapshot.find_command_position(id, offset).map(node_pos =>
          new Hyperlink {
            def follow(unit: Unit): Unit = server.channel.write(LSP.Caret_Update(node_pos, focus))
          })
    }


    /* dispatcher thread */

    override def assert_dispatcher[A](body: => A): A = session.assert_dispatcher(body)
    override def require_dispatcher[A](body: => A): A = session.require_dispatcher(body)
    override def send_dispatcher(body: => Unit): Unit = session.send_dispatcher(body)
    override def send_wait_dispatcher(body: => Unit): Unit = session.send_wait_dispatcher(body)
  }
}

class Language_Server(
  val channel: Channel,
  val options: Options,
  session_name: String = Isabelle_System.default_logic(),
  include_sessions: List[String] = Nil,
  session_dirs: List[Path] = Nil,
  session_ancestor: Option[String] = None,
  session_requirements: Boolean = false,
  session_no_build: Boolean = false,
  modes: List[String] = Nil,
  log: Logger = new Logger
) {
  server =>

  val editor: Language_Server.Editor = new Language_Server.Editor(server)


  /* prover session */

  private val session_ = Synchronized(None: Option[VSCode_Session])
  def session: VSCode_Session = session_.value getOrElse error("Server inactive")
  def resources: VSCode_Resources = session.resources
  def ml_settings: ML_Settings = session.store.ml_settings

  private val sledgehammer = new VSCode_Sledgehammer(server)
  private val find_theorems = new VSCode_Find_Theorems(server)
  private val debugger_adapter = new Debugger_Adapter(server)

  def rendering_offset(node_pos: Line.Node_Position): Option[(VSCode_Rendering, Text.Offset)] =
    for {
      rendering <- resources.get_rendering(new JFile(node_pos.name))
      offset <- rendering.model.content.doc.offset(node_pos.pos)
    } yield (rendering, offset)

  private val dynamic_output = Dynamic_Output(server)


  /* input from client or file-system */

  private val file_watcher: MCP_File_Watcher =
    MCP_File_Watcher(sync_documents, options.seconds("vscode_load_delay"))

  private val delay_load: Delay =
    Delay.last(options.seconds("vscode_load_delay"), channel.Error_Logger) {
      val (invoke_input, invoke_load) =
        resources.resolve_dependencies(session, editor, file_watcher)
      if (invoke_input) editor.invoke()
      if (invoke_load) delay_load.invoke()
    }

  private def close_document(file: JFile): Unit = {
    if (resources.close_model(file)) {
      file_watcher.register_parent(file)
      sync_documents(Set(file))
      editor.invoke()
      delay_output.invoke()
    }
  }

  private def sync_documents(changed: Set[JFile]): Unit = {
    resources.sync_models(changed)
    editor.invoke()
    delay_output.invoke()
  }

  private def change_document(
    file: JFile,
    version: Long,
    changes: List[LSP.TextDocumentChange]
  ): Unit = {
    changes.foreach(change =>
      resources.change_model(session, editor, file, version, change.text, change.range))

    editor.invoke()
    delay_output.invoke()
  }


  /* caret handling */

  private val delay_caret_update: Delay =
    Delay.last(options.seconds("vscode_input_delay"), channel.Error_Logger) {
      session.caret_focus.post(Session.Caret_Focus)
    }

  private def update_caret(caret: Option[(JFile, Line.Position)]): Unit = {
    resources.update_caret(caret)
    delay_caret_update.invoke()
    editor.invoke()
  }


  /* preview */

  private lazy val preview_panel = new Preview_Panel(resources)

  private lazy val delay_preview: Delay =
    Delay.last(options.seconds("vscode_output_delay"), channel.Error_Logger) {
      if (preview_panel.flush(channel)) delay_preview.invoke()
    }

  private def preview_request(file: JFile, column: Int): Unit = {
    preview_panel.request(file, column)
    delay_preview.invoke()
  }


  /* output to client */

  /* delay_output.invoke() is the one "something may have changed" signal.  Delay.first: a
     busy prover posts Commands_Changed every editor_output_delay, and Delay.last would
     re-arm on each of them and never fire (measured: pushes withheld for seconds, no bound
     in principle).  So from the first signal to flush_output is at most
     vscode_output_delay, and every visible model is rendered on each run (I-2). */

  private val delay_output: Delay =
    Delay.first(options.seconds("vscode_output_delay"), channel.Error_Logger) {
      if (resources.flush_output(session, channel)) delay_output.invoke()
    }

  private val prover_output =
    Session.Consumer[Session.Commands_Changed](getClass.getName) {
      case _ => delay_output.invoke()
    }

  private val syslog_messages =
    Session.Consumer[Prover.Output](getClass.getName) {
      case output => channel.log_writeln(resources.output_text(XML.content(output.message)))
    }


  /* decoration request */

  private def decoration_request(file: JFile): Unit =
    resources.force_decorations(channel, file)


  /* init and exit */

  def init(id: LSP.Id): Unit = {
    def reply_ok(msg: String): Unit = {
      channel.write(LSP.Initialize.reply(id, ""))
      channel.writeln(msg)
    }

    def reply_error(msg: String): Unit = {
      channel.write(LSP.Initialize.reply(id, msg))
      channel.error_message(msg)
    }

    val try_session =
      try {
        val progress = channel.progress(verbose = true)
        val session_background =
          Language_Server.build_session(options, session_name,
            session_dirs = session_dirs,
            include_sessions = include_sessions,
            session_ancestor = session_ancestor,
            session_requirements = session_requirements,
            session_no_build = session_no_build,
            build_started = { logic =>
              val msg = Build.build_logic_started(logic)
              progress.echo(msg)
              channel.writeln(msg) },
            build_failed = { logic =>
              val msg = Build.build_logic_failed(logic, editor = true)
              progress.echo(msg)
              error(msg) })

        val session_resources = new VSCode_Resources(options, session_background, log)
        val session_options = options.bool.update("editor_output_state", true)
        val session =
          new VSCode_Session(session_options, session_resources) {
            override def deps_changed(): Unit = delay_load.invoke()
          }

        Some((session_background, session))
      }
      catch { case ERROR(msg) => reply_error(msg); None }

    for ((session_background, session) <- try_session) {
      val store = Store(options)
      val session_heaps =
        store.session_heaps(session_background, logic = session_background.session_name)

      session_.change(_ => Some(session))

      session.commands_changed += prover_output
      session.syslog_messages += syslog_messages

      dynamic_output.init()
      sledgehammer.init()
      find_theorems.init()

      /* The ML prelude is a HARD startup dependency: Poly/ML treats a "--use" file that is
         missing or fails to compile as fatal, so a broken prelude does not degrade
         cancellation, it kills the prover -- and Poly/ML's reason goes to stdout, which is not
         syslog, so without the capture below the user would only ever see "Return code 1".
         Guard the file, keep the raw output, and prove the prelude is live with a ping. */

      val prelude = MCP_Main.prelude_ml.expand

      val raw_output = Synchronized(List.empty[String])
      val raw_output_capture =
        Session.Consumer[Prover.Output](getClass.getName + "/startup") { output =>
          val text = XML.content(output.message).trim
          if (text.nonEmpty) raw_output.change(text :: _)
        }

      def startup_details: String =
        raw_output.value.reverse match {
          case Nil => ""
          case lines => "\nProver output:\n" + lines.mkString("\n")
        }

      session.init_protocol_handler(prelude_handler)
      session.init_protocol_handler(cancel_handler)
      session.init_protocol_handler(query_handler)
      debugger_adapter.init()
      session.raw_output_messages += raw_output_capture

      try {
        if (!prelude.is_file) {
          error("Missing ML prelude: " + prelude +
            "\nThe Isabelle-MCP component is incomplete; reinstall it.")
        }

        Isabelle_Process.start(
          options, session, session_background, session_heaps, modes = modes,
          use_prelude = List(File.standard_path(prelude))).await_startup()

        session.protocol_command("Isabelle_MCP.ping")
        prelude_handler.await_pong(Time.seconds(10)) match {
          case None =>
            error("The ML prelude did not answer: cancellation would silently do nothing." +
              "\nPrelude: " + prelude + startup_details)
          case Some(version) if version != Language_Server.prelude_version =>
            error("The ML prelude is version " + quote(version) + ", but this build of " +
              "Isabelle-MCP speaks version " + quote(Language_Server.prelude_version) + "." +
              "\nThey share the query commands, the debugger eval wrapper and a reply" +
              " format, so serving with a skew between them would hang requests rather" +
              " than fail them." +
              "\nPrelude: " + prelude +
              "\nReinstall the Isabelle-MCP component so that both halves come from one build.")
          case Some(_) =>
        }

        reply_ok(
          "Welcome to Isabelle/" + session_background.session_name +
          Isabelle_System.isabelle_heading())
      }
      catch { case ERROR(msg) => reply_error(msg + startup_details) }
      finally { session.raw_output_messages -= raw_output_capture }
    }
  }

  def shutdown(id: LSP.Id): Unit = {
    def reply(err: String): Unit = channel.write(LSP.Shutdown.reply(id, err))

    session_.change({
      case Some(session) =>
        session.commands_changed -= prover_output
        session.syslog_messages -= syslog_messages

        dynamic_output.exit()

        delay_load.revoke()
        file_watcher.shutdown()
        editor.revoke()
        delay_output.revoke()
        delay_caret_update.revoke()
        delay_preview.revoke()
        sledgehammer.exit()
        find_theorems.exit()
        debugger_adapter.exit()

        val result = session.stop()
        if (result.ok) reply("")
        else reply("Prover shutdown failed: " + result.rc)
        None
      case None =>
        reply("Prover inactive")
        None
    })
  }

  def exit(): Unit = {
    log("\n")
    sys.exit(if (session_.value.isEmpty) Process_Result.RC.ok else Process_Result.RC.failure)
  }


  /* completion */

  def completion(id: LSP.Id, node_pos: Line.Node_Position): Unit = {
    val result =
      (for ((rendering, offset) <- rendering_offset(node_pos))
        yield rendering.completion(node_pos, offset)) getOrElse Nil
    channel.write(LSP.Completion.reply(id, result))
  }


  /* spell-checker dictionary */

  def update_dictionary(include: Boolean, permanent: Boolean): Unit = {
    for {
      spell_checker <- resources.spell_checker.get
      caret <- resources.get_caret()
      rendering = resources.rendering(caret.model)
      range = rendering.before_caret_range(caret.offset)
      Text.Info(_, word) <- Spell_Checker.current_word(rendering, range)
    } {
      spell_checker.update(word, include, permanent)
      delay_output.invoke()
    }
  }

  def reset_dictionary(): Unit = {
    for (spell_checker <- resources.spell_checker.get) {
      spell_checker.reset()
      delay_output.invoke()
    }
  }


  /* hover */

  def hover(id: LSP.Id, node_pos: Line.Node_Position): Unit = {
    val result =
      for {
        (rendering, offset) <- rendering_offset(node_pos)
        info <- rendering.tooltips(VSCode_Rendering.tooltip_elements, Text.Range(offset, offset + 1))
      } yield {
        val range = rendering.model.content.doc.range(info.range)
        val contents = info.info.map(t => LSP.MarkedString(resources.output_pretty_tooltip(List(t))))
        (range, contents)
      }
    channel.write(LSP.Hover.reply(id, result))
  }


  /* goto definition */

  def goto_definition(id: LSP.Id, node_pos: Line.Node_Position): Unit = {
    val result =
      (for ((rendering, offset) <- rendering_offset(node_pos))
        yield rendering.hyperlinks(Text.Range(offset, offset + 1))) getOrElse Nil
    channel.write(LSP.GotoDefinition.reply(id, result))
  }


  /* document highlights */

  def document_highlights(id: LSP.Id, node_pos: Line.Node_Position): Unit = {
    val result =
      (for ((rendering, offset) <- rendering_offset(node_pos))
        yield {
          val model = rendering.model
          rendering.caret_focus_ranges(Text.Range(offset, offset + 1), model.content.text_range)
            .map(r => LSP.DocumentHighlight.text(model.content.doc.range(r)))
        }) getOrElse Nil
    channel.write(LSP.DocumentHighlights.reply(id, result))
  }


  /* code actions */

  def code_action_request(id: LSP.Id, file: JFile, range: Line.Range): Unit = {
    for {
      model <- resources.get_model(file)
      version <- model.version
      doc = model.content.doc
      text_range <- doc.text_range(range)
    } {
      val snapshot = resources.snapshot(model)
      val results =
        snapshot.command_results(Text.Range(text_range.start - 1, text_range.stop + 1))
          .iterator.map(_._2).toList
      val actions =
        List.from(
          for {
            (snippet, props) <- Protocol.sendback_snippets(results).iterator
            id <- Position.Id.unapply(props)
            (node, command) <- snapshot.find_command(id)
            start <- node.command_start(command)
            range = command.core_range + start
            current_text <- model.get_text(range)
          } yield {
            val line_range = doc.range(range)
            val edit_text =
              if (props.contains(Markup.PADDING_COMMAND)) {
                val whole_line = doc.lines(line_range.start.line)
                val indent = whole_line.text.takeWhile(_.isWhitespace)
                current_text + "\n" + Library.prefix_lines(indent, snippet)
              }
              else current_text + snippet
            val edit = LSP.TextEdit(line_range, resources.output_edit(edit_text))
            LSP.CodeAction(snippet, List(LSP.TextDocumentEdit(file, Some(version), List(edit))))
          })
      channel.write(LSP.CodeActionRequest.reply(id, actions))
    }
  }


  /* abbrevs */

  def abbrevs_request(): Unit = {
    val syntax = session.resources.session_base.overall_syntax
    channel.write(LSP.Abbrevs_Request.reply(syntax.abbrevs))
  }


  def documentation_request(): Unit =
    channel.write(LSP.Documentation_Response(ml_settings))


  /* theory status: every row from ONE document state, stamped with its version (I-1a) */

  def theory_status(id: LSP.Id): Unit = {
    val now = Date.now()
    val models = resources.get_models()
    val doc_state = session.get_state()
    val pending_edits = Document.Pending_Edits.make(models)
    val document_version = doc_state.snapshot(pending_edits = pending_edits).version.id
    val theories =
      (for (model <- models.iterator) yield {
        val snapshot = doc_state.snapshot(node_name = model.node_name, pending_edits = pending_edits)
        val status = Document_Status.Node_Status.make(
          now = now,
          state = snapshot.state,
          version = snapshot.version,
          name = model.node_name)
        model.node_name.json ++
          JSON.Object(
            "external" -> model.external_file,
            "imports" -> snapshot.node.header.imports.map(_.json)) ++
          status.json
      }).toList
    channel.write(LSP.Theory_Status.reply(id, document_version, theories))
  }


  /* budget-bounded requests: what the cancel and flush requests share

     Each runs on its own bare thread (not the fixed-size Future pool: an abandoned wait
     would occupy a pool thread) against ONE deadline, and replies at most once, from its
     finally.  Manager round trips are made on a further bare thread and awaited against
     the deadline; a wait given up leaks its daemon thread until the JVM goes. */

  private val request_poll_step = Time.seconds(0.05)

  private def check_ready(session: VSCode_Session): Unit =
    session.phase match {
      case Session.Ready =>
      case Session.Inactive => VSCode_Resources.request_fail("prover never started")
      case Session.Startup => VSCode_Resources.request_fail("prover still starting")
      case Session.Shutdown => VSCode_Resources.request_fail("prover shutting down")
      case Session.Terminated(_) => VSCode_Resources.request_fail("prover already terminated")
    }

  /* Poll the document state until `ready` holds.  check_ready runs FIRST on every pass,
     before the state is read: it is the whole guard against a prover that exits while a
     request waits for an assignment.  A Raw_Edits the manager drops because the prover is
     gone has phase = Terminated set before it, on the manager thread, so the first pass
     after session.update returns already fails; a prover that dies after accepting the
     edits never assigns, the tip never becomes stable, and the poll ends at the phase
     flip or at the deadline -- never a false success. */

  private def await_state(
    session: VSCode_Session,
    deadline: Time,
    reason: String
  )(ready: Document.State => Boolean): Document.State = {
    val promise = Future.promise[Document.State]
    val stop = new java.util.concurrent.atomic.AtomicBoolean(false)
    Isabelle_Thread.fork(name = "await_state", daemon = true) {
      try {
        while (!stop.get && !promise.is_finished) {
          check_ready(session)
          val st = session.get_state()
          if (ready(st)) promise.fulfill(st) else request_poll_step.sleep()
        }
      }
      catch { case exn: Throwable => promise.fulfill_result(Exn.Exn(exn)) }
    }
    val st =
      Language_Server.await_promise(promise, deadline) getOrElse {
        stop.set(true)
        VSCode_Resources.request_fail(reason)
      }
    // A manager that has shut down answers with Document.State.init (session.scala); so
    // does a live session that has not seen a single update yet.  The phase tells them
    // apart, up to the moment it flips; past that the budget does.  Kept beside the
    // per-pass check above because session.stop() writes phase Shutdown BEFORE the
    // manager's Stop resets the state, so a pass that read Ready before the flip and the
    // init singleton after it must still fail: this re-reads the phase AFTER the state.
    if (st eq Document.State.init) check_ready(session)
    st
  }


  /* the flush request: PIDE/flush (ISABELLE_MCP_DECORATION_VERSION_STAMP_PLAN.md section 3.1 (d))

     Absorb everything the client has sent, hand it to the prover, and name an assigned
     version that contains it all.  Steps: (1) when asked, re-read every dependency file;
     (2) resolve imports; (3) flush_input -- one session.update, NOT forked and NOT bounded
     separately: the reply's soundness is "send_wait returned => our change is history.tip
     => any stable tip the poll then sees is that change or a descendant"; (4) poll until
     the tip is stable; (5) reply.  Steps 1-3 hold the monitor (timed entry); step 3's
     session.update and the round trips inside flush_edits are untimed by construction, so
     the client bounds the whole request with its own hard timeout.  Two overlapping
     flushes serialise on the monitor.  document_version 0 (Version.init) is the legitimate
     answer of a session that has handed the prover nothing yet. */

  private val flush_budget = Time.seconds(120)

  def flush(id: LSP.Id, resync_dependencies: Boolean): Unit = {
    Isabelle_Thread.fork(name = "flush", daemon = true) {
      val deadline = Time.now() + flush_budget
      var reply: JSON.T = LSP.Flush.error(id, "no result")
      try {
        val (document_version, changed_files) = flush_body(deadline, resync_dependencies)
        reply = LSP.Flush.reply(id, document_version, changed_files)
      }
      catch {
        case exn: VSCode_Resources.Request_Failure =>
          log("flush failed: " + exn.reason)
          reply = LSP.Flush.error(id, exn.reason)
        case exn: Throwable =>
          log("flush failed: " + Exn.message(exn))
          reply = LSP.Flush.error(id, "internal failure: " + Exn.message(exn))
      }
      finally { channel.write(reply) }
    }
  }

  private def flush_body(deadline: Time, resync_dependencies: Boolean): (Long, List[JFile]) = {
    val session =
      try { this.session }
      catch { case ERROR(_) => VSCode_Resources.request_fail("server inactive") }
    check_ready(session)

    val changed_files =
      if (resync_dependencies) {
        val (changed, vanished) = resources.resync_external_models(Some(deadline))
        changed ::: vanished
      }
      else Nil

    val (_, invoke_load) =
      resources.resolve_dependencies(session, editor, file_watcher, Some(deadline))
    if (invoke_load) delay_load.invoke()
    // the resolved models are in pending_input: step 3 flushes them along with the rest

    resources.flush_input(session, channel, Some(deadline))

    val st =
      await_state(session, deadline, "no assigned version within the budget")(
        _.stable_tip_version.isDefined)
    val version =
      st.stable_tip_version getOrElse VSCode_Resources.request_fail("stable tip vanished")
    (version.id, changed_files)
  }

  /* cancellation: PIDE/cancel_evaluation (ISABELLE_MCP_CANCELLATION_REDESIGN_PLAN.md section 3)

     One request, one reply, three outcomes: retired, nothing_running, aborted.  In order:

       stanch + probe -- Isabelle_MCP.cancel_evaluation (ML/mcp_prelude.ML) discontinues the
                   execution, probes which eval execs still have live tasks, and cancels every
                   exec Scala knows.  A ping follows the cancel; the manager mailbox, the ML
                   protocol loop and the ML message channel are all FIFO, so a pong that
                   arrives without the report means the report was lost, not delayed (F18).
       retract  -- step 0, once: caret gone, overlay table emptied, every model's perspective
                   emptied.  The prover stops handing out work; nothing finished is lost (a
                   finished exec keeps its command alive in the common prefix, F2).
       retire   -- a loop: each exec still alive gets a zero-length edit inside its command
                   on the stable tip V, so the command is re-minted under a fresh id and the
                   prover cancels and purges the interrupted exec.  A round whose edit did not
                   land is retried; the same target failing twice in a row is a failure of
                   the request.

     Everything waits against ONE deadline, 120 s from the moment the body starts; the
     budget running out, the prover vanishing, a lost report, or any exception is the
     aborted outcome, and the Python side then terminates the prover.  Thread and reply
     discipline as for every budget-bounded request above; a client that has stopped
     reading gets no reply, and is covered by the Python side's 135 s gate.

     The reply carries document_version, the reply version: an assigned version that
     contains every edit the request made -- the stable tip after the last awaited
     assignment, v0 when the request sent nothing. */

  private val cancel_handler = new Language_Server.Cancel_Handler
  private val prelude_handler = new Language_Server.Prelude_Handler
  private val cancel_serial = Counter.make()

  private val cancel_budget = Time.seconds(120)
  private val cancel_update_bound = Time.seconds(5)   // session.update under the monitor; Z14(b)

  private def cancel_aborted(reason: String): JSON.T =
    JSON.Object("outcome" -> "aborted", "reason" -> reason)

  def cancel_evaluation(id: LSP.Id): Unit = {
    Isabelle_Thread.fork(name = "cancel_evaluation", daemon = true) {
      val deadline = Time.now() + cancel_budget
      var reply: JSON.T = cancel_aborted("no result")
      try { reply = cancel_evaluation_body(deadline) }
      catch {
        case exn: VSCode_Resources.Request_Failure =>
          log("cancel_evaluation aborted: " + exn.reason)
          reply = cancel_aborted(exn.reason)
        case exn: Throwable =>
          log("cancel_evaluation failed: " + Exn.message(exn))
          reply = cancel_aborted("internal failure: " + Exn.message(exn))
      }
      finally { channel.write(LSP.Cancel_Evaluation.reply(id, reply)) }
    }
  }

  /* rendering off the version the command was found on: no document model, no monitor */

  private def cancel_command_json(
    name: Document.Node.Name,
    command: Command,
    version: Document.Version
  ): JSON.Object.T = {
    val lines =
      version.nodes(name).command_start_line(command) match {
        case Some(line) =>
          JSON.Object("line" -> line, "end_line" -> (line + Library.count_newlines(command.source)))
        case None => JSON.Object()
      }
    JSON.Object(
      "file" -> name.node,
      "command" -> command.span.name,
      "id" -> command.id) ++ lines ++
    JSON.Object("loads" -> command.blobs_names.map(_.node))
  }

  private def cancel_evaluation_body(deadline: Time): JSON.T = {
    def fail(reason: String): Nothing = VSCode_Resources.request_fail(reason)

    val session = try { this.session } catch { case ERROR(_) => fail("server inactive") }

    def check_ready(): Unit = server.check_ready(session)
    check_ready()
    // a precondition of retraction: with 0 the caret branch is skipped and every visible
    // model's perspective is its full text, never empty (vscode_model.scala)
    if (options.int("vscode_caret_perspective") == 0) {
      fail("vscode_caret_perspective is 0: no perspective can be retracted")
    }

    /* manager round trips, awaited against the deadline (await_state above); giving up a
       session.update does not withdraw it */

    def await_state(reason: String)(ready: Document.State => Boolean): Document.State =
      server.await_state(session, deadline, reason)(ready)

    def get_state(): Document.State =
      await_state("document state not readable within the budget")(_ => true)

    // a version of ours has been assigned: the stable tip is no longer the one recorded
    // before session.update, and not Version.init's id either (a dropped Raw_Edits --
    // prover undefined -- leaves the old tip in place, so the ids must be compared)
    def assigned_after(tip_before: Option[Document_ID.Version])(st: Document.State): Boolean =
      st.stable_tip_version match {
        case Some(v) => v.id != Document_ID.none && !tip_before.contains(v.id)
        case None => false
      }

    def state_bits(st: Document.State): String =
      " (removing_versions = " + st.removing_versions + ")"

    def after_update(): Unit = {
      // a stable retraction is a fixpoint of the next flush; files still carrying unflushed
      // edits stayed in pending_input and need theirs
      editor.revoke()
      if (resources.has_pending_input(deadline)) editor.invoke()
    }

    /* stanch + probe, off one reading of the document state */

    val st0 = get_state()
    val cancel_ids = st0.execs.keys.toList
    val v0 =
      Exn.capture(st0.recent_stable.version.get_finished) match {
        case Exn.Res(v) => v
        case Exn.Exn(_) => fail("probe not performed: no stable version to read the eval execs from")
      }
    val assignment0 = st0.the_assignment(v0).check_finished
    // the reply version: advanced at each point where an assignment of ours was awaited
    var reply_version = v0.id
    // eval exec ids only (execs also holds print entries); ids, never Command objects
    val probe: Map[Document_ID.Exec, (Document.Node.Name, Document_ID.Command)] =
      (for {
        (name, node) <- v0.nodes.iterator
        (command, _) <- node.command_iterator()
        exec_id <- assignment0.command_execs.getOrElse(command.id, Nil).headOption
      } yield exec_id -> (name, command.id)).toMap

    val serial = cancel_serial().toString
    val report_promise = cancel_handler.register(serial)
    val pong_promise = prelude_handler.register(serial)
    try {
      check_ready()  // protocol_command is dropped on the floor when the prover is not defined
      session.protocol_command("Isabelle_MCP.cancel_evaluation",
        XML.string(serial), XML.string(cancel_ids.mkString(",")),
        XML.string(probe.keys.mkString(",")))
      session.protocol_command("Isabelle_MCP.ping", XML.string(serial))
      log("cancel_evaluation " + serial + ": " + cancel_ids.length + " execs, " +
        probe.size + " probed")

      var report: Option[Language_Server.Cancel_Report] = None
      while (report.isEmpty) {
        if (report_promise.is_finished) report = Some(report_promise.join)
        else if (pong_promise.is_finished) {
          fail("cancel report lost: the prover answered the ping sent after the cancel command" +
            " but never reported on the cancel")
        }
        else if (Time.now() >= deadline) {
          fail("prover did not acknowledge the stop within the budget" +
            " (backlog or dead: indistinguishable)")
        }
        else request_poll_step.sleep()
      }
      for (err <- report.get.error) fail("the prover failed while stopping: " + err)

      val active: Map[Document_ID.Exec, (Document.Node.Name, Document_ID.Command)] =
        (for (exec_id <- report.get.alive; target <- probe.get(exec_id)) yield exec_id -> target).toMap
      log("cancel_evaluation " + serial + ": " + report.get.alive.length + " alive after " +
        report.get.calls + " probe calls, " + active.size + " to retire")

      /* step 0: retract */

      val retraction = resources.cancel_retract(session, st0, deadline, cancel_update_bound)
      for (VSCode_Resources.Commit(tip_before) <- retraction.commit) {
        after_update()
        val st1 = await_state("retraction not assigned within the budget")(assigned_after(tip_before))
        for (v <- st1.stable_tip_version) reply_version = v.id
      }

      /* the retire loop */

      var retire_set = active
      var strikes = Map.empty[Document_ID.Exec, Text.Offset]   // offset of the failed round
      val retired_all = new mutable.ListBuffer[(VSCode_Resources.Retired, Document.Version)]
      val excluded_all = new mutable.ListBuffer[(VSCode_Resources.Excluded, Document.Version)]
      var stale_rounds = 0
      var flush_rounds = 0
      var retire_rounds = 0
      val execution_delay = options.seconds("editor_execution_delay")

      def counters: String =
        " (rounds: " + retire_rounds + " retire, " + flush_rounds + " flush, " +
        stale_rounds + " stale)"

      while (retire_set.nonEmpty) {
        if (Time.now() >= deadline) {
          fail("budget exhausted with " + retire_set.size + " commands still to retire" + counters)
        }
        val serial0 = resources.read_update_serial(deadline)
        val st = get_state()
        resources.cancel_round(session, retire_set, st, serial0, deadline, cancel_update_bound) match {
          case VSCode_Resources.Stale =>
            stale_rounds += 1
            if (Time.now() >= deadline) {
              fail("document state kept changing under the cancel" + counters + state_bits(st))
            }
            execution_delay.sleep()
          case VSCode_Resources.No_Stable_Tip =>
            if (Time.now() >= deadline) {
              fail("no stable tip within the budget" + counters + state_bits(st))
            }
            execution_delay.sleep()
          case VSCode_Resources.Flushed(VSCode_Resources.Commit(tip_before)) =>
            flush_rounds += 1
            after_update()
            val st1 =
              await_state("unflushed edits kept arriving" + counters + state_bits(st))(
                assigned_after(tip_before))
            for (v <- st1.stable_tip_version) reply_version = v.id
          case VSCode_Resources.Retire(version, retired, excluded, commit) =>
            for (x <- excluded) {
              excluded_all += ((x, version))
            }
            retire_set = retire_set.filter({ case (_, (name, command_id)) =>
              !excluded.exists(x => x.name == name && x.command_id == command_id) })
            for (VSCode_Resources.Commit(tip_before) <- commit) {
              retire_rounds += 1
              after_update()
              val st1 =
                await_state("assignment never arrived" + counters + state_bits(st))(
                  assigned_after(tip_before))
              val version1 = st1.stable_tip_version.get
              reply_version = version1.id
              for (r <- retired) {
                val still_there = version1.nodes(r.name).commands.exists(_.id == r.command.id)
                if (!still_there) {
                  retire_set -= r.exec_id
                  strikes -= r.exec_id
                  retired_all += ((r, version))
                }
                else {
                  strikes.get(r.exec_id) match {
                    case Some(previous_start) =>
                      fail("command " + r.command.id + " in " + r.name.node +
                        " kept its id after two retire rounds (offsets " + previous_start +
                        " and " + r.start + "): the version the edit was taken from was not" +
                        " the tip at commit, the edit missed the command, or the prover" +
                        " vanished mid-request" + counters)
                    case None => strikes += (r.exec_id -> r.start)
                  }
                }
              }
            }
            if (retire_set.nonEmpty) execution_delay.sleep()
        }
      }

      /* the post-retraction invariant, after the monitor was released for the last time */

      resources.cancel_assert_retracted(get_state(), retraction.overlay_serial, deadline)

      /* reply */

      val outcome = if (active.isEmpty) "nothing_running" else "retired"
      val retired_json =
        retired_all.toList.map({ case (r, v) => cancel_command_json(r.name, r.command, v) })
      val waived_json =
        retired_all.toList.filter(_._1.waived).map({ case (r, v) =>
          cancel_command_json(r.name, r.command, v) })
      // excluded entries carry no lines: the command may no longer exist on the current
      // version, so Python renders them by file (and keyword when known) only
      val excluded_json =
        excluded_all.toList.map({ case (x, v) =>
          (x.command match {
            case Some(command) => cancel_command_json(x.name, command, v) - "line" - "end_line"
            case None => JSON.Object("file" -> x.name.node, "id" -> x.command_id)
          }) + ("reason" -> x.reason) })
      // R11: everything from the first retired command of a node to its end is unloaded,
      // the ML files those commands load included; the earliest start over all rounds
      val unload_json =
        retired_all.toList.groupBy(_._1.name).toList.map({ case (name, rs) =>
          val (first, v) = rs.minBy(_._1.start)
          val loads =
            v.nodes(name).command_iterator(first.start).flatMap(_._1.blobs_names).map(_.node)
              .toList.distinct
          JSON.Object("file" -> name.node, "loads" -> loads) ++
          (v.nodes(name).command_start_line(first.command) match {
            case Some(line) => JSON.Object("line" -> line)
            case None => JSON.Object()
          })
        })

      JSON.Object(
        "outcome" -> outcome,
        "document_version" -> reply_version,
        "retired" -> retired_json,
        "excluded" -> excluded_json,
        "waived" -> waived_json,
        "unloaded_from" -> unload_json)
    }
    finally {
      cancel_handler.forget(serial)
      prelude_handler.forget(serial)
    }
  }

  /* the load commands of a file (R-D3 (iv)): a .ML file is never an evaluation target, so
     the refusals and the breakpoint tools point at the ML_file command that loads it.
     Nodes.commands_loading (document.scala) knows a loader only once the loading theory is
     in the document model; early in a session the list is empty and the caller falls back
     to the generic sentence. */

  def loaders(id: LSP.Id, file: JFile): Unit = {
    val name = resources.node_name(file)
    // The commands come from the stable version, the line from the model's CURRENT text:
    // the version offset is converted through every edit the version does not have yet
    // (flushed-but-unassigned history edits and the models' unflushed ones), the way
    // Snapshot.commands_loading_ranges does.  Without the conversion an edit above the
    // load command reports a stale line (and an offset past the text length throws).
    val snapshot =
      session.snapshot(pending_edits = Document.Pending_Edits.make(resources.get_models()))
    val version = snapshot.version
    val result =
      for {
        command <- version.nodes.commands_loading(name)
        (node_name, node) <- version.nodes.iterator.find(_._2.commands.contains(command)).toList
        start <- node.command_start(command).toList
      } yield {
        val status = snapshot.state.command_status(version, command)
        val state =
          if (status.is_running) "running"
          else if (status.is_finished || status.is_failed) "evaluated"
          else "unevaluated"
        JSON.Object(
          "file" -> node_name.node,
          "theory" -> node_name.theory,
          "command" -> command.span.name,
          "state" -> state) ++
        (resources.get_model(node_name) match {
          case Some(model) =>
            val offset = snapshot.switch(node_name).convert(start)
            JSON.Object("line" -> (model.content.doc.position(offset).line + 1))
          case None => JSON.Object()
        })
      }
    channel.write(LSP.Loaders.reply(id, result))
  }

  def command_at_position(id: LSP.Id, node_pos: Line.Node_Position): Unit = {
    val result =
      rendering_offset(node_pos) match {
        case Some((rendering, offset)) =>
          val it = rendering.snapshot.node.command_iterator(offset)
          if (it.hasNext) {
            val (command, start) = it.next()
            if (command.is_ignored) None
            else {
              val text_range = Text.Range(start, start + command.length)
              Some((Symbol.decode(command.source), rendering.model.content.doc.range(text_range)))
            }
          }
          else None
        case None => None
      }
    channel.write(LSP.Command_At_Position.reply(id, result))
  }

  def output_at_position(id: LSP.Id, node_pos: Line.Node_Position): Unit = {
    val result =
      rendering_offset(node_pos) match {
        case Some((rendering, offset)) =>
          val snapshot = rendering.snapshot
          val it = snapshot.node.command_iterator(offset)
          if (it.hasNext) {
            val (command, start) = it.next()
            if (command.is_ignored) None
            else {
              val text_range = Text.Range(start, start + command.length)
              val range = rendering.model.content.doc.range(text_range)
              val output_state = resources.options.bool("editor_output_state")
              // Rendering.output_messages was removed in 2025-2; inline its body.
              val results = snapshot.command_results(command)
              val (states, other) =
                results.iterator.map(_._2).filterNot(Protocol.is_result).toList
                  .partition(Protocol.is_state)
              val output = (if (output_state) states else Nil) ::: other
              val node_context =
                new Browser_Info.Node_Context {
                  override def make_ref(props: Properties.T, body: XML.Body): Option[XML.Elem] =
                    for {
                      thy_file <- Position.Def_File.unapply(props)
                      def_line <- Position.Def_Line.unapply(props)
                      // source_file moved Resources -> Store in 2025-2 (returns a platform path).
                      platform_path <- session.store.source_file(thy_file)
                      uri = File.uri(Path.explode(File.standard_path(platform_path)).absolute_file)
                    } yield HTML.link(uri.toString + "#" + def_line, body)
                }
              val elements = Browser_Info.extra_elements.copy(entity = Markup.Elements.full)
              val html =
                node_context.make_html(elements,
                  rendering.resolve_here_positions(Pretty.separate(output)))
              Some((Symbol.decode(command.source), range, HTML.source(html).toString))
            }
          }
          else None
        case None => None
      }
    channel.write(LSP.Output_At_Position.reply(id, result))
  }

  // Render XML to browser HTML, the same way output_at_position renders a command's
  // results, so the client can reuse the same HTML parsing. Lives here (not in
  // VSCode_Find_Theorems) for access to `session`/`store`.
  def render_html(body: XML.Body): String = {
    val node_context =
      new Browser_Info.Node_Context {
        override def make_ref(props: Properties.T, body: XML.Body): Option[XML.Elem] =
          for {
            thy_file <- Position.Def_File.unapply(props)
            def_line <- Position.Def_Line.unapply(props)
            platform_path <- session.store.source_file(thy_file)
            uri = File.uri(Path.explode(File.standard_path(platform_path)).absolute_file)
          } yield HTML.link(uri.toString + "#" + def_line, body)
      }
    val elements = Browser_Info.extra_elements.copy(entity = Markup.Elements.full)
    HTML.source(node_context.make_html(elements, body)).toString
  }

  // Query-operation output (e.g. find_theorems) is separated but NOT formatted: its
  // own layout is already in the strings, and a second Pretty pass rewraps the item
  // list into something the client's parser does not recognise.
  def render_query_html(messages: XML.Body): String = render_html(Pretty.separate(messages))


  /* position-explicit queries (docs/archive/QUERY_TOOLS_UPGRADE.md section 5)

     These handlers must not block.  The main loop below reads one message, handles it
     inline, and only then reads the next, so waiting for the prover here would queue
     every later request behind this one -- including the theory_status that the client's
     evaluation poll depends on, and cancel_evaluation.  And these queries are meant to be
     usable DURING an evaluation, so that collision is the normal case, not an edge one.
     So: resolve, register, send, return.  The response is written later, from the
     protocol handler's callback or from the timer, whichever takes the request first. */

  private[mcp] val query_handler = new Query_Handler

  private def query_command(node_pos: Line.Node_Position): Option[(String, String)] =
    for {
      (rendering, offset) <- rendering_offset(node_pos)
      command <- rendering.snapshot.current_command(rendering.model.node_name, offset)
    } yield Language_Server.command_ref(command)

  private def query_messages(result: Query.Result): XML.Body =
    for (case XML.Elem(markup, body) <- Symbol.decode_yxml_failsafe(result.text))
      yield Protocol.make_message(body, markup.name)

  private def query_at_position(
    id: LSP.Id,
    params: LSP.Query_Params,
    command: String,
    content: Query.Result => String
  ): Unit = {
    def respond(result: Query.Result): Unit =
      channel.write(
        LSP.query_reply(id, result.status, result.comment, result.forked,
          if (result.status == Query.OK) content(result) else result.text))

    query_command(params.node_pos) match {
      case None => respond(Query.Result(Query.NO_COMMAND))
      case Some((node_name, command_id)) =>
        val token = params.token
        val timer =
          Event_Timer.request(Time.now() + Time.seconds(params.timeout)) {
            for (respond_timeout <- query_handler.take(token)) {
              session.protocol_command("Isabelle_MCP.cancel_query", XML.string(token))
              respond_timeout(Query.Result(Query.TIMEOUT))
            }
          }
        query_handler.register(token, result => { timer.cancel(); respond(result) })
        session.protocol_command_args(command,
          (token :: node_name :: command_id :: params.args).map(XML.string))
    }
  }

  def proof_state_at_position(id: LSP.Id, params: LSP.Query_Params): Unit =
    query_at_position(id, params, "Isabelle_MCP.proof_state", result =>
      render_html(
        Pretty.formatted(Pretty.separate(query_messages(result)),
          margin = resources.message_margin, metric = Symbol.Metric)))

  def find_theorems_at_position(id: LSP.Id, params: LSP.Query_Params): Unit =
    query_at_position(id, params, "Isabelle_MCP.find_theorems", result =>
      render_query_html(query_messages(result)))

  /* A cancel for an unknown or already-finished token is a silent no-op on both sides.
     For one still in flight, taking the entry here is what stops the prover's own
     "cancelled" reply from writing a second response later -- so this must ANSWER the
     request it just took, or that request never gets a response at all. The client
     sends its cancel after its request has returned, so in practice the entry is
     already gone; a client that cancels a live query is what this branch is for. */
  def query_cancel(token: String): Unit = {
    for (respond <- query_handler.take(token)) respond(Query.Result(Query.CANCELLED))
    session.protocol_command("Isabelle_MCP.cancel_query", XML.string(token))
  }


  /* the commands overlapping each of several lines of one file

     Walking the file with repeated PIDE/command_at_position cannot do this: the gap
     between any two commands is its own ignored span, that request answers None for an
     ignored command, and None carries no range, so the walk stalls at every boundary. */

  def commands_at_lines(id: LSP.Id, file: JFile, lines: List[Int]): Unit = {
    val result =
      for (rendering <- resources.get_rendering(file)) yield {
        val doc = rendering.model.content.doc
        val node = rendering.snapshot.node
        for (line <- lines) yield {
          val commands =
            (for {
              text <- doc.lines.lift(line).map(_.text)
              start <- doc.offset(Line.Position(line))
            } yield {
              val stop = start + text.length
              node.command_iterator(start)
                .takeWhile({ case (_, command_start) => command_start < stop })
                .collect({
                  case (command, command_start) if !command.is_ignored =>
                    val range = Text.Range(command_start, command_start + command.length)
                    (doc.range(range), Symbol.decode(command.source))
                })
                .toList
            }) getOrElse Nil
          (line, commands)
        }
      }
    channel.write(LSP.Commands_At_Lines.reply(id, result))
  }

  def symbols(id: LSP.Id): Unit = {
    val content = Symbol.Symbols.files().map(File.read).mkString("\n")
    channel.write(LSP.Symbols.reply(id, content))
  }


  /* main loop */

  def start(): Unit = {
    log("Server started " + Date.now())

    def handle(json: JSON.T): Unit = {
      try {
        json match {
          case LSP.Initialize(id) => init(id)
          case LSP.Initialized() =>
          case LSP.Shutdown(id) => shutdown(id)
          case LSP.Exit() => exit()
          case LSP.DidOpenTextDocument(file, _, version, text) =>
            change_document(file, version, List(LSP.TextDocumentChange(None, text)))
            delay_load.invoke()
          case LSP.DidChangeTextDocument(file, version, changes) =>
            change_document(file, version, changes)
          case LSP.DidCloseTextDocument(file) => close_document(file)
          case LSP.Completion(id, node_pos) => completion(id, node_pos)
          case LSP.Include_Word() => update_dictionary(true, false)
          case LSP.Include_Word_Permanently() => update_dictionary(true, true)
          case LSP.Exclude_Word() => update_dictionary(false, false)
          case LSP.Exclude_Word_Permanently() => update_dictionary(false, true)
          case LSP.Reset_Words() => reset_dictionary()
          case LSP.Hover(id, node_pos) => hover(id, node_pos)
          case LSP.GotoDefinition(id, node_pos) => goto_definition(id, node_pos)
          case LSP.DocumentHighlights(id, node_pos) => document_highlights(id, node_pos)
          case LSP.CodeActionRequest(id, file, range) => code_action_request(id, file, range)
          case LSP.Decoration_Request(file) => decoration_request(file)
          case LSP.Caret_Update(caret) => update_caret(caret)
          case LSP.Output_Set_Margin(margin) => dynamic_output.set_margin(margin)
          case LSP.State_Init(id) => State_Panel.init(id, server)
          case LSP.State_Exit(state_id) => State_Panel.exit(state_id)
          case LSP.State_Locate(state_id) => State_Panel.locate(state_id)
          case LSP.State_Update(state_id) => State_Panel.update(state_id)
          case LSP.State_Auto_Update(state_id, enabled) =>
            State_Panel.auto_update(state_id, enabled)
          case LSP.State_Set_Margin(state_id, margin) => State_Panel.set_margin(state_id, margin)
          case LSP.Preview_Request(file, column) => preview_request(file, column)
          case LSP.Abbrevs_Request() => abbrevs_request()
          case LSP.Documentation_Request() => documentation_request()
          case LSP.Sledgehammer_Provers_Request() => sledgehammer.provers()
          case LSP.Sledgehammer_Request(args) => sledgehammer.request(args)
          case LSP.Sledgehammer_Cancel() => sledgehammer.cancel()
          case LSP.Sledgehammer_Locate() => sledgehammer.locate()
          case LSP.Sledgehammer_Sendback(text) => sledgehammer.sendback(text)
          case LSP.Theory_Status(id) => theory_status(id)
          case LSP.Cancel_Evaluation(id) => cancel_evaluation(id)
          case LSP.Flush(id, resync_dependencies) => flush(id, resync_dependencies)
          case LSP.Loaders(id, file) => loaders(id, file)
          case LSP.Command_At_Position(id, node_pos) => command_at_position(id, node_pos)
          case LSP.Output_At_Position(id, node_pos) => output_at_position(id, node_pos)
          case LSP.Symbols(id) => symbols(id)
          case LSP.Find_Theorems_Request(token, args) => find_theorems.request(token, args)
          case LSP.Find_Theorems_Cancel(token) => find_theorems.cancel(token)
          case LSP.Proof_State_At_Position(id, params) => proof_state_at_position(id, params)
          case LSP.Find_Theorems_At_Position(id, params) => find_theorems_at_position(id, params)
          case LSP.Query_Cancel(token) => query_cancel(token)
          case LSP.Commands_At_Lines(id, file, lines) => commands_at_lines(id, file, lines)
          case LSP.Debugger_Breakpoints(id, file, range, token, timeout) =>
            debugger_adapter.breakpoints(id, file, range, token, timeout)
          case LSP.Debugger_Toggle_Breakpoint(id, file, serial, state, token, timeout) =>
            debugger_adapter.toggle_breakpoint(id, file, serial, state, token, timeout)
          case LSP.Debugger_Eval(id, params) => debugger_adapter.eval(id, params)
          case LSP.Debugger_Print_Vals(id, params) => debugger_adapter.print_vals(id, params)
          case LSP.Debugger_Abort(id, thread, token) => debugger_adapter.abort(id, thread, token)
          case LSP.Debugger_Input(id, thread, verbs) => debugger_adapter.input(id, thread, verbs)
          case _ => if (!LSP.ResponseMessage.is_empty(json)) log("### IGNORED")
        }
      }
      catch { case exn: Throwable => channel.log_error_message(Exn.message(exn)) }
    }

    @tailrec def loop(): Unit = {
      channel.read() match {
        case Some(json) =>
          json match {
            case bulk: List[_] => bulk.foreach(handle)
            case _ => handle(json)
          }
          loop()
        case None => log("### TERMINATE")
      }
    }
    loop()
  }
}


class VSCode_Find_Theorems(server: Language_Server) {
  private val query_operation =
    new Query_Operation(server.editor, (), "find_theorems", consume_status, consume_output)

  // The token of the in-flight query, echoed back in every status/output notification
  // so the client can drop stragglers from a superseded query. Only ever read/written
  // on the editor dispatcher thread (request, and the Query_Operation callbacks), so a
  // plain var is safe.
  private var current_token: String = ""

  private def consume_status(status: Query_Operation.Status): Unit =
    server.channel.write(LSP.Find_Theorems_Status(current_token, status.toString))

  private def consume_output(output: Editor.Output): Unit = {
    // apply_query emits an empty init output before the command match; send it as ""
    // so the client's "non-empty ⇒ real result" filter treats a no-command query as
    // producing no output. A genuine "found nothing" result has non-empty messages.
    val content =
      if (output.messages.isEmpty) "" else server.render_query_html(output.messages)
    server.channel.write(LSP.Find_Theorems_Output(current_token, content))
  }

  def request(token: String, args: List[String]): Unit =
    server.editor.send_dispatcher { current_token = token; query_operation.apply_query(args) }

  // Token-guarded: ignore a cancel for a query that has already been superseded, so a
  // late cancel from a finished query cannot tear down the next one.
  def cancel(token: String): Unit =
    server.editor.send_dispatcher {
      if (token == current_token) query_operation.cancel_query()
    }

  def init(): Unit = query_operation.activate()
  def exit(): Unit = query_operation.deactivate()
}
