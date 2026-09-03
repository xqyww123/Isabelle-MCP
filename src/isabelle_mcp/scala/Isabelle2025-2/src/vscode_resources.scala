/*  Title:      Tools/VSCode/src/vscode_resources.scala
    Author:     Makarius

Resources for VSCode Language Server: file-system access and global state.
*/

package isabelle.mcp


import isabelle._

import java.io.{File => JFile}

import java.util.{List => JList}
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import java.nio.file.FileSystems
import java.nio.file.{WatchKey, WatchEvent, Path => JPath}
import java.nio.file.StandardWatchEventKinds.{ENTRY_CREATE, ENTRY_DELETE, ENTRY_MODIFY}

import scala.collection.mutable
import scala.jdk.CollectionConverters._
import scala.util.parsing.input.Reader


/* File_Watcher with a trigger filter (fork-local)

   The distribution's File_Watcher cannot be subclassed -- its primary constructor is
   private[File_Watcher] (Pure/General/file_watcher.scala:19) -- so this is an independent
   class in package isabelle.mcp; Pure is untouched.

   It differs from the distribution in exactly one respect: which batches restart the
   debounce timer.  The single Delay.last is restarted by *every* event, so any file being
   written at a sub-delay rate anywhere under a watched directory postpones disk pickup
   indefinitely -- measured: a dependency edit never landed while a log file was being
   appended to.  A batch now restarts the timer only if it mentions a .thy/.ML/.sml file
   (nobody writes those at that rate) or a file the prover actually holds (the registered
   path table below).

   IRON RULE: the filter changes the trigger boolean ONLY.  Every event is still accumulated
   into st.changed exactly as upstream does it -- moving the filter into the accumulation
   would turn "delayed" into "lost for good". */

class MCP_File_Watcher private[MCP_File_Watcher] {
  // dummy template
  def register(dir: JFile): Unit = {}
  def register_parent(file: JFile): Unit = {}
  def deregister(dir: JFile): Unit = {}
  def purge(retain: Set[JFile]): Unit = {}
  def shutdown(): Unit = {}
}

object MCP_File_Watcher {
  val none: MCP_File_Watcher = new MCP_File_Watcher {
    override def toString: String = "MCP_File_Watcher.none"
  }

  def apply(handle: Set[JFile] => Unit, delay: => Time = Time.seconds(0.5)): MCP_File_Watcher =
    if (Platform.is_windows) none else new Impl(handle, delay)

  private val trigger_extensions = List(".thy", ".ml", ".sml")

  private def trigger_extension(name: String): Boolean = {
    val lower = Word.lowercase(name)
    trigger_extensions.exists(lower.endsWith)
  }


  /* proper implementation */

  sealed case class State(
    dirs: Map[JFile, WatchKey] = Map.empty,
    /* the registered path table: the plain file names register_parent was asked about,
       grouped by the directory object it computed.  Both sides of the lookup use that same
       directory object and the bare event.context name -- NO canonicalization anywhere
       (measured: canonicalizing pulls the two spellings apart again).  Grows only: models
       are never dropped and purge has no caller. */
    registered: Map[JFile, Set[String]] = Map.empty,
    changed: Set[JFile] = Set.empty)

  class Impl private[MCP_File_Watcher](handle: Set[JFile] => Unit, delay: Time)
  extends MCP_File_Watcher {
    private val state = Synchronized(MCP_File_Watcher.State())
    private val watcher = FileSystems.getDefault.newWatchService()

    override def toString: String =
      state.value.dirs.keySet.mkString("MCP_File_Watcher(", ", ", ")")


    /* registered directories */

    override def register(dir: JFile): Unit =
      state.change(st =>
        st.dirs.get(dir) match {
          case Some(key) if key.isValid => st
          case _ =>
            val key = dir.toPath.register(watcher, ENTRY_CREATE, ENTRY_DELETE, ENTRY_MODIFY)
            st.copy(dirs = st.dirs + (dir -> key))
        })

    override def register_parent(file: JFile): Unit = {
      val dir = file.getParentFile
      if (dir != null && dir.isDirectory) {
        // record the file identity FIRST: register(dir) short-circuits on an existing
        // WatchKey, and the table must not be skipped along with it.
        state.change(st =>
          st.copy(registered =
            st.registered + (dir -> (st.registered.getOrElse(dir, Set.empty) + file.getName))))
        register(dir)
      }
    }

    override def deregister(dir: JFile): Unit =
      state.change(st =>
        st.dirs.get(dir) match {
          case None => st
          case Some(key) =>
            key.cancel()
            st.copy(dirs = st.dirs - dir)
        })

    override def purge(retain: Set[JFile]): Unit =
      state.change(st =>
        st.copy(dirs = st.dirs --
          (for ((dir, key) <- st.dirs.iterator if !retain(dir)) yield { key.cancel(); dir })))


    /* changed directory entries */

    private val delay_changed = Delay.last(delay) {
      val changed = state.change_result(st => (st.changed, st.copy(changed = Set.empty)))
      handle(changed)
    }

    private val watcher_thread = Isabelle_Thread.fork(name = "file_watcher", daemon = true) {
      try {
        while (true) {
          val key = watcher.take
          val trigger =
            state.change_result { st =>
              val (remove, changed, trigger) =
                st.dirs.collectFirst({ case (dir, key1) if key == key1 => dir }) match {
                  case Some(dir) =>
                    val events: Iterable[WatchEvent[JPath]] =
                      key.pollEvents.asInstanceOf[JList[WatchEvent[JPath]]].asScala
                    val remove = if (key.reset) None else Some(dir)
                    val changed =
                      events.iterator.foldLeft(Set.empty[JFile]) {
                        case (set, event) => set + dir.toPath.resolve(event.context).toFile
                      }
                    val registered = st.registered.getOrElse(dir, Set.empty)
                    val trigger =
                      events.iterator.map(_.context.toString).exists(name =>
                        trigger_extension(name) || registered(name))
                    (remove, changed, trigger)
                  case None =>
                    key.pollEvents
                    key.reset
                    (None, Set.empty[JFile], false)
                }
              (changed.nonEmpty && trigger,
                st.copy(dirs = st.dirs -- remove, changed = st.changed ++ changed))
            }
          if (trigger) delay_changed.invoke()
        }
      }
      catch { case Exn.Interrupt() => }
    }


    /* shutdown */

    override def shutdown(): Unit = {
      watcher_thread.interrupt()
      watcher_thread.join()
      delay_changed.revoke()
    }
  }
}


object VSCode_Resources {
  /* internal state */

  sealed case class State(
    models: Map[JFile, VSCode_Model] = Map.empty,
    caret: Option[(JFile, Line.Position)] = None,
    overlays: Document.Overlays = Document.Overlays.empty,
    pending_input: Set[JFile] = Set.empty,
    pending_output: Set[JFile] = Set.empty,
    /* counts the session.update calls made under the monitor with a non-empty edit list:
       the cancel request reads the document state outside the monitor and uses this to
       tell whether that reading is still current once it is back inside */
    update_serial: Long = 0,
    /* counts the changes to the overlay table.  Two writers: change_overlay bumps this
       when the table really changed; the cancel request's step 0 empties the table and
       records the value, and asserts at the end that nobody touched overlays since */
    overlay_serial: Long = 0
  ) {
    def update_models(changed: Iterable[(JFile, VSCode_Model)]): State =
      copy(
        models = models ++ changed,
        pending_input = changed.foldLeft(pending_input) { case (set, (file, _)) => set + file },
        pending_output = changed.foldLeft(pending_output) { case (set, (file, _)) => set + file })

    def update_caret(new_caret: Option[(JFile, Line.Position)]): State =
      if (caret == new_caret) this
      else
        copy(
          caret = new_caret,
          pending_input = pending_input ++ caret.map(_._1) ++ new_caret.map(_._1))

    def get_caret(file: JFile): Option[Line.Position] =
      caret match {
        case Some((caret_file, caret_pos)) if caret_file == file => Some(caret_pos)
        case _ => None
      }

    lazy val document_blobs: Document.Blobs =
      Document.Blobs(
        (for {
          (_, model) <- models.iterator
          blob <- model.get_blob
        } yield (model.node_name -> blob)).toMap)

    def change_overlay(insert: Boolean, file: JFile,
        command: Command, fn: String, args: List[String]): State = {
      val overlays1 =
        if (insert) overlays.insert(command, fn, args)
        else overlays.remove(command, fn, args)
      // Bumped only when the table really changed.  Document.Overlays always returns a
      // fresh object, so identity says nothing; and a query finishing after the cancel
      // request's step 0 emptied the table removes an entry that is already gone --
      // that must not read as "someone touched the overlays".
      val changed = overlays(command.node_name).dest != overlays1(command.node_name).dest
      copy(
        overlays = overlays1,
        overlay_serial = if (changed) overlay_serial + 1 else overlay_serial,
        pending_input = pending_input + file)
    }
  }


  /* cancellation (ISABELLE_MCP_CANCELLATION_REDESIGN_PLAN.md section 3.1.2)

     Every failure of the cancel request is a Cancel_Failure with the reason for the log;
     Language_Server turns any of them into the one catastrophic reply. */

  final class Cancel_Failure(val reason: String) extends RuntimeException(reason)
  def cancel_fail(reason: String): Nothing = throw new Cancel_Failure(reason)

  /* a command retired this round: the zero-length edit (or the net-zero pair) is in the
     batch; "waived" marks a first-of-file single-character command, whose net-zero pair
     also re-mints the commands the same re-parse sweeps up (R8) */
  sealed case class Retired(
    exec_id: Document_ID.Exec,
    name: Document.Node.Name,
    command: Command,
    start: Text.Offset,
    waived: Boolean)

  /* a target dropped from the retire set without an edit: "gone" (its exec is no longer in
     any retained version) or "reassigned" (its command already has another exec on V, or
     the command id is no longer in V's node at all -- a real edit re-minted it) */
  sealed case class Excluded(
    name: Document.Node.Name,
    command_id: Document_ID.Command,
    command: Option[Command],
    reason: String)

  /* step 0's result: the overlay serial recorded after emptying the table, and the
     commit if a retraction batch was sent */
  sealed case class Retraction(overlay_serial: Long, commit: Option[Commit])

  /* a batch was sent (step 0 or step f): the stable tip's id before it, if any */
  sealed case class Commit(tip_before: Option[Document_ID.Version])

  /* one round of the retire loop, as seen from outside the monitor */
  sealed abstract class Round
  case object Stale extends Round                       // step a: the state reading aged; retake
  case object No_Stable_Tip extends Round               // step c: wait a beat and retake
  case class Flushed(commit: Commit) extends Round      // step b: unflushed edits went out alone
  case class Retire(
    version: Document.Version,
    retired: List[Retired],
    excluded: List[Excluded],
    commit: Option[Commit])                             // step f: None if there was nothing to send
  extends Round

  /* The emptiness IS the point: Text.Edit.inserts/removes/replace all filter empty text,
     so this reaches for the raw constructor deliberately.  Byte-identical source, fresh
     command id.  A command of source length 1 has no interior, so it gets the net-zero
     pair instead: remove its source and insert it back at the same offset, in this order
     and adjacent in the batch (F20). */
  def zero_length_edit(offset: Text.Offset): Text.Edit = Text.Edit.insert(offset, "")
  def net_zero_edits(start: Text.Offset, source: String): List[Text.Edit] =
    List(Text.Edit.remove(start, source), Text.Edit.insert(start, source))

  /* the monitor: a re-entrant lock whose acquisition can be bounded by a deadline

     Synchronized would do for everyone but the cancel request, which may not wait
     unboundedly on anything (R-D9).  Re-entrant because node_perspective reads
     visible_node -> get_model -> value while the cancel request holds the lock.  No
     condition waiting: nobody uses timed_access/guarded_access on this state. */

  final class Monitor[A](init: A) {
    private val lock = new ReentrantLock
    private var state: A = init

    private def locked[B](body: => B): B = { lock.lock(); try { body } finally { lock.unlock() } }

    def value: A = locked(state)
    def change(f: A => A): Unit = locked { state = f(state) }
    def change_result[B](f: A => (B, A)): B =
      locked { val (result, state1) = f(state); state = state1; result }

    def change_result_timed[B](deadline: Time)(f: A => (B, A)): B = {
      val acquired =
        try { lock.tryLock((deadline - Time.now()).ms max 0L, TimeUnit.MILLISECONDS) }
        catch {
          case _: InterruptedException =>
            cancel_fail("interrupted while waiting for the document model monitor")
        }
      if (!acquired) {
        cancel_fail("document model monitor held by another party for the rest of the budget")
      }
      try { val (result, state1) = f(state); state = state1; result }
      finally { lock.unlock() }
    }
  }

  /* session.update from inside the monitor: a short fixed bound, not the remaining budget,
     because while the monitor is held the whole server is pinned.  A miss abandons the
     state assignment with the exception; the Raw_Edits stays in the manager's mailbox and
     lands whenever the manager gets to it -- abandoning the wait is not withdrawing it. */

  def bounded_update(
    session: VSCode_Session,
    blobs: Document.Blobs,
    edits: List[Document.Edit_Text],
    bound: Time
  ): Unit = {
    val sent = Future.promise[Unit]
    Isabelle_Thread.fork(name = "cancel_update", daemon = true) {
      try { session.update(blobs, edits); sent.fulfill(()) }
      catch { case exn: Throwable => sent.fulfill_result(Exn.Exn(exn)) }
    }
    if (Language_Server.await_promise(sent, Time.now() + bound).isEmpty) {
      cancel_fail("session.update not acknowledged within " + bound)
    }
  }


  /* caret */

  sealed case class Caret(file: JFile, model: VSCode_Model, offset: Text.Offset) {
    def node_name: Document.Node.Name = model.node_name
  }
}

class VSCode_Resources(
  val options: Options,
  session_background: Sessions.Background,
  log: Logger = new Logger)
extends Resources(session_background, log = log) {
  resources =>

  private val state = new VSCode_Resources.Monitor(VSCode_Resources.State())


  /* options */

  def pide_extensions: Boolean = options.bool("vscode_pide_extensions")
  def html_output: Boolean = options.bool("vscode_html_output")
  def tooltip_margin: Int = options.int("vscode_tooltip_margin")
  def message_margin: Int = options.int("vscode_message_margin")
  def output_delay: Time = options.seconds("vscode_output_delay")

  def unicode_symbols_output: Boolean = options.bool("vscode_unicode_symbols_output")
  def unicode_symbols_edits: Boolean = options.bool("vscode_unicode_symbols_edits")


  /* document node name */

  def node_file(name: Document.Node.Name): JFile = new JFile(name.node)

  def node_name(file: JFile): Document.Node.Name =
    find_theory(file) getOrElse {
      val node = file.getPath
      val theory = theory_name(Sessions.DRAFT, Thy_Header.theory_name(node))
      if (loaded_theory(theory)) Document.Node.Name.loaded_theory(theory)
      else Document.Node.Name(node, theory = theory)
    }

  override def migrate_name(standard_name: Document.Node.Name): Document.Node.Name =
    node_name(Path.explode(standard_name.node).canonical_file)

  override def append_path(prefix: String, source_path: Path): String = {
    val path = source_path.expand
    if (prefix == "" || path.is_absolute) File.platform_path(path)
    else if (path.is_current) prefix
    else if (path.is_basic && !prefix.endsWith("/") && !prefix.endsWith(JFile.separator))
      prefix + JFile.separator + File.platform_path(path)
    else if (path.is_basic) prefix + File.platform_path(path)
    else File.absolute(new JFile(prefix + JFile.separator + File.platform_path(path))).getPath
  }

  override def read_dir(dir: String): List[String] =
    File.read_dir(Path.explode(File.standard_path(dir)))

  def get_models(): Iterable[VSCode_Model] = state.value.models.values
  def get_model(file: JFile): Option[VSCode_Model] = state.value.models.get(file)
  def get_model(name: Document.Node.Name): Option[VSCode_Model] = get_model(node_file(name))


  /* snapshot */

  def snapshot(model: VSCode_Model): Document.Snapshot =
    model.session.snapshot(
      node_name = model.node_name,
      pending_edits = Document.Pending_Edits.make(get_models()))

  def get_snapshot(file: JFile): Option[Document.Snapshot] =
    get_model(file).map(snapshot)

  def get_snapshot(name: Document.Node.Name): Option[Document.Snapshot] =
    get_model(name).map(snapshot)


  /* rendering */

  def rendering(snapshot: Document.Snapshot, model: VSCode_Model): VSCode_Rendering =
    new VSCode_Rendering(snapshot, model)

  def rendering(model: VSCode_Model): VSCode_Rendering = rendering(snapshot(model), model)

  def get_rendering(file: JFile): Option[VSCode_Rendering] =
    get_model(file).map(rendering)

  def get_rendering(name: Document.Node.Name): Option[VSCode_Rendering] =
    get_model(name).map(rendering)


  /* file content */

  def read_file_content(name: Document.Node.Name): Option[String] = {
    make_theory_content(name) orElse {
      try { Some(Line.normalize(File.read(node_file(name)))) }
      catch { case ERROR(_) => None }
    }
  }

  def get_file_content(name: Document.Node.Name): Option[String] =
    get_model(name) match {
      case Some(model) => Some(model.content.text)
      case None => read_file_content(name)
    }

  override def with_thy_reader[A](name: Document.Node.Name, f: Reader[Char] => A): A = {
    val file = node_file(name)
    get_model(file) match {
      case Some(model) => f(Scan.char_reader(model.content.text))
      case None if file.isFile => using(Scan.byte_reader(file))(f)
      case None => error("No such file: " + quote(file.toString))
    }
  }


  /* document models */

  def visible_node(name: Document.Node.Name): Boolean =
    get_model(name) match {
      case Some(model) => model.node_visible
      case None => false
    }

  def change_model(
    session: VSCode_Session,
    editor: Language_Server.Editor,
    file: JFile,
    version: Long,
    text: String,
    range: Option[Line.Range] = None
  ): Unit = {
    state.change { st =>
      val model = st.models.getOrElse(file, VSCode_Model.init(session, editor, node_name(file)))
      // A model coming back from external_file (a reopen) must forget its published baseline:
      // the decoration publish is differential (vscode_model.scala:214-227), so for a clean
      // file whose content did not change it would send nothing at all and the client would
      // never see any decoration for the reopened file.  The clearing has to happen here, on
      // the reopen side -- on the close side it would suppress the erase push instead.
      val model0 =
        if (model.external_file) {
          model.copy(published_decorations = Nil, published_diagnostics = Nil)
        }
        else model
      val model1 =
        (model0.change_text(text, range) getOrElse model0).set_version(version).external(false)
      st.update_models(Some(file -> model1))
    }
  }

  def close_model(file: JFile): Boolean =
    state.change_result(st =>
      st.models.get(file) match {
        case None => (false, st)
        case Some(model) => (true, st.update_models(Some(file -> model.external(true))))
      })

  def sync_models(changed_files: Set[JFile]): Unit =
    state.change { st =>
      val changed_models =
        (for {
          (file, model) <- st.models.iterator
          if changed_files(file) && model.external_file
          text <- read_file_content(model.node_name)
          model1 <- model.change_text(text)
        } yield (file, model1)).toList
      st.update_models(changed_models)
    }


  /* overlays */

  def node_overlays(name: Document.Node.Name): Document.Node.Overlays =
    state.value.overlays(name)

  def insert_overlay(command: Command, fn: String, args: List[String]): Unit =
    state.change(_.change_overlay(true, node_file(command.node_name), command, fn, args))

  def remove_overlay(command: Command, fn: String, args: List[String]): Unit =
    state.change(_.change_overlay(false, node_file(command.node_name), command, fn, args))


  /* resolve dependencies */

  def resolve_dependencies(
    session: VSCode_Session,
    editor: Language_Server.Editor,
    file_watcher: MCP_File_Watcher
  ): (Boolean, Boolean) = {
    state.change_result { st =>
      val stable_tip_version = session.stable_tip_version(st.models.values)

      val thy_files =
        resources.resolve_dependencies(st.models.values, editor.document_required())

      val aux_files = stable_tip_version.toList.flatMap(undefined_blobs)

      val loaded_models =
        (for {
          node_name <- thy_files.iterator ++ aux_files.iterator
          file = node_file(node_name)
          if !st.models.isDefinedAt(file)
          text <- { file_watcher.register_parent(file); read_file_content(node_name) }
        }
        yield {
          val model = VSCode_Model.init(session, editor, node_name)
          val model1 = (model.change_text(text) getOrElse model).external(true)
          (file, model1)
        }).toList

      val invoke_input = loaded_models.nonEmpty
      val invoke_load = stable_tip_version.isEmpty

      ((invoke_input, invoke_load), st.update_models(loaded_models))
    }
  }


  /* pending input */

  def flush_input(session: VSCode_Session, channel: Channel): Unit = {
    state.change { st =>
      val changed_models =
        (for {
          file <- st.pending_input.iterator
          model <- st.models.get(file)
          (edits, model1) <-
            model.flush_edits(st.document_blobs, file, st.get_caret(file))
        } yield (edits, (file, model1))).toList

      val edits = changed_models.flatMap(_._1)
      session.update(st.document_blobs, edits)

      st.copy(
        models = st.models ++ changed_models.iterator.map(_._2),
        pending_input = Set.empty,
        update_serial = if (edits.nonEmpty) st.update_serial + 1 else st.update_serial)
    }
  }


  /* cancellation (ISABELLE_MCP_CANCELLATION_REDESIGN_PLAN.md section 3.1.2)

     Language_Server drives the request: budget, the prover round trips, the retire loop.
     What lives here is everything that touches the monitor.  Discipline while the cancel
     request holds it: no session round trip except session.update under a short fixed
     bound, no flush_input/flush_edits (they take the state through a round trip), every
     entry through change_result_timed.  The document state is read OUTSIDE the monitor
     (a manager round trip) and re-validated inside by update_serial. */

  def has_pending_input(deadline: Time): Boolean =
    state.change_result_timed(deadline)(st => (st.pending_input.nonEmpty, st))

  def read_update_serial(deadline: Time): Long =
    state.change_result_timed(deadline)(st => (st.update_serial, st))

  private def cancel_required(model: VSCode_Model): Boolean =
    model.node_required || model.editor.document_node_required(model.node_name)

  /* The perspective a model would compute now, caret gone, with the two catastrophe
     checks.  EVERY perspective the cancel request computes goes through here (step 0,
     step b, step e, the final assertion): a required theory cannot be retracted (required
     spreads through make_required to its imports), and Text.Perspective.full means a file
     loaded by a load command is an open visible model, which pins the loading theory's
     whole text (vscode_model.scala). */

  private def cancel_perspective(
    st: VSCode_Resources.State,
    doc_state: Document.State,
    pending_edits: Document.Pending_Edits,
    model: VSCode_Model,
    overlays: Document.Overlays
  ): Document.Node.Perspective_Text.T = {
    val (_, perspective) =
      model.node_perspective(
        doc_state.snapshot(node_name = model.node_name, pending_edits = pending_edits),
        st.document_blobs, None, overlays(model.node_name), cancel_required(model))
    if (perspective.required) {
      VSCode_Resources.cancel_fail("theory " + model.node_name + " is marked required")
    }
    if (perspective.visible == Text.Perspective.full) {
      VSCode_Resources.cancel_fail("load-command escape: a file loaded from " +
        model.node_name + " is an open visible model")
    }
    perspective
  }

  /* the retraction half of a batch: every theory model whose held or recomputed
     perspective is not empty gets the empty one */

  private def retraction(
    st: VSCode_Resources.State,
    doc_state: Document.State,
    overlays: Document.Overlays
  ): (List[(JFile, VSCode_Model)], List[Document.Edit_Text]) = {
    val pending_edits = Document.Pending_Edits.make(st.models.values)
    val retracted =
      (for {
        (file, model) <- st.models.iterator
        perspective = cancel_perspective(st, doc_state, pending_edits, model, overlays)
        if !Document.Node.Perspective_Text.is_empty(model.last_perspective) ||
          !Document.Node.Perspective_Text.is_empty(perspective)
      } yield (file, model)).toList
    val edits =
      retracted.map({ case (_, model) =>
        model.node_name -> Document.Node.Perspective_Text.empty: Document.Edit_Text })
    (retracted.map({ case (file, model) =>
        (file, model.copy(last_perspective = Document.Node.Perspective_Text.empty)) }),
      edits)
  }

  /* the one way a retraction is written back: models replaced, the retracted files
     whose edits are all flushed leave pending_input */

  private def apply_retraction(
    st: VSCode_Resources.State,
    retracted: List[(JFile, VSCode_Model)]
  ): VSCode_Resources.State = {
    val touched = retracted.iterator.map(_._1).filter(f => st.models(f).pending_edits.isEmpty).toSet
    st.copy(models = st.models ++ retracted, pending_input = st.pending_input -- touched)
  }

  /* step 0: once per request, before any flush.  The caret goes away, the overlay table is
     emptied (written directly: change_overlay would put the file back into pending_input
     and cancel the subtraction below), and every model gets the empty perspective.
     Returns Some(tip id before the update) if a batch was sent, None if there was nothing
     to retract. */

  def cancel_retract(
    session: VSCode_Session,
    doc_state: Document.State,
    deadline: Time,
    update_bound: Time
  ): VSCode_Resources.Retraction = {
    state.change_result_timed(deadline) { st =>
      val (retracted, edits) = retraction(st, doc_state, Document.Overlays.empty)
      val st1 = apply_retraction(st, retracted).copy(caret = None, overlays = Document.Overlays.empty)
      if (edits.isEmpty) (VSCode_Resources.Retraction(st1.overlay_serial, None), st1)
      else {
        val tip_before = doc_state.stable_tip_version.map(_.id)
        val st2 = st1.copy(update_serial = st1.update_serial + 1)
        VSCode_Resources.bounded_update(session, st.document_blobs, edits, update_bound)
        (VSCode_Resources.Retraction(st2.overlay_serial, Some(VSCode_Resources.Commit(tip_before))),
          st2)
      }
    }
  }

  /* one round of the retire loop, steps a(3) to f, under the monitor throughout.

     serial0 was read under the monitor BEFORE doc_state was taken; if the counter moved
     since, doc_state may predate a flush and its offsets are not trusted (Stale).  Unflushed
     edits go out first and alone (Flushed).  Otherwise V is the stable tip of doc_state,
     every target is located on V by command id, and the batch is: per node, the empty
     perspective first, then the retire edits in ascending start order. */

  def cancel_round(
    session: VSCode_Session,
    targets: Map[Document_ID.Exec, (Document.Node.Name, Document_ID.Command)],
    doc_state: Document.State,
    serial0: Long,
    deadline: Time,
    update_bound: Time
  ): VSCode_Resources.Round = {
    state.change_result_timed(deadline) { st =>
      if (st.update_serial != serial0) (VSCode_Resources.Stale, st)
      else if (st.models.values.exists(_.pending_edits.nonEmpty)) {
        /* step b */
        for ((file, model) <- st.models if model.pending_edits.nonEmpty && !st.pending_input(file)) {
          VSCode_Resources.cancel_fail("model with unflushed edits outside pending_input: " + file)
        }
        val pending_edits = Document.Pending_Edits.make(st.models.values)
        val changed =
          (for {
            file <- st.pending_input.iterator
            model <- st.models.get(file)
            perspective = cancel_perspective(st, doc_state, pending_edits, model, st.overlays)
            if model.pending_edits.nonEmpty || model.last_perspective != perspective
          } yield {
            val edits = model.node_edits(model.node_header, model.pending_edits, perspective)
            (edits, (file, model.copy(pending_edits = Nil, last_perspective = perspective)))
          }).toList
        val edits = changed.flatMap(_._1)
        if (edits.isEmpty) VSCode_Resources.cancel_fail("unflushed edits produced no edit")
        val tip_before = doc_state.stable_tip_version.map(_.id)
        val st1 =
          st.copy(
            models = st.models ++ changed.map(_._2),
            pending_input = Set.empty,
            update_serial = st.update_serial + 1)
        VSCode_Resources.bounded_update(session, st.document_blobs, edits, update_bound)
        (VSCode_Resources.Flushed(VSCode_Resources.Commit(tip_before)), st1)
      }
      else {
        doc_state.stable_tip_version match {
          case None => (VSCode_Resources.No_Stable_Tip, st)
          case Some(version) =>
            /* step d */
            val assignment = doc_state.the_assignment(version).check_finished
            val retired = new mutable.ListBuffer[VSCode_Resources.Retired]
            val excluded = new mutable.ListBuffer[VSCode_Resources.Excluded]
            for ((name, node_targets) <- targets.groupBy(_._2._1)) {
              val node = version.nodes(name)
              val wanted = node_targets.values.map(_._2).toSet
              val located: Map[Document_ID.Command, (Command, Text.Offset)] =
                (for {
                  (command, start) <- node.command_iterator()
                  if wanted(command.id)
                } yield command.id -> (command, start)).toMap
              for ((exec_id, (_, command_id)) <- node_targets) {
                val eval_exec = assignment.command_execs.getOrElse(command_id, Nil).headOption
                if (!doc_state.execs.isDefinedAt(exec_id)) {
                  excluded += VSCode_Resources.Excluded(name, command_id,
                    located.get(command_id).map(_._1), "gone")
                }
                else if (!eval_exec.contains(exec_id)) {
                  excluded += VSCode_Resources.Excluded(name, command_id,
                    located.get(command_id).map(_._1), "reassigned")
                }
                else {
                  located.get(command_id) match {
                    case None =>
                      excluded += VSCode_Resources.Excluded(name, command_id, None, "reassigned")
                    case Some((command, start)) =>
                      if (command.length == 0) {
                        VSCode_Resources.cancel_fail("offset invalid: empty command " +
                          command_id + " in " + name)
                      }
                      retired += VSCode_Resources.Retired(exec_id, name, command, start,
                        waived = command.length == 1 && start == 0)
                  }
                }
              }
            }
            val retire_edits: Map[Document.Node.Name, List[Text.Edit]] =
              retired.toList.groupBy(_.name).map({ case (name, rs) =>
                name ->
                  rs.sortBy(_.start).flatMap(r =>
                    if (r.command.length > 1) List(VSCode_Resources.zero_length_edit(r.start + 1))
                    else VSCode_Resources.net_zero_edits(r.start, r.command.source))
              })
            /* pre-commit validation (F19): an edit outside every command of the node makes
               Thy_Syntax.edit_text throw in the change parser and the session never produces
               a version again.  Dry-run the real function on V's commands -- the same
               sequential fold the pipeline performs; Command.unparsed mints no ids. */
            for ((name, edits) <- retire_edits) {
              Exn.capture { Thy_Syntax.edit_text(edits, version.nodes(name).commands) } match {
                case Exn.Exn(exn) =>
                  VSCode_Resources.cancel_fail(
                    "offset invalid in " + name + ": " + Exn.message(exn))
                case Exn.Res(_) =>
              }
            }
            /* step e: the retraction half re-done every round (resolve_dependencies may have
               added models since step 0), then the retire edits; per node the perspective
               edit comes first */
            val (retracted, perspective_edits) = retraction(st, doc_state, st.overlays)
            val perspective_of = perspective_edits.groupBy(_._1)
            val edits: List[Document.Edit_Text] =
              (perspective_of.keysIterator ++ retire_edits.keysIterator).toList.distinct.flatMap(name =>
                perspective_of.getOrElse(name, Nil) :::
                retire_edits.get(name).toList.map(es =>
                  name -> Document.Node.Edits[Text.Edit, Text.Perspective](es)))
            if (edits.isEmpty) {
              (VSCode_Resources.Retire(version, retired.toList, excluded.toList, None), st)
            }
            else {
              /* step f */
              val tip_before = doc_state.stable_tip_version.map(_.id)
              val st1 = apply_retraction(st, retracted).copy(update_serial = st.update_serial + 1)
              VSCode_Resources.bounded_update(session, st.document_blobs, edits, update_bound)
              (VSCode_Resources.Retire(version, retired.toList, excluded.toList,
                  Some(VSCode_Resources.Commit(tip_before))),
                st1)
            }
        }
      }
    }
  }

  /* the post-retraction invariant (plan section 3.3), asserted after the monitor was
     released for the last time: nothing the next flush could compute puts a perspective
     back, the overlay table is as step 0 left it, and no unflushed edit is outside
     pending_input */

  def cancel_assert_retracted(
    doc_state: Document.State,
    overlay_serial0: Long,
    deadline: Time
  ): Unit = {
    state.change_result_timed(deadline) { st =>
      if (st.caret.isDefined) VSCode_Resources.cancel_fail("caret still set after retraction")
      // step 0 emptied the table and recorded the serial; a change since is someone
      // else's overlay, which can hand the prover work after the retraction
      if (st.overlay_serial != overlay_serial0) {
        VSCode_Resources.cancel_fail("overlay table changed after retraction")
      }
      for ((file, model) <- st.models if model.pending_edits.nonEmpty && !st.pending_input(file)) {
        VSCode_Resources.cancel_fail("model with unflushed edits outside pending_input: " + file)
      }
      val (retracted, _) = retraction(st, doc_state, st.overlays)
      if (retracted.nonEmpty) {
        VSCode_Resources.cancel_fail("perspective not empty after retraction: " +
          retracted.map(_._2.node_name).mkString(", "))
      }
      ((), st)
    }
  }


  /* pending output */

  def update_output(changed_nodes: Iterable[JFile]): Unit =
    state.change(st => st.copy(pending_output = st.pending_output ++ changed_nodes))

  def update_output_visible(): Unit =
    state.change(st => st.copy(pending_output = st.pending_output ++
      (for ((file, model) <- st.models.iterator if model.node_visible) yield file)))

  def flush_output(channel: Channel): Boolean = {
    state.change_result { st =>
      val (postponed, flushed) =
        (for {
          file <- st.pending_output.iterator
          model <- st.models.get(file)
        } yield (file, model, rendering(model))).toList.partition(_._3.snapshot.is_outdated)

      val changed_iterator =
        for {
          (file, model, rendering) <- flushed.iterator
          (changed_diags, changed_decos, model1) = model.publish(rendering)
          if changed_diags.isDefined || changed_decos.isDefined
        }
        yield {
          for (diags <- changed_diags)
            channel.write(LSP.PublishDiagnostics(file, rendering.diagnostics_output(diags)))
          if (pide_extensions) {
            for (decos <- changed_decos)
              channel.write(rendering.decoration_output(decos).json(file))
          }
          (file, model1)
        }

      (postponed.nonEmpty,
        st.copy(
          models = st.models ++ changed_iterator,
          pending_output = postponed.map(_._1).toSet))
    }
  }


  /* output text */

  def output_text(content: String): String = Symbol.output(unicode_symbols_output, content)
  def output_edit(content: String): String = Symbol.output(unicode_symbols_edits, content)

  def output_text_xml(body: XML.Body): XML.Body =
    body.map {
      case XML.Elem(markup, body) => XML.Elem(markup, output_text_xml(body))
      case XML.Text(content) => XML.Text(output_text(content))
    }

  def output_pretty(body: XML.Body, margin: Double): String =
    output_text(Pretty.string_of(body, margin = margin, metric = Symbol.Metric))
  def output_pretty_tooltip(body: XML.Body): String = output_pretty(body, tooltip_margin)
  def output_pretty_message(body: XML.Body): String = output_pretty(body, message_margin)


  /* caret handling */

  def update_caret(caret: Option[(JFile, Line.Position)]): Unit =
    state.change(_.update_caret(caret))

  def get_caret(): Option[VSCode_Resources.Caret] = {
    val st = state.value
    for {
      (file, pos) <- st.caret
      model <- st.models.get(file)
      offset <- model.content.doc.offset(pos)
    }
    yield VSCode_Resources.Caret(file, model, offset)
  }


  /* decoration requests */

  def force_decorations(channel: Channel, file: JFile): Unit = {
    val model = state.value.models(file)
    val rendering1 = rendering(model)
    val (_, decos, model1) = model.publish_full(rendering1)
    if (pide_extensions) {
      channel.write(rendering1.decoration_output(decos).json(file))
    }
  }


  /* spell checker */

  val spell_checker = new Spell_Checker_Variable
  spell_checker.update(options)
}
