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
  /* proof that the injected ML prelude is live

     An undefined protocol command is NOT fatal in ML -- the protocol loop downgrades it to a
     system message and carries on (isabelle_process.ML) -- so without this probe a prover
     without the prelude would happily accept every cancel request and cancel nothing.
     Isabelle_MCP.ping answers with an isabelle_mcp_pong protocol message. */

  class Prelude_Handler extends Session.Protocol_Handler {
    private val pong = Future.promise[String]

    private def handle_pong(msg: Prover.Protocol_Output): Boolean = {
      if (!pong.is_finished) pong.fulfill(msg.text)
      true
    }

    override def functions: Session.Protocol_Functions =
      List("isabelle_mcp_pong" -> handle_pong)

    def await_pong(timeout: Time): Boolean = {
      val step = Time.seconds(0.05)
      var waited = Time.zero
      while (!pong.is_finished && waited < timeout) {
        step.sleep()
        waited += step
      }
      pong.is_finished
    }
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

  def rendering_offset(node_pos: Line.Node_Position): Option[(VSCode_Rendering, Text.Offset)] =
    for {
      rendering <- resources.get_rendering(new JFile(node_pos.name))
      offset <- rendering.model.content.doc.offset(node_pos.pos)
    } yield (rendering, offset)

  private val dynamic_output = Dynamic_Output(server)


  /* input from client or file-system */

  private val file_watcher: File_Watcher =
    File_Watcher(sync_documents, options.seconds("vscode_load_delay"))

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

  private val delay_output: Delay =
    Delay.last(options.seconds("vscode_output_delay"), channel.Error_Logger) {
      if (resources.flush_output(channel)) delay_output.invoke()
    }

  def update_output(changed_nodes: Iterable[JFile]): Unit = {
    resources.update_output(changed_nodes)
    delay_output.invoke()
  }

  def update_output_visible(): Unit = {
    resources.update_output_visible()
    delay_output.invoke()
  }

  private val prover_output =
    Session.Consumer[Session.Commands_Changed](getClass.getName) {
      case changed =>
        update_output(changed.nodes.toList.map(resources.node_file(_)))
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

      val prelude_handler = new Language_Server.Prelude_Handler
      session.init_protocol_handler(prelude_handler)
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
        if (!prelude_handler.await_pong(Time.seconds(10))) {
          error("The ML prelude did not answer: cancellation would silently do nothing." +
            "\nPrelude: " + prelude + startup_details)
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
      update_output_visible()
    }
  }

  def reset_dictionary(): Unit = {
    for (spell_checker <- resources.spell_checker.get) {
      spell_checker.reset()
      update_output_visible()
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


  /* theory status */

  def theory_status(id: LSP.Id): Unit = {
    val now = Date.now()
    val theories =
      (for (model <- resources.get_models().iterator) yield {
        val snapshot = resources.snapshot(model)
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
    channel.write(LSP.Theory_Status.reply(id, theories))
  }

  /* Global cancel, on a stock Isabelle.

     "Isabelle_MCP.cancel_execution" is defined by ML/mcp_prelude.ML, injected into the prover
     at startup, and is built from the public EXECUTION API alone: discontinue the current
     execution (so nothing not yet started ever starts), then cancel the Future groups of every
     exec Scala knows about.  Document.State.execs spans ALL nodes, which is what lets this
     reach a runaway proof in an imported theory -- the perspective-restriction fallback cannot.

     The prelude is a hard startup dependency: `init` below pings it and refuses to serve if it
     is not there, so this cannot silently report a cancellation that never happened.

     See docs/CANCELLATION.md for the design, the validation, and the stale-mirror analysis.

     The alternative, used by vscode_server, is the pide_control ML patch, which adds a
     "Document.cancel_execution" command that walks Execution's own private exec table.  It is
     authoritative by construction, but applying the patch invalidates every session heap on the
     machine.  Kept here for reference:

       session.protocol_command("Document.cancel_execution")
  */
  def cancel_execution(id: LSP.Id): Unit = {
    val execs = session.get_state().execs.keys.toList
    session.protocol_command("Isabelle_MCP.cancel_execution",
      List(XML.Text(execs.mkString(","))))
    log("cancel_execution: " + execs.length + " execs")
    channel.write(LSP.Cancel_Execution.reply(id))
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
              val html = node_context.make_html(elements, Pretty.separate(output))
              Some((Symbol.decode(command.source), range, HTML.source(html).toString))
            }
          }
          else None
        case None => None
      }
    channel.write(LSP.Output_At_Position.reply(id, result))
  }

  // Render query-operation output (e.g. find_theorems) to browser HTML, the same
  // way output_at_position renders a command's results, so the client can reuse the
  // same HTML parsing. Lives here (not in VSCode_Find_Theorems) for access to
  // `session`/`store`.
  def render_query_html(messages: XML.Body): String = {
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
    HTML.source(node_context.make_html(elements, Pretty.separate(messages))).toString
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
          case LSP.Cancel_Execution(id) => cancel_execution(id)
          case LSP.Command_At_Position(id, node_pos) => command_at_position(id, node_pos)
          case LSP.Output_At_Position(id, node_pos) => output_at_position(id, node_pos)
          case LSP.Symbols(id) => symbols(id)
          case LSP.Find_Theorems_Request(token, args) => find_theorems.request(token, args)
          case LSP.Find_Theorems_Cancel(token) => find_theorems.cancel(token)
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
