# Caret, Perspective, and Position-Explicit Queries — Research Findings

Source-grounded findings from two research passes over the Isabelle2025-2
distribution and this repository's Scala fork, gathered while designing an
upgrade to the existing query tools. Everything here is **derived from reading
sources**; the items in §5 are explicitly flagged as needing experiment before
being relied on. Line numbers are pristine Isabelle2025-2 and the current fork.

Motivation: four of the six query tools are position-explicit and never move the
caret (`isabelle_hover`, `isabelle_definition`, `isabelle_local_occurrences`,
`isabelle_command_output`); two move the global caret and are therefore refused
during any evaluation — `isabelle_goal`'s proof-state half and
`isabelle_find_theorems`. Two ways out were investigated: move the caret and
restore it, or remove the caret dependence entirely.

---

## 1. What a caret round-trip actually costs

**A caret move never cancels execution that has already started.** In
`Document.update`, a command survives in the common prefix if it is
`visible' orelse node_required orelse Command.eval_running eval`
(`document.ML:674-676`). `Command.eval_running` is true for any exec registered
in `Execution`'s table (`command.ML:159`, `execution.ML:100-101`); an exec is
registered when it *starts* and removed only by `Execution.purge`, whose sole
live caller is `protocol.ML:141` acting on execs a new assignment superseded —
which requires a **text edit**. So every command that ever began running,
finished or not, is pinned against arbitrary perspective changes.

Independent empirical corroboration already in this repository: caret-move-only
was tried as a cancellation mechanism and failed to stop forked proofs
(`docs/ARCHITECTURE.md:555-556`, and the measurement in
`scala/Isabelle2025-2/docs/CANCELLATION.md` §7).

What a caret excursion does cost:

1. **The frontier stalls.** With `node_required = false` — which is the normal
   case for every theory this server drives (`vscode_model.scala:108`, and
   `File_Format.registry.is_theory` is false for ordinary `.thy`) — new execs
   are created only up to `visible_last` (`document.ML:864-870`). While the
   caret is away, nothing past the new window advances; only already-started
   commands finish.
2. **Unstarted commands churn.** Commands assigned but not yet running are
   dropped and re-created with fresh exec ids on return. No proof work is lost,
   but they momentarily render as unprocessed, and any exec-id bookkeeping goes
   stale.
3. **Print tasks are cancelled and re-created** (see §2 — `print_state` is not
   persistent).
4. **Lexically unfinished files re-parse.** If any command in the node is
   unfinished (unterminated token, cartouche, or comment), every perspective
   change re-parses spans up to `editor_reparse_limit`
   (`thy_syntax.scala:328-351`), minting new command ids — genuine
   re-execution and cancellation.
5. **Cross-file moves are worse.** Moving the caret to another file makes the
   first file's perspective empty and the second's node visible;
   `make_required` (`document.ML:514-527`) may then pull the second file's
   imports into the required set and start executing them, competing for
   workers.

Also established: **`didOpen` with no caret executes nothing.** The node gets an
empty perspective (`vscode_model.scala:101-137` with the caret absent →
`Text.Range.offside`), no execs are created and no worker task is scheduled
(`document.ML:551`). Opening a file only resolves dependencies. (Its cost to an
in-flight evaluation is the global decoration-freshness invalidation on our
side, not prover work.)

**`vscode_caret_perspective = 0` is a trap.** The option means "unrestricted",
not "no window": `caret_range` becomes the whole document whenever the node is
visible (`vscode_model.scala:122`). A bare `didOpen` would then execute the
entire file to EOF, destroying the evaluate-only-to-the-caret model this server
is built on. The stock default is 50; this server passes 1
(`lsp_client.py:361`).

## 2. Where the proof state comes from

Two independent mechanisms exist, and the current implementation uses the harder
one.

**(a) The `print_state` print function.** Registered at `command.ML:478-489`,
gated on the prover-side default of the `editor_output_state` system option and
on the command being a printed (goal/proof) command. Its `Output.state` messages
carry `Markup.STATE`, accumulate into that command's results
(`command.scala:349-358`), and are therefore readable from a plain document
snapshot via `snapshot.command_results(command)`.

**This is already being read and thrown away.** The fork's `output_at_position`
partitions results and emits the state half only when a Scala-side flag is set
(`language_server.scala:668-674`):

```scala
val (states, other) =
  results.iterator.map(_._2).filterNot(Protocol.is_result).toList
    .partition(Protocol.is_state)
val output = (if (output_state) states else Nil) ::: other
```

The two `editor_output_state` settings that look contradictory are not: the
session options sent to the prover hard-code it to **true**
(`language_server.scala:355` → `Prover.options` → `Options.set_default`), so the
prover always computes proof state for visible printed commands; while
`resources.options` carries the raw command line, where this server passes
**false** (`lsp_client.py:364`), making it purely a **Scala-side display
filter**.

**(b) The `print_state_query` query operation** — what the state panel uses
(`state_panel.scala:71-89`). It inserts a temporary document overlay on a
specific command and reads `Markup.RESULT` messages tagged with an instance id.
No protocol command is involved; the overlay travels in the ordinary
`Document.update`. **The caret dependence lives entirely in
`editor.current_command`**, not in the underlying mechanism.

**The retention caveat that decides the design.** Prints are instantiated only
for *visible* commands, and `print_state` is `persistent = false`
(`command.ML:346-384`), so it is not retained once a command leaves the
perspective — its print exec is dropped from the assignment and the state
disappears from the snapshot. With a ±1-line window, proof state exists only for
commands next to the caret. **The state is therefore not freely readable for an
arbitrary command; interest must be declared.**

**The escape hatch: overlays.** What ML receives as the visible command set is
`visible_overlay`, not `visible` (`thy_syntax.scala:322-325`), and a command
carrying an overlay is added to it regardless of the caret
(`thy_syntax.scala:41-42`). So an overlay makes one specific command visible on
the ML side **without touching the caret and without widening the window**.

Note the interaction with §1: `visible_last` is the last command of that list in
document order, so an overlay *behind* the frontier does not extend execution,
while an overlay *ahead* of it would. A policy of only serving positions that are
already processed therefore also guarantees the overlay cannot pull the frontier
forward.

## 3. Where find_theorems depends on the caret

Exactly one place: `Query_Operation.apply_query` calls
`editor.current_command(editor_context, snapshot)`
(`query_operation.scala:171-192`). Everything downstream is position-agnostic.

The `Editor` abstraction already provides the intended hook: `type Context` with
`current_node`, `current_node_snapshot`, `current_command` taking it
(`editor.scala:29, 108-111`) — jEdit passes a `View` there. The fork pins
`type Context = Unit` (`language_server.scala:90`). Giving the context an
optional position, resolved when present and falling back to the caret when
absent, makes the query operation position-explicit with no change to Pure.

The fork's `VSCode_Find_Theorems` (`language_server.scala:797-831`) is a single
long-lived query operation and inherits the caret dependence wholesale; the
Python side sets the caret first and then sleeps 0.15 s
(`lsp_client.py:1563-1567`).

Pure already ships the position→command resolver:
`Document.Snapshot.current_command(node_name, offset)`
(`document.scala:777-786`), which does what the fork's `command_at_position`
iteration does plus the backward skip over ignored commands. The fork's existing
`rendering_offset` (`language_server.scala:208-212`) supplies the offset.

## 4. The `focus` field is dead weight inbound

The fork's `Caret_Update.unapply` extracts only the uri and position; `focus` is
never read (`lsp.scala:573-593`). Inbound, a caret update with or without
`focus` behaves identically, and **either way it recomputes the node
perspective**. `focus` is only meaningful outbound (server→client, telling an
editor to reveal a location). So it is not a cheap non-disturbing query channel.

One genuinely cheap detail does exist: updating the caret to its current value is
a no-op for the perspective (`vscode_resources.scala:33-38`).

## 5. Not settled by the sources — verify by experiment

1. Whether an overlay on a command outside the text perspective triggers any
   **re-evaluation**. Code reading says it creates only a new *print* exec
   (`document.ML:657-691` reuses the eval), but this was not measured.
2. The width and reachability of the **unstarted-frontier race**: a command
   assigned but not yet running is dropped by a perspective shrink. Benign
   (fresh exec id, no lost work) but it churns exec ids.
3. How often an agent's in-progress file is **lexically unfinished**, tripping
   the re-parse path of §1.4.
4. The cost of running `print_state` broadly (only relevant if the perspective
   were widened, which §1 argues against anyway).
5. Why the Python client sleeps 0.15 s after a caret update
   (`lsp_client.py:1566`) — an undocumented timing dependency the sources do not
   explain.

## 6. Consequences for the design

- Position-explicit does **not** mean zero document traffic: the
  perspective/overlay is the only declaration-of-interest channel, so a
  position-explicit state or query still causes a `Document.update` carrying an
  overlay. What it avoids is moving the **global caret**, which is the resource
  that drives evaluation.
- Neither feature needs a patch to the Isabelle distribution.
- The current implementation never restores the caret after a query
  (`lsp_client.py:1464, 1562, 1664` all set it and leave it), so today an
  `isabelle_goal` call permanently relocates the evaluation frontier's anchor.
  This is invisible only because the guard forbids queries during an evaluation.
