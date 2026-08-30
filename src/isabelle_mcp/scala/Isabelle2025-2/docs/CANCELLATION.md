# Patch-free cancellation, and cancellation that leaves no corpse

How `isabelle mcp_server` stops all running proofs **without patching the Isabelle
distribution**, how it then puts every interrupted command back to *unevaluated* so that
the next evaluation re-runs it, and the evidence that both work.

The authoritative design is `ISABELLE_MCP_CANCELLATION_REDESIGN_PLAN.md` (rev 6) in the
parent tree; this document is the component-level account that travels with the code.
Sections 1, 3, 4, 6 and 8 are the original (2026-05/06) analysis of the stanch mechanism
and are unchanged in substance; sections 2, 5, 7 and 9 describe the 2026-08-30 redesign.

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
val snapshot: Document_ID.exec list -> Future.task list   (* the live tasks of these execs *)
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

This document describes how to get the same behaviour with **zero distribution changes** — and
then how to go further than the patch ever did.

---

## 2. Stopping is not enough: the corpse problem

Cancelling an exec (§3) stops the CPU burn. It does **not** put the command back to
"unevaluated": the interrupted exec keeps its entry in `Execution`'s table, its result is
memoised as `Fail "Interrupt"`, and — this is the part that matters — the next `Document.update`
**keeps it**. A command's exec is retained across versions when (`document.ML:674-675`):

```ml
Command.eval_eq (eval0, eval) andalso (visible' orelse node_required orelse Command.eval_running eval)
```

and `eval_running = Execution.is_running_exec o eval_exec_id` (`command.ML:159`) is "already
registered in `Execution`'s table" — true for finished and for interrupted execs alike. **An
interrupted exec is therefore pinned inside the common prefix**: no perspective change, no
required-set change, no later evaluation will ever re-run that command. The agent sees an error
with no content at that line, and the only way out is to edit the file. We call such an entry a
*corpse*.

The only PIDE primitive that re-mints a command is a **text edit inside it**: `Thy_Syntax.edit_text`
replaces the hit command by `Command.unparsed`, `chop_common` cannot match it, and the re-parse
gives it a fresh id (`thy_syntax.scala:128-141, :250-253`). The old exec then lands in `removed`
and is cancelled and purged by the standard pipeline (`document.ML:724-725, :896-897`,
`protocol.ML:136-141`). A *zero-length* edit — inserting the empty string at `start + 1` — does
exactly that while leaving the text byte-identical (§5); a one-character command has no interior
and gets a *net-zero pair* instead (remove its source, insert it back at the same offset).

> **History.** The first fork (2026-05) cancelled and then appended a space to line 0 of the
> target file, re-minting the whole node and re-running everything after the header; a later
> revision anchored that space at the first unfinished command. Both changed the text the user
> sees and both were scoped to one file. The redesign replaces them with the mechanism above,
> applied to exactly the interrupted commands, in whatever theories they live.

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
reaches the imported-theory case a single-file edit cannot.

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

**The probe set.** Retirement (§5) needs to know which of those execs are *alive*, and only the
*eval* execs (the map also holds print entries). Scala reads them off its most recent stable
version: one pass over `V0.nodes × node.command_iterator()`, taking
`the_assignment(V0).command_execs(cmd.id).headOption` — the eval exec is always the head
(`command.ML:414-415`, `document.scala:1153`). ML then answers which of them still have live
tasks: `Execution.snapshot` returns one flat task list for a whole id list, so a single call only
says "some of these are alive"; the per-id answer comes from *binary narrowing* — split a
non-empty list in half, recurse only into non-empty halves. Attribution is by ancestry (the same
`fold_groups` registration as above), so a task forked inside a command body is seen under its
command's exec. There is no cap on the number of calls: the protocol loop runs the probe inline
and uninterruptibly, and the only bound is the Scala side's budget (§5).

---

## 5. The implementation (rev 6, 2026-08-30)

One LSP request, `PIDE/cancel_evaluation`, no parameters, **one reply**, three outcomes:
`retired`, `nothing_running`, `aborted`. The request runs on its own bare thread, against one
120 s deadline; anything it cannot finish in time, or anything that throws, is `aborted`, and the
Python side then terminates the prover (§5.4).

### 5.1 Stanch and probe — `ML/mcp_prelude.ML`, `Isabelle_MCP.cancel_evaluation serial cancel_ids probe_ids`

Three steps, in this order, all inside one `Exn.capture` after the serial has been stripped:

1. `Execution.discontinue ()` — the barrier (§3.3). Nothing that has not started will start.
2. **Probe** which of `probe_ids` still have live tasks (§4).
3. `Execution.cancel` on every id in `cancel_ids` (every exec Scala knows).

Probe **before** cancel: a cancelled task leaves its group before a later snapshot could see it,
and an interrupted command nobody reports is exactly the corpse the design forbids.

The reply is one protocol message `isabelle_mcp_cancel_report` with properties `serial`,
`calls` and, if any step failed, `error`; its single body chunk lists the live ids. On failure
the whole probe set is reported alive — an exec nobody could examine must be retired, not
forgotten. The report goes out after every step; only then is a failure re-raised.

**Ordering, not wall-clock, tells a lost report from a slow one.** Right after the cancel
command Scala sends `Isabelle_MCP.ping serial`. The manager mailbox, the ML protocol loop and
the ML message channel are all FIFO, so a pong that arrives *without* the report proves the
report lost (→ `aborted`); a report arriving is the normal case; neither arriving is waited out
to the deadline. The pong echoes the serial in its **properties** (the body stays the bare
version string the startup handshake compares); a pong without a serial fulfils the startup
handshake, so a new jar with an old prelude still reaches the version diagnostic.

### 5.2 Retract — `VSCode_Resources.cancel_retract`

Once, before anything else: the server-side caret goes away, the overlay table is emptied, and
every document model gets the empty perspective. The prover stops handing out work; nothing
finished is lost (a finished exec keeps its command alive in the common prefix, §2). With the
caret gone and the overlay table empty, the next flush recomputes an empty perspective and sends
nothing — retraction is a fixpoint (measured, §7).

### 5.3 Retire to completion — `VSCode_Resources.cancel_round`, driven by `Language_Server.cancel_evaluation_body`

Every exec the probe reported alive is a *target* `(exec_id, node, command_id)`. Each round:

- **a.** read the monitor's `update_serial`; take `session.get_state()` *outside* the monitor
  (a manager round trip under the monitor would pin the whole server); re-enter and check the
  serial is unchanged — otherwise the reading is stale and the round is retaken;
- **b.** if any model holds unflushed edits, flush them first (this round sends nothing else);
- **c.** `V :=` the stable tip version; none → wait a beat, retake;
- **d.** locate each target on **V** by command id: its exec no longer in `execs` → excluded
  `gone`; its command has another eval exec on V, or the id is not in V's node → excluded
  `reassigned` (a real edit already re-minted it); otherwise one zero-length edit at `start + 1`
  (net-zero pair for a one-character command). Before commit the batch is dry-run through
  `Thy_Syntax.edit_text` on V's commands — an edit outside every command would make the change
  parser throw and the session would never produce a version again;
- **e.** the batch = per node, the empty-perspective edit first, then the retire edits in
  ascending order — one `session.update`, never split;
- **g.** wait until the stable tip's id changed;
- **h.** on that tip, a target whose command id is gone is done; one still present goes into the
  next round; the same target failing twice in a row is `aborted`.

The monitor is held from a(3) to the commit, so no flush can interleave with the offsets taken
from V; every wait is against the one deadline; the monitor itself is a re-entrant lock with a
timed acquisition (a flush holding it for the rest of the budget is `aborted`, not a hang).

"Retired" claims exactly this: the version carrying the retire edits has been assigned and on it
the target command ids have changed. It proves the Scala parse and assignment; it does not prove
that ML's `Execution.purge` has finished (that runs in a forked task after the cancelled tasks
are dead).

### 5.4 The Python side — one budget, one catastrophe

`IsabelleLSPClient.force_interrupt()` sends the request (135 s reply timeout: the server's 120 s
plus dispatch queueing) and admits **only** the two success payloads; the server's `aborted`, a
timeout, a transport failure or a reply outside the contract raise `IsabelleCatastrophe`.
`cancel_evaluation` holds the evaluation-state lock for the whole request (every other tool
call queues behind a cancellation) under **one** total budget (`anyio.move_on_after(150 s)`,
with the scope's `cancel_called` as the witness — `fail_after` stays silent when the deadline
passes inside a shielded segment); expiry raises the same exception.

`IsabelleCatastrophe` is system-wide: exactly one handler, `CatastropheMiddleware` at the tool
boundary, logs the reason, tears the prover down through the single `IsabelleLSPClient.teardown()`
(shared with `isabelle_terminate`, relaunch and server shutdown; layered so that whatever
`shutdown()` does, the process is killed and forgotten and every in-flight waiter is failed), and
answers with one fixed sentence: *"The Isabelle session hit an internal failure and has been
terminated; call isabelle_launch to start a new one. Details are in the server log."* The
exception is a `FastMCPError` — FastMCP masks any other exception into a generic tool error
before middleware sees it.

The agent-facing reply on success is the main sentence plus the list of reset commands
(`file:line (keyword)`); commands excluded as `gone`/`reassigned` are back to unevaluated all the
same and are only logged.

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

That is why the server pings the prelude after startup and refuses to serve without a pong, and
why the pong carries a **prelude version** that must match the jar's (`Language_Server.prelude_version`,
currently `"6"`): the two sides share the cancel report format, the query commands and the
debugger eval wrapper, and a skew there is a request that hangs with no correlatable trace.

### 6.3 Do not block the protocol loop

`Protocol_Command.run` executes on ML's **single protocol reader thread**. An early prototype
called `Scala.function` from inside a protocol command and **deadlocked**: `Scala.function`
blocks waiting for Scala's reply, which arrives as *another protocol command* (`"Scala.result"`)
that only the blocked loop could read. Any protocol command that waits on the other side must
`Future.fork` its body — as Scala's own `Scala.Handler` does.

`discontinue`, the probe and `cancel` never block on Scala. The probe *is* CPU work on the
protocol thread, uninterruptible once dispatched — which is why the only bound on it is the
request budget after which the prover is terminated.

---

## 7. Validation

### 7.1 The stanch (2026-06, unchanged)

Cancellation was first judged by **prover CPU time** (`utime + stime` over the whole process tree
from `/proc/<pid>/stat`), not by status strings, in a multi-node scenario: the burning proofs
live in `Burn.thy`, *imported* by `Top.thy`, so no edit to `Top.thy` can reach them and whatever
stops the burn is the cancel command. `Burn.thy` holds non-terminating forked proofs built on an
allocating loop (it hits GC safe points and is genuinely interruptible).

| cancel mechanism | needs a patch? | CPU before | CPU after |
|---|---|---|---|
| `Document.cancel_execution` (the ML patch) | yes | 3.50 cores | **0.01** |
| **`discontinue` + `cancel` from the prelude (this design)** | **no** | 3.36 cores | **0.03** |
| none — an edit to the importing file only | — | 4.59 cores | **3.08, steady for 30 s** |

### 7.2 The redesign — the Z series (2026-08-30, `tests/integration/test_cancel_z.py`)

Device: a theory whose slow command is a time-bounded *allocating* loop (interruptible; left
alone it finishes, so a re-run can complete), and file-append witnesses written by the commands
themselves (a command that ran leaves its letter). Iron rule: every step ends with an
`evaluate_to`, and the witness must move — a focus trap cannot fake a result.

| Z | what | result |
|---|---|---|
| Z2/Z4/Z6/Z22 | cancel the target file | 0.43 s; reply `Reset to unevaluated: Z2.thy:6 (ML)`; the line reads `not_evaluated`, the prefix `processed`, witness `A`; idle 25 s → nothing re-runs; a second cancel is the early-exit reply and the server answers `nothing_running` when asked directly; re-evaluation completes with witness `ASC` (the interrupted command ran to its end); `goal` still answers |
| Z3 | the interrupted command is in an imported theory | retired there; the importer stays unevaluated; re-evaluating the importer re-runs it |
| Z5 | target whitelist | `.sml`/`.bib`/`.ml` refused and never opened; a `.ML` target is redirected to its `ML_file` command and the ML really ran |
| Z11 | two forked proofs alive in one theory | one batch, ascending (`:4 (by), :5 (by)`), both re-run afterwards |
| Z12 | retraction is a fixpoint | an edit pushed without a caret move reschedules nothing; the positive control (`evaluate_to`) does |
| Z14(a) | ~2000 evaluated commands, one alive | 0.52 s end to end — the probe is not the cost |
| Z17 | the file is edited while the cancel runs | retired; the session still produces versions; a full run completes |
| Z19 | `SIGSTOP` the ML process | `aborted` at 120.0 s with the reason; teardown 10.2 s; no `poly` process left; a fresh launch works |
| Z20/Z21 | 20 000 lines of output then cancel; a status call during the cancel | the report still arrives, retired; the status call completes after the cancel |

Not measured yet: Z14(b) (manager round-trip latency under a heavy backlog, which is what the
5 s bound on the monitor-held `session.update` rests on — only Z20's indirect evidence so far);
the injected-fault cases (a second round forced by a missed edit, a monitor held for the rest
of the budget, the hard-kill teardown branch). Every experiment uses the same allocating loop;
`sledgehammer`, `auto` and external provers were never cancelled in a test.

---

## 8. The stale-mirror race — probed, and closed (2026-06, unchanged)

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
only one document shape on one machine was probed. In the redesign this is boundary R3(vi):
an exec ML registers during the window is neither stanched nor retired by that request; the
"no corpse" claim is scoped to the probed set.

---

## 9. Scope

Retired for Isabelle-MCP, because the fork carries them as its own code:

- `pide_control`'s Scala half (`lsp.scala`, `language_server.scala`, `protocol.scala`)
- `perspective_eof_clamp` (`vscode_model.scala`)

Retired by this document's mechanism:

- `pide_control`'s ML half (`execution.ML`, `protocol.ML`)

**Not** affected — these serve Isa-REPL and Isa-Mini, not Isabelle-MCP:

- `register_thy`, `show_types_nv`, `expose_map_syn`, `expose_foreign`

**No Isabelle patch is required by Isabelle-MCP**, as a mechanism and as a product: the
component ships a prebuilt jar (`no_build = true`, see `docs/COMPONENT_INSTALL_PLAN.md`) and the
prelude, and the Python client no longer gates a launch on a patched distribution.
