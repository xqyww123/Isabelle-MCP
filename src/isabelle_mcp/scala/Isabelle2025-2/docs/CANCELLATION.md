# Patch-free global cancellation

How `isabelle mcp_server` stops all running proofs **without patching the Isabelle
distribution**, and the evidence that it works.

> **Reading the citations.** Line numbers refer to a **pristine** Isabelle2025-2. The checkout
> in `contrib/Isabelle2025-2` is *patched* by `my_better_isabelle_prover`, so a few of the
> spans below will not line up there — notably `execution.ML`, whose patched signature already
> contains the `cancel_execution` this document says is absent. The `*.bak` files next to the
> patched sources are the pristine originals.

---

## 1. The problem

An agent driving Isabelle must be able to abandon a proof that has run away — a `sledgehammer`
that will not return, an `auto` that diverges, a tactic that loops. "Abandon" has to mean *the
prover stops burning CPU*, not *the client stops waiting*.

Isabelle tracks running work in `Execution` (`src/Pure/PIDE/execution.ML`):

```ml
datatype state = State of
 {execution_id: Document_ID.execution,                        (* the current execution *)
  nodes: Future.task list Symtab.table,
  execs: (Future.group list * print list) Inttab.table};      (* exec_id -> its future groups *)
```

`vscode_server` cancels by asking ML to walk that `execs` table and cancel every group. But the
table is **private to the `Execution` structure**, and the public `EXECUTION` signature exposes
**no non-destructive enumerator**:

```ml
val discontinue: unit -> unit                      (* clear execution_id *)
val cancel: Document_ID.exec -> unit               (* cancel ONE exec *)
val peek: Document_ID.exec -> Future.group list
val reset: unit -> Future.group list               (* returns ALL groups -- but see below *)
```

`reset` does fold the whole table, but it is a **shutdown primitive**: it also clears `nodes`
and re-initialises `execs`, after which `Execution.fork` / `Execution.print` /
`Execution.fork_prints` raise `Fail (unregistered exec_id)` for any exec the document still
references. Stock Isabelle calls it only from `Isabelle_Process` at exit and from `Thy_Info`
after a batch build. It cannot be used to cancel a live session — which is exactly why the
`pide_control` patch adds a `cancel_execution` that preserves `nodes`/`execs`.

That patch is expensive to depend on:

- it edits `src/Pure/PIDE/{execution,protocol}.ML`, so **every session heap on the machine is
  invalidated** — Pure, HOL, AFP, everything must be rebuilt;
- the patch manager shells out to an external GNU `patch` binary, whose availability on
  macOS / Windows-Cygwin is unverified;
- it couples Isabelle-MCP to a second repository's release cycle.

This document describes how to get the same behaviour with **zero distribution changes**.

---

## 2. Why the fallback path is not enough

Isabelle-MCP's `force_interrupt` (`src/isabelle_mcp/lsp_client.py`) cancels, then moves the
caret to line 0 and sends a `didChange`. It is worth being precise about what actually stops
the prover there, because the obvious explanation is wrong.

**A perspective change cannot cancel a running exec.** In `Document.update`, a command's exec
is retained across versions when (`document.ML:676`):

```ml
Command.eval_eq (eval0, eval) andalso (visible' orelse node_required orelse Command.eval_running eval)
```

and `eval_running = Execution.is_running_exec o eval_exec_id` (`command.ML:159`) — "already
registered in `Execution`'s table". **An already-running exec is therefore pinned inside the
common prefix and cannot be dropped by a perspective or required-set change alone.**

**What does the stopping is the synthetic edit.** `force_interrupt` inserts a literal `" "` at
the end of line 0 — *inside the theory-header command's span*. That command gets a new id,
`lookup_entry node0` misses, the common prefix collapses, the whole node is re-assigned, and
every old exec of that node lands in `removed`:

```ml
(* document.ML:724-725 *)
fun removed_execs node0 (command_id, exec_ids) =
  subtract (op =) exec_ids (Command.exec_ids (lookup_entry node0 command_id));

(* document.ML:896-897 *)
val removed = maps (removed_execs node0) assign_result;
val _ = List.app Execution.cancel removed;
```

So `document.ML:897` cancels **execs the new assignment supersedes** — execs invalidated by the
*edit*. There is no code path that cancels an exec because its node left the required set.

**Hence the fallback is scoped to the edited file, and only that file.** Measured (§7): with the
cancel command disabled and the runaway proofs living in an *imported* theory, the synthetic
edit to the importing file cannot reach them and the prover **keeps burning 3.08 cores for at
least 30 seconds** — the tactics are non-terminating, so nothing will ever stop them. Editing a
theory that imports other theories is the normal case for an agent, so relying on the fallback
means a reproducible, silent, permanent CPU leak while the tool reports `cancelled`.

**The cancel command is load-bearing.** The only question is how to get one.

> **What cancellation means in PIDE.** It stops the *current* execution; it does not mark a
> command as abandoned. Any later `Document.update` calls `Execution.start ()` and re-schedules
> every required node, so a cancelled proof that is still in a still-required file will run
> again on the next edit — anywhere in the document. This is PIDE semantics, identical under the
> patch, and it is why a cancel is not a substitute for changing the source.

---

## 3. The three mechanisms

### 3.1 ML can be injected at prover startup — `use_prelude`

`ML_Process` assembles the Poly/ML command line (`src/Pure/ML/ml_process.scala:103-106`):

```
poly  --eval "(PolyML.SaveState.loadHierarchy [<session heaps>]; PolyML.print_depth 0)"
      --eval "Options.load_default ()"
      --eval "Resources.init_session_env ()"
      --use  <use_prelude files>          <-- our ML goes here
      --eval "Isabelle_Process.init ()"   <-- the protocol loop starts only here
```

The `--use` files land in exactly the right window:

- **after** the session heap is loaded, so `Execution`, `Protocol_Command`, `Output`,
  `Document_ID` are all present in Poly/ML's global namespace;
- **before** `Isabelle_Process.init ()`, so no `Document.update` has happened yet.

`Isabelle_Process.start(options, session, background, heaps, use_prelude = …, …)` is **public
API** (`src/Pure/System/isabelle_process.scala`); `vscode_server` merely passes the default. Our
fork passes a file. Nothing in the distribution changes.

> **Two constraints.**
> 1. `--use` runs the *raw Poly/ML compiler*, not Isabelle's. The prelude must be plain SML: no
>    antiquotations (`\<^here>`), no cartouches (`‹…›`).
> 2. **A failing `--use` is fatal.** A missing file, or one that does not compile, aborts poly
>    before `Isabelle_Process.init ()`, so the prover never comes up at all. See §6.

### 3.2 Protocol commands are a runtime table

```ml
(* src/Pure/PIDE/protocol_command.ML *)
val commands =
  Synchronized.var "Protocol_Command.commands" (Symtab.empty: (Bytes.T list -> unit) Symtab.table);

fun define_bytes name cmd =
  Synchronized.change commands (fn cmds =>
   (if not (Symtab.defined cmds name) then ()
    else warning ("Redefining Isabelle protocol command " ^ quote name);
    Symtab.update (name, cmd) cmds));
```

Dispatch is a lookup in a synchronized table, not a compile-time match. Late definition — and
even redefinition — is supported, and is normal practice in Isabelle itself:
`print_operation.ML`, `simplifier_trace.ML`, `debugger.ML`, `scala.ML` all define protocol
commands from ordinary ML files. Scala then calls one with `session.protocol_command(name, args)`.

### 3.3 `discontinue` is a barrier; `cancel` cascades

**The barrier.** `Execution.discontinue ()` sets `execution_id := Document_ID.none`. Every
command exec must pass this check before it runs (`command.ML:419-425`):

```ml
fun run_process execution_id exec_id process =
  let val group = Future.worker_subgroup () in
    if Execution.running execution_id exec_id [group] then   (* ok = execution_id = current *)
      ignore (task_context group (fn () => Lazy.force_result {strict = true} process) ())
    else ()                                                  (* mismatch -> never runs *)
  end;
```

After `discontinue`, **every exec that has not yet started never starts**, and the node worker
loop stops iterating for the same reason (`document.ML`, guarded by `Execution.is_running`).

**The kill.** `Execution.cancel exec_id = List.app Future.cancel_group (peek exec_id)`, and
`peek` returns *all* groups of that exec — one per forked proof included, because
`Execution.fork` prepends each new subgroup to the exec's list.

**The cascade.** Cancelling a group also interrupts the *running threads* of its descendants,
because `Task_Queue` registers every task under its group **and all ancestors**
(`task_queue.ML:354-357`, via `fold_groups` at `:75-76`):

```ml
fun fold_groups f (g as Group {parent = NONE, ...}) a = f g a
  | fold_groups f (g as Group {parent = SOME group, ...}) a = fold_groups f group (f g a);

val groups' = fold_groups (fn g => add_task (group_id g, task)) group groups;
```

So `Task_Queue.cancel`'s `get_tasks groups (group_id g)` (`:297-304`) yields the whole subtree,
and `Future.cancel_group` → `cancel_now` → `Isabelle_Thread.interrupt_thread` reaches every one
(`future.ML:193-199, :392-400`).

**Conclusion.** `discontinue ()` followed by `Execution.cancel` on every live exec is
semantically what the patch does — using only public API. The one thing missing is *which exec
ids*.

---

## 4. Where the exec ids come from

ML cannot enumerate them non-destructively, but **Scala already has them**
(`src/Pure/PIDE/document.scala:962-972`):

```scala
final case class State private(
  ...
  execs: Map[Document_ID.Exec, Command.State] = Map.empty,   // ALL execs, across ALL nodes
  ...)
```

That map spans every node, not just the one holding the caret — which is precisely why it
reaches the imported-theory case the fallback cannot.

**Why ML never runs an exec Scala has not been told about.** The ML handler for `Document.update`
emits the assignment *before* it starts executing (`protocol.ML`, `Document.update` handler):

```ml
val (edited, removed, assign_update, state') = Document.update old_id new_id edits consolidate state;
...
val _ = Output.protocol_message Markup.assign_update [...];   (* 1. tell Scala the exec ids *)
in Document.start_execution state' end                        (* 2. only then run them *)
```

Scala's set is therefore a *superset* of what is running (it also retains finished execs from
earlier versions — it is never pruned — for which `Execution.cancel` is a no-op). The one gap in
that argument, and the experiment that probed it, are in §8.

---

## 5. The implementation

### ML — `ML/mcp_prelude.ML`

Plain SML, injected via `use_prelude`. The barrier runs **first**, before anything that could
raise:

```ml
val _ =
  Protocol_Command.define "Isabelle_MCP.cancel_execution"
    (fn args =>
      let
        val _ = Execution.discontinue ();                                  (* cannot fail *)
        val exec_ids = maps (map Document_ID.parse o space_explode ",") args;
        val _ = List.app Execution.cancel exec_ids;
      in
        Output.system_message
          ("Isabelle_MCP.cancel_execution: discontinued, cancelled " ^
            string_of_int (length exec_ids) ^ " execs")
      end);

val _ =
  Protocol_Command.define "Isabelle_MCP.ping"
    (fn _ =>
      Output.protocol_message
        [("function", "isabelle_mcp_pong")] [[XML.Text mcp_prelude_version]]);
```

An exec id ML does not know is a silent no-op: `Execution.cancel` → `peek` → `exec_groups`
returns `[]` for an unknown id, and `raise Fail (unregistered …)` occurs only in
`Execution.fork` / `print`, which the prelude never calls.

### Scala — `src/language_server.scala`

```scala
def cancel_execution(id: LSP.Id): Unit = {
  val execs = session.get_state().execs.keys.toList
  session.protocol_command("Isabelle_MCP.cancel_execution", List(XML.Text(execs.mkString(","))))
  log("cancel_execution: " + execs.length + " execs")
  channel.write(LSP.Cancel_Execution.reply(id))
}
```

and at startup, the prelude is guarded, injected, and **proved live**:

```scala
if (!prelude.is_file) error("Missing ML prelude: " + prelude + " …")

Isabelle_Process.start(options, session, session_background, session_heaps, modes = modes,
  use_prelude = List(File.standard_path(prelude))).await_startup()

session.protocol_command("Isabelle_MCP.ping")
if (!prelude_handler.await_pong(Time.seconds(10)))
  error("The ML prelude did not answer: cancellation would silently do nothing. …")
```

No file in the Isabelle distribution is touched.

---

## 6. Failure modes, and why the guards exist

### 6.1 A broken prelude kills the prover — loudly, now

Poly/ML treats a failing `--use` as fatal, so a missing or non-compiling prelude does not
degrade cancellation, it takes the whole server down. Worse, poly's reason goes to **stdout**,
and `Prover.Output.is_syslog` excludes stdout — so before the guards, the operator saw only
`Session startup failed: Return code 1`.

`Language_Server.init` therefore (a) checks `prelude.is_file` up front, and (b) subscribes
`session.raw_output_messages` for the duration of startup and appends whatever the prover said to
the error. Verified by hiding the file:

```
LSP error: Missing ML prelude: ".../scala/ML/mcp_prelude.ML"
The Isabelle-MCP component is incomplete; reinstall it.
```

### 6.2 An undefined protocol command is *not* fatal — hence the ping

If ML lacks the command, `Protocol_Command.run` raises, and the protocol loop turns it into a
*system message* and carries on (`isabelle_process.ML`). The prover survives, every cancel
request is accepted, and **nothing is ever cancelled**. Nothing crashes; the cores just burn.

That is why the server pings the prelude after startup and refuses to serve without a pong.
Verified with a prelude that compiles but defines nothing:

```
LSP error: The ML prelude did not answer: cancellation would silently do nothing.
Prover output:
  a prelude that defines nothing
  Undefined Isabelle protocol command "Isabelle_MCP.ping"
```

### 6.3 Do not block the protocol loop

`Protocol_Command.run` executes on ML's **single protocol reader thread**. An early prototype
called `Scala.function` from inside a protocol command and **deadlocked**: `Scala.function`
blocks waiting for Scala's reply, which arrives as *another protocol command* (`"Scala.result"`)
that only the blocked loop could read. Any protocol command that waits on the other side must
`Future.fork` its body — as Scala's own `Scala.Handler` does.

`discontinue` and `cancel` never block, so the cancel command is safe as written. (This property
also turns out to be why the stale-mirror race is closed — see §8.)

---

## 7. Validation

Cancellation is judged by **prover CPU time** (`utime + stime` over the whole process tree from
`/proc/<pid>/stat`, re-walking the descendants at each sample), not by status strings.

Two confounds had to be removed before any measurement meant anything:

1. **`force_interrupt` masks everything in a single file.** Its synthetic line-0 edit (§2) stops
   work on its own there, so a single-file test cannot attribute the stop to the cancel command.
   Control: with the command disabled, CPU still fell 2.44 → 0.12 cores.
2. **A bare cancel is not the production path.** Sending only `PIDE/cancel_execution` with no
   follow-up is unmasked but unrealistic: any later `Document.update` calls `Execution.start ()`,
   minting a fresh execution id and voiding the earlier `discontinue`.

The **multi-node scenario resolves both**: the burning proofs live in `Burn.thy`, *imported* by
`Top.thy`. `force_interrupt` edits only `Top.thy`, so `Burn.thy`'s commands are never re-parsed,
its execs never enter `removed`, and the synthetic-edit path provably cannot reach them. (`Top`
stays visible, so `Burn` stays *required*; requiredness is not what cancels.) Whatever stops the
burn there is the cancel command.

`Burn.thy` holds non-terminating forked proofs:

```isabelle
ML ‹fun burn (i: int) : unit =
  let val _ = String.size (Int.toString i) in if i = ~1 then () else burn (i + 1) end;›

lemma bx1: "True" by (tactic ‹fn st => (burn 0; Seq.single st)›)
```

(the loop allocates, so it hits GC safe points and is genuinely interruptible).

### Results

| cancel mechanism | needs a patch? | CPU before | CPU after |
|---|---|---|---|
| `Document.cancel_execution` (the ML patch) | yes | 3.50 cores | **0.01** |
| **`Isabelle_MCP.cancel_execution` (this design)** | **no** | 3.36 cores | **0.03** |
| none — fallback path only | — | 4.59 cores | **3.08, steady for 30 s** |

The fallback arm was produced by pointing the server at an *undefined* protocol command, which
reproduces an unpatched prover's response to the cancel request exactly (`Symtab.lookup` → `NONE`
→ `error` → system message → loop continues) without touching the distribution.

### Other coverage

- Isabelle-MCP's own `pytest -m integration`: 6/6 against `mcp_server`.
- `PIDE/find_theorems`, `theory_status`, `output_at_position`, `symbols`: exercised.
- After a cancel the session stays healthy: a fresh theory evaluates to `complete` and
  `command_output` still answers.
- No `Unregistered execution` failure is possible from this design, and the argument is static,
  not statistical: `Execution.cancel` on an id ML does not know goes `peek` → `exec_groups` → `[]`
  (`execution.ML`), and `raise Fail (unregistered …)` lives only in `Execution.fork` / `print`,
  which the prelude never calls.

---

## 8. The stale-mirror race — probed, and closed

The patch walks **ML's own table**; this design walks **Scala's mirror**. There is a window in
which ML has already begun `Document.start_execution` while the `assign_update` message
announcing the exec ids is still in flight to Scala's manager thread. A cancel landing there
would send an incomplete id list. `discontinue` would not help, since it only blocks execs that
have not yet called `Execution.running`.

**The window is real and was entered.** Harness: `Burn.thy` grown to ~4000 commands (a large
assignment message takes Scala longer to apply, widening the window), one cancel per round fired
at an offset anchored on the prover's own CPU trace. In six rounds Scala's `execs` map was
**completely empty** when the prelude read it. From the wire dump:

```
+3.005s  PIDE/cancel_execution {}                     <- fired
+3.661s  window/logMessage "...cancelled 0 execs"     <- ML runs it 656 ms later
+3.800s  PIDE/decoration Top.thy                      <- Scala's snapshot finally updates
```

**And yet nothing leaked** — 11/11 rounds clean, prelude and patched alike. The reason is
structural, not luck: **the two conditions for a leak are mutually exclusive.**

- **ML's protocol-command loop is single-threaded.** A cancel that races the assignment is
  necessarily queued *behind* the `Document.update` handler (measured: 656 ms of queueing), so it
  runs at `start_execution + ε` — before *any* exec has called `Execution.running`.
  `Execution.discontinue ()` then blocks all of them (CPU fell to 0.00, not merely the burners).
  The abstract worry — "`discontinue` cannot save an exec that is already running" — is true, and
  irrelevant: in this window nothing is running yet.
- **Conversely, by the time the burners run, the mirror is already complete.** The exec count
  flipped 0 → 16053 while ML was still forking execs inside `start_execution`: ML's own forking
  cost exceeds Scala's apply latency.
- **A third layer:** `editor_execution_delay = 0.02` (`document.ML:534`) gates every exec on an
  `Event_Timer` future at `now + 20 ms`. Execs do not start when `start_execution` returns.
  Setting it to `0` still produced no leak.

**Honest verdict: not reachable in this configuration, and the closure is structural — but not
proven impossible.** The margin rests on Scala's apply being faster than ML's exec-forking, and
only one document shape on one machine was probed.

### Limits of the evidence

- Every experiment used the same allocating ML loop, chosen *because* it is interruptible at GC
  safe points. **`sledgehammer`, `auto`, and external provers were never cancelled in a test.**
  This limitation is identical on the patched arm, so the two mechanisms' equivalence is
  unaffected — but "it stops the things an agent actually needs to stop" is not yet evidence.
- The end-to-end claim has not been run against a *pristine* Isabelle: this machine's
  distribution is patched. Nothing in this design uses a patched symbol, but that is an argument,
  not a measurement.

---

## 9. Scope — what this does and does not replace

Retired for Isabelle-MCP, because the fork carries them as its own code:

- `pide_control`'s Scala half (`lsp.scala`, `language_server.scala`, `protocol.scala`)
- `perspective_eof_clamp` (`vscode_model.scala`)

Retired by this document's mechanism:

- `pide_control`'s ML half (`execution.ML`, `protocol.ML`)

**Not** affected — these serve Isa-REPL and Isa-Mini, not Isabelle-MCP:

- `register_thy`, `show_types_nv`, `expose_map_syn`, `expose_foreign`

So **no Isabelle patch is technically required by Isabelle-MCP any more.** That is a statement
about the *mechanism*. It is not yet true of the *product*: the Python client still gates every
launch on `check_isabelle_patched()`, and the Scala component that provides `isabelle mcp_server`
is not yet packaged or registered by the installer. Both are packaging work, tracked separately.
