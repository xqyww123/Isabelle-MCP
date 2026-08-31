# Query-Tool Upgrade: Evaluation Target and Position-Explicit Queries

Status: **all six stages are done.** Every query tool is position-explicit and
answers while an evaluation is running; a query that cannot produce a result says
why instead of timing out; `isabelle_command_status` ships; the ML prelude, the
jar and the Python client all agree and check each other's version. Probes 1–7
have passed, nine integration tests cover the §5.3 table end to end, and the
tree, the tests and the docs are consistent with each other.

**Nothing is open.** What remains are the tickets §4.7 records, each of which
deserves its own change rather than a corner of this one.

**Read this document as history, not as a plan.** Every design decision it
records has been through review; where a section says "decided" or "approved",
it is not an invitation to reconsider. All agent-facing wording in it is approved
text — changing any of it needs a fresh sign-off.

### What anyone touching this again must know

The work shipped in these commits: `97391fd` (part A, plus the fixes an
adversarial review of it found), `41e8c84` (`file:line` everywhere, and the
running-vs-forked correction), `2ce2683` (the prelude), `dd67579` and `600e10d`
(wording), `3de6c43` (the Scala adapter), `3464790` (`isabelle_command_status`),
`979ae62` (releasing the last two tools), `3a75849` (the instructions),
`dd2884b` (dead code and docs), and the stage-6 commit.

**The contract is §5.1's "The reply, exactly".** Two LSP requests,
`PIDE/proof_state_at_position` and `PIDE/find_theorems_at_position`, each
answering `{status, comment, forked, content}`; one notification,
`PIDE/query_cancel`. The client supplies the correlation token and the deadline.
The sentence each status becomes is rendered in Python — that is where the file
and the line are known, and where the wording is unit-tested.

**Pass `resolve_caret`'s position, not column 0.** Column 0 of an indented line
sits inside the ignored span before the command, and the server's resolution
skips backward from there onto the *previous* command; `resolve_caret`'s rule —
the line's last non-blank character — is what makes a line resolve to its own
command.

**Taking a request out of its table is the permission to answer it**, on both
sides. Stage 6 found the one place that took without answering, and it orphaned
an LSP request. If you add a fourth way for a query to end, it must take the
entry and reply.

`mcp_prelude_version` is `"2"`, and the jar refuses to serve a prover whose
prelude says anything else. Bump both together.

Six facts, all measured, that the prelude depends on and that both other sides
must respect:

1. **Command ids are negative.** They are allocated by the JVM side, whose
   counter ticks backwards (`counter.scala`, `document_id.scala`). A probe that
   scans positive ids finds nothing, forever. They travel as Java decimals
   (`-1`): `Document_ID.parse` rejects ML's own `string_of_int` rendering of a
   negative number (`~1`) with `Bad integer`, which is how stage 3's first probe
   run failed.
2. **ML cannot map a position to a command.** A command's `Toplevel.pos_of`
   carries no absolute line, and the public `DOCUMENT` signature exposes only
   `command_exec: state -> node -> id -> exec option`. The Scala-resolves-the-id
   split of §5.1 is forced by the API.
3. **`command_exec` raises for an unknown id** (`Undefined command entry: N`),
   it does not return `NONE`. Wrap it, per §5.2 step 3 — and this is not a rare
   path: any edit to a file re-creates every command id in it (§3.10).
4. **A command still executing has no state to read.** `eval_result_state`
   forces a lazy value and gives `Fail "Unfinished lazy"` until the transition
   completes. §5.3 rules that this is refused, not worked around.
5. **`Pretty.string_of` output carries markup.** Strip it with
   `XML.content_of (YXML.parse_body ...)`; `YXML.content_of` does not exist in
   ML. §5.4 governs the real rendering.
6. **The exec id is the discriminator**, not the command id: an id can survive
   an imported-theory edit with a fresh exec underneath it (§3.10).

Two working notes on the prelude itself. It is compiled by the raw Poly/ML
compiler, and `isabelle ML_process -l HOL -f FILE` type-checks a candidate in
exactly that context without touching the installed component — use it before
every install, because a prelude that fails to compile is **fatal to every
prover start on the machine**, not just this session's. And the probe vehicle
§9 describes (a file-triggered read-only observer thread) is the cheapest way to
measure anything else on the ML side: no Scala change, no jar rebuild. Stage 3
extended that vehicle to drive the new protocol commands themselves, by teeing
`Private_Output.protocol_message_fn` — swallowing the replies whose `function`
is `isabelle_mcp_query_result`, which no jar yet handles, and forwarding
everything else untouched. That tee is the way to exercise the prelude end to
end before the Scala side exists.

Companion research notes: [`CARET_AND_POSITION_RESEARCH.md`](CARET_AND_POSITION_RESEARCH.md)
(caret/perspective semantics, overlays, where the proof state comes from). This
document adds the findings of a second, deeper pass and turns all of it into a
concrete design, an implementation plan, a cost estimate, and the list of things
that must be measured before the design is trusted.

Every factual claim below carries a source citation. Claims that could not be
settled from source are collected in §9 and must be settled by experiment —
this project's standing rule is that Isabelle-MCP behaviour is measured, not
inferred.

---

## 1. The problem

Three defects: one that produces a wrong answer, one visible to the agent, and
one structural.

**A stopped evaluation is reported as still running — and a successful one as
unfinished.** The wait loop watches a single boolean that three different writers
can clear, so it cannot tell a cancel from a session teardown from an
`isabelle_evaluation_status` call that observed the evaluation *succeed*; all
three render as `Evaluation in progress.` Because that string also reaches the
six query tools as their refusal text, the agent is told "in progress", polls
`isabelle_evaluation_status` as instructed, and is told "No evaluation in
progress." Full analysis and the approved fix are in §4.8.

**The server never says what it is working toward.** An evaluation target exists
internally — `evaluation_state.file_path` plus `destination_line`
(`evaluation.py:224-235`) — but almost none of it reaches the agent. The
result model carries `destination_line` and **no target file at all**
(`models.py:222-229`); the completion message names a line but not a file
(`evaluation.py:196`); the in-progress message names neither
(`evaluation.py:206-216`); and the other nine tools never mention it. When a
query is refused the agent is told only `"Evaluation in progress. Call
evaluation_status to check progress."` (`evaluation.py:836-840`) — no target,
no reason, no way to tell whether the position it asked about was even the
problem.

**Two query tools are blocked by a global resource they do not need.** Four of
the six query tools are position-explicit and never move the caret:
`isabelle_hover` (`textDocument/hover`, `lsp_client.py:1338`),
`isabelle_definition` (`:1417`), `isabelle_local_occurrences` (`:1426`), and
`isabelle_command_output` (`PIDE/output_at_position`, `:1381`). Two do move it:
`isabelle_goal`'s proof-state half (`get_goals_at_position` sends
`PIDE/caret_update` at `lsp_client.py:1465`, then runs the state-panel cycle)
and `isabelle_find_theorems` (`:1563`). Because the caret is a single global
that also drives evaluation, the guard refuses **all six** whenever an
evaluation is outstanding — including queries about lines that finished long
ago, and including files unrelated to the evaluation, since the `active` check
precedes any per-line check (`evaluation.py:835-846`).

Neither of the two caret-moving tools restores the caret afterwards
(`lsp_client.py:1464`, `:1562`, `:1664` all set it and leave it), so an
`isabelle_goal` call silently relocates the anchor that drives evaluation. This
is invisible today only because the guard forbids those calls during an
evaluation.

## 2. Goals

1. Make the evaluation target a first-class, always-reported concept.
2. Serve the four position-explicit query tools whenever the position they ask
   about is genuinely available, instead of refusing on a global flag.
3. Remove the caret dependence of the remaining two tools entirely, rather than
   managing it — so that they too become servable, and so that the caret stops
   being silently relocated.
4. Replace guessed answers with definite ones: today "this command has no proof
   state" is a timeout heuristic (`lsp_client.py:1546-1552`), not an observation.

Non-goal: changing what evaluation *is* or how it is driven. The caret remains
the evaluation anchor; we stop using it for anything else.

## 3. Established facts

The design rests on these. Each was verified against sources; the citations are
the audit trail.

### 3.1 Command output is already free of every constraint

Ordinary command messages (`writeln`/`warning`/`error`) are emitted by the
command's own evaluation and are **not gated by visibility**; only *print
functions* are. `docs/TECH_NOTE.md:177-195` records the empirical confirmation
(an error decoration at line 8 was pushed while the caret window was lines
11–14). They accumulate into the command's results
(`command.scala:349-358`) and are read from a plain snapshot by
`output_at_position` (`language_server.scala:670-674`). **So command output
needs no new mechanism at all** — it is already available for any command, with
no caret, no overlay, and no delay.

### 3.2 The proof state is a print function, and print functions need visibility

`print_state` (`command.ML:478-489`) and the state panel's
`print_state_query` (`query_operation.ML:47-57`) are both print functions.
`Command.print` instantiates print functions only when the command is *visible*,
and `print_state` is `persistent = false`, so it is not retained once the
command leaves the perspective (`command.ML:346-384`). With the ±1-line window
this server uses (`-o vscode_caret_perspective=1`, `lsp_client.py:361`), proof
state exists only for commands beside the caret. **Interest must be declared.**

Two ways to declare it: an overlay on the command (what the state panel does),
or bypassing print functions entirely (§3.3).

### 3.3 The prover can be asked directly, and there is a precedent in Pure

A finished command's `Toplevel.state` is reachable from ML through public API:
`Document.state ()` (`document.ML:35`) → `Document.command_exec`
(`document.ML:30`, `:480-484`) → `Command.eval_finished` (`command.ML:20`) →
`Command.eval_result_state` (`command.ML:22`) → `Toplevel.pretty_state`
(`toplevel.ML:29`). This is exactly the chain `Debugger.breakpoint` uses
(`debugger.ML:277-283`), so it is a sanctioned pattern inside Pure itself, not
an invention.

`Toplevel.pretty_state` delegates to the proof state's **own** context
(`toplevel.ML:237-242`), so syntax, notation and abbreviations are correct with
no context juggling.

### 3.4 Rendering is deterministic and reusable

In PIDE mode `Pretty.strings_of` emits **symbolic pretty markup with no margin
applied** (`pretty.ML:271-273`, `:545`); formatting happens on the Scala side.
The fork already exposes the exact renderer the client's parsers expect:
`Language_Server.render_query_html(messages: XML.Body): String`
(`language_server.scala:701-714`), written so "the client can reuse the same
HTML parsing" (`:697-700`).

Print options (`show_types`, …) are context configs whose defaults come from the
global options set by `Prover.options` (`config.ML:182-192`, `protocol.ML:21-23`);
there is no per-print channel (`command.ML:295-298`). **A direct read under the
same globals therefore yields byte-identical content to the print-function path.**

### 3.5 Two rendering traps

**`Pretty.formatted` must not be applied to find_theorems output.** It rewrites
`Markup.ITEM` into `item_markup = Markup.Expression.item`
(`pretty.scala:65`, `:240-241`), so `make_html` emits `<span class="expression">`
instead of `<span class="item">` — silently breaking
`parse_find_theorems_from_html`, which keys on `item`
(`utils/formatters.py:222`). The state-panel path can afford formatting because
it keys on `subgoal` (`utils/formatters.py:38-56`), which survives.

**Protocol-message chunks are not symbol-decoded.** Ordinary messages go through
`decode_xml = Symbol.decode_yxml_failsafe` (`prover.scala:253-254`), but for
`Markup.PROTOCOL` the chunks are handed over as raw `Bytes`
(`prover.scala:262-271`); only the properties are decoded. A reply body
containing prover text must be decoded explicitly, as `debugger.scala:108` and
`print_operation.scala:31` do. Skipping this yields `\<forall>` where every
other tool yields `∀`, and the Python side has no decoder to undo it (its
`ascii_of_unicode` is used only on input paths).

### 3.6 The protocol loop is sequential and unforgiving

`Protocol_Command.run` is called inline on the single protocol reader thread
(`isabelle_process.ML:149-165`), and the whole of `init_protocol` is
`Thread_Attributes.uninterruptible` (`:76`). A slow handler stalls
`Document.update`, `Document.cancel_exec`, everything.

Worse, a handler that raises loses the request with no trace the client can
correlate: `Protocol_Command.run` rewrites the exception into
`Output.system_message "Isabelle protocol command failure: …"`
(`protocol_command.ML:39-47`), which is `Markup.systemN`, never reaches
`Protocol_Handlers.invoke`, and carries no id and no arguments. The loop then
continues. **A request that fails this way hangs until the client times out.**

The distribution's one asynchronous protocol command,
`Build.build_session` (`build.ML:69-119`), is the model: decode arguments on the
protocol thread so malformed requests fail fast; fork; and make the reply
*unconditional*, with `Exn.capture_body` around the work and a second capture
around the error formatting (the source's own `(*sic!*)`), collapsing to a
crash code rather than ever skipping the reply.

### 3.7 find_theorems is costly — natively, and that is not this upgrade's business

`pretty_theorems` (`find_theorems.ML:465-497`) calls `all_facts_of`, which
materializes **every** local and global fact of the context
(`:386-397`), then filters and sorts them with `Par_List`
(`:305-318`) — i.e. the work is spread over the worker pool and the calling
thread joins. `find_theorems_limit = 40` (`etc/options:394`) is a **display cap
applied after the full scan** (`:415`). The cheap lazy path (`:418-420`)
requires both an explicit limit and `rem_dups = false`, but this server's
default is `allow_duplicates=False` (`tools/find_theorems.py:135`) ⇒
`rem_dups = true` ⇒ the full scan, always.

**This is Isabelle's native behaviour and this upgrade neither changes nor
compensates for it.** It is recorded here for exactly one reason: it fixes
*where* the work may run. Today the query executes as a document print exec on
the worker pool, safely off the protocol thread. Moving the query to a protocol
command must preserve that property (§5.2), or a single find_theorems call would
stall all PIDE traffic (§3.6).

Two capabilities already exist and must be **preserved, not reinvented**: the
client bounds its own wait and enriches the timeout error
(`lsp_client.py:1578-1580`), and it cancels on every exit path — including
`CancelledError` — by sending `PIDE/find_theorems_cancel` from a `finally`,
token-guarded so a late cancel cannot tear down the next query
(`lsp_client.py:1586-1592`). Prover-side, that cancel works today by removing
the overlay, which drops and cancels the print exec. A forked protocol-command
task is not reachable that way (§5.2), so an equivalent path must be rebuilt to
avoid a regression.

### 3.8 The ML prelude is a raw Poly/ML `--use`, and it is fragile

It runs after the heap, options and session resources are loaded and **before**
`Isabelle_Process.init ()` (`ml_process.scala:103-106`,
`isabelle_process.scala:19`, `:33`). Consequences:

- The raw Poly/ML compiler is used, **not** Isabelle's `ML_Compiler`: no
  antiquotations, no cartouches — plain SML over Pure's ML library
  (`mcp_prelude.ML:6-8`).
- **A failing `--use` is fatal**: the prover never comes up, which is why the
  server checks the file exists first (`language_server.scala:406-409`).
- **`Output.protocol_message` raises at load time** — `protocol_message_fn` is
  `protocol_message_undefined` until `init_protocol` installs the real one
  (`output.ML:80`, `:96-97`, `:112`; `isabelle_process.ML:141-142`), so the
  prelude must not call it while being loaded. **Afterwards it is a
  process-global channel callable from any thread**, which is what permits the
  forked reply of §5.2: the installing `setmp` wraps the whole protocol loop,
  not one command, and Isabelle itself calls it off the protocol thread in
  several places, including `Tools/debugger.ML:204` and `Build/build.ML:118`.
  (An earlier draft of this bullet said it "may only be called from inside a
  protocol command body". That was false, and taken as binding it would have
  pushed an implementer to route replies back through the protocol thread —
  reintroducing exactly the stall §3.6 exists to avoid. The prelude's own
  comment at `mcp_prelude.ML:63-66` also misstates why startup output must use
  `TextIO.print`; fix it while implementing, and record the corrected fact.)
- There is no document, theory or proof context at load time.

### 3.9 Correlation must be built; the existing probe does not generalise

`("function", <name>)` must be the **first** property or
`Protocol_Handlers.invoke` will not dispatch (`protocol_handlers.scala:44-49`,
`markup.ML:820`); function names are globally unique
(`protocol_handlers.scala:26-28`), so ours must stay namespaced
(`isabelle_mcp_*`). Nothing in `Protocol_Command`, `Protocol_Handlers` or
`Session` tracks pending requests — every handler keeps its own table. The
existing `Isabelle_MCP.ping`/`isabelle_mcp_pong` pair carries **no id** and
collapses into a one-shot promise (`language_server.scala:30-50`,
`mcp_prelude.ML:57-61`): a liveness probe, not a request/response protocol.

The model to copy is `Scala.Handler` (`scala.scala:292-351`): a `synchronized`
map from id to pending future, populated on request, drained in `exit` so no
caller hangs at shutdown. `State_Panel`'s `Synchronized(Map[Counter.ID, …])`
(`state_panel.scala:15-45`) is the same shape inside this fork.

`Prover.Protocol_Output.text` **throws unless the reply is exactly one chunk**
(`prover.scala:56-59`, `:67-68`). Replies must be single-chunk.

### 3.10 What survives, what does not

`Document.command_exec` consults the execution version, which is replaced by
`define_version` at the end of every `Document.update`, including a
perspective-only one (`document.ML:378-386`, `:908-910`). A finished command
nevertheless remains reachable indefinitely, because the entry is kept in the
common prefix whenever `Command.eval_running eval` holds
(`document.ML:674-676`) and an exec stays registered in `Execution`'s table from
the moment it starts until a new assignment supersedes it
(`execution.ML:100-109`, purged only at `protocol.ML:141`). Version pruning
cannot remove the execution version (`document.ML:491-496`).

It stops being reachable exactly when its exec is re-created: **an edit before
it in the same file**, or **an edit in an imported theory**
(`document.ML:846-848`).

**Measured, and one half of that is wrong for this client.** A caret move leaves
the finished command byte-identical — same command id, same exec id, same state.
But ANY edit to the same file re-creates every command id in it, whether the
edit is before or after the command: the position of the edit is irrelevant
because this client's `didChange` carries no range
(`lsp_client.py:1326-1330` sends `contentChanges: [{"text": content}]`), so the
node is re-parsed whole. The before/after distinction is a property of
incremental edits, which this client never sends. An edit in an imported theory
behaves as predicted from the other side: the ids survive, every exec is
re-created.

Two constraints follow. A command id must never be cached across calls — the
Scala side resolves it from the current snapshot per request, and the only
exposure left is the window between that resolution and the ML read, which §5.3
already requires to be a classified reply rather than a raise. And **exec**
identity, not command identity, says whether a finished command is still the one
that was read: an id can survive with a fresh exec underneath it, which is
exactly what an imported-theory edit produces.

> **Correction to a plausible misreading.** `vscode_model.scala:56-57`
> initialises `node_required` from `File_Format.registry.is_theory`, which asks
> whether a *file format* claims the node (`file_format.scala:28-29`) and is
> **false for ordinary `.thy` files**. The identically named `is_theory` used at
> `vscode_model.scala:105, 146, 233` is a different predicate —
> `Document.Model.is_theory = node_name.is_theory` (`document.scala:890`). So
> ordinary theory nodes are **not** required, and retention of finished commands
> rests on `eval_running`, not on requiredness. One research pass conflated the
> two; the conclusion is unchanged but the reasoning is not.

## 4. Design, part A — the evaluation target

### 4.1 The concept

**Evaluation target** — the file and line the current evaluation is advancing
toward. It already exists as `evaluation_state.file_path` +
`destination_line`; this part gives it a name, a lifetime, and a voice.

The target exists from the moment `evaluate_to` starts one until that evaluation
is observed complete or is cancelled. When no evaluation is outstanding there is
no target, and the stale leftovers in `evaluation_state` must never be reported.

### 4.2 The evaluation footer

Every tool result gains a **footer**: one line reporting the evaluation target
and the prover activity around it. Approved wording and rules follow; they are
user-facing text and may not be reworded without approval.

**Where it goes.** At the **end** of the result, as an appended text block. It is
ambient context, not the answer, so it must not push the answer down; and the
end is where the agent reads immediately before deciding what to do next. The
existing `UnicodeWarningMiddleware` (`server.py:104-130`) already appends a text
block to every successful result and already handles the non-`ToolResult` case,
so the footer rides the same mechanism. When both are present, the unicode
warning comes first and the footer is last: the footer is a fixed-format line the
agent learns to skim, and burying a rare, actionable warning underneath a
constant one would be worse than the reverse.

**Which tools.** The rules below are approved for the **six query tools** —
`isabelle_hover`, `isabelle_definition`, `isabelle_local_occurrences`,
`isabelle_goal`, `isabelle_find_theorems`, `isabelle_command_output` — and for
`isabelle_command_status` (§4.5). The three session tools
(`isabelle_launch`, `isabelle_terminate`, `isabelle_session_info`) never carry a
footer, since no evaluation is meaningfully in play.

`isabelle_evaluate_to`, `isabelle_evaluation_status` and
`isabelle_cancel_evaluation` **never carry the footer.** Decided. The first two
already state the same facts in their own bodies, in more detail (§4.6);
`isabelle_cancel_evaluation` keeps its present single-line output unchanged.

`isabelle_command_status` is itself a query tool and carries the footer on that
basis.

**The sentences.** One main sentence, optionally followed by suffix sentences,
space-separated on one line. Paths render relative to the project root, like
every other path in the formatters (`evaluation.py:858-870`).

Main sentences:

```
Evaluating towards MyTheory.thy:120.
Evaluation has arrived at MyTheory.thy:120.
Evaluation has completed up to MyTheory.thy:120.
```

Suffix sentences (proper singular/plural, not "command(s)"):

```
2 commands have been running for over 10s.
1 command failed.
Call isabelle_evaluation_status for details.
```

The trailing call-to-action appears when either count is non-zero.

**The six cases.**

| Situation | Footer |
|---|---|
| An evaluation is outstanding, the target line is not reached | `Evaluating towards F:L.` + suffixes |
| Outstanding, target reached, the evaluated prefix still busy (running or unjoined forks), or an import not done | `Evaluation has arrived at F:L.` + suffixes |
| Outstanding, target reached, **and the authoritative check confirms completion** | `Evaluation has completed up to F:L.` + `N commands failed. Call isabelle_evaluation_status for details.` when failures remain (D-C3); no other suffixes |
| Outstanding, but the decoration cache is stale (a recent edit) | `Evaluating towards F:L.` — **no suffixes**, because the counts would come from the same untrusted cache |
| No evaluation outstanding, but commands are running | suffixes only, no main sentence |
| No evaluation outstanding, nothing running | nothing at all |

The fifth row is deliberate: "Nothing is under evaluation." followed by "2
commands have been running…" contradicts itself, so the main sentence is dropped
and the suffix stands alone. It covers a state nothing reports today — prover
work the agent did not start, such as a re-evaluation triggered by a file save,
or forked proofs still settling after an evaluation finished.

**Cost.** The counts and the position judgement come from the local decoration
cache, which tracks unprocessed, running and error ranges and stamps running
ranges with an onset time (`processing.py:76-79`, `:179-189`, `:288-307`,
`lsp_client.py:1192-1211`). All of it is a pure in-memory read, so the footer
adds no round trip to a tool call.

**The one exception, and what it buys.** The completion sentence is the only
claim the local cache cannot support: the authoritative verdict also requires
every recursively imported theory to be done, which needs `PIDE/theory_status`
(`evaluation.py:131-151`). So **when, and only when, the local view says the
target is reached with nothing running or failed**, the footer performs that one
check. If it confirms completion, the footer ends the run and prints the
completion sentence.

This is bounded: no ordinary call pays for it, and on the happy path it fires
once per evaluation, after which the sixth row applies and the footer goes
silent. When the check does NOT confirm completion — an import still running, or
a target absent from the theory_status list — nothing changes state, so the next
query call runs it again. That costs no new round trip in kind:
`resync_and_check_freshness` already issues the same `theory_status` request
unconditionally, earlier in the very same call. It
also repairs an existing defect: today the outstanding-evaluation flag is
cleared only if someone calls `isabelle_evaluation_status`
(`evaluation.py:687-689`), so an evaluation that finished quietly keeps every
query tool blocked indefinitely. Note that observing completion is a genuine
**state transition the server must make somewhere**, not a display concern that
happens to mutate; today only one tool makes it, so it does not happen while
nobody polls.

Four implementation constraints follow, all decided.

1. **The footer is computed only on the tools that display it** — the six query
   tools and `isabelle_command_status`. It is not computed on
   `isabelle_evaluate_to`, `isabelle_evaluation_status`,
   `isabelle_cancel_evaluation`, or the three session tools. This is the rule,
   not an exemption carved out for particular tools: a computation that a tool
   does not display has no business running on that tool's path, still less
   mutating state there. Without it, a run completing on the very poll that
   `isabelle_evaluation_status` performs would have its flag flipped inside the
   shared entry function, and that tool's own body would then answer
   "No evaluation in progress." instead of "Evaluation complete, arrived at line
   N." Nothing is lost by the restriction: the seven footer-carrying tools carry
   the great majority of traffic, and `isabelle_evaluation_status` already
   performs this transition itself.
2. **The footer is computed before the guard** within a call. The guard reads
   the outstanding-evaluation flag, so it must see the fresh truth: after a
   confirmed completion, a query about a still-unprocessed position should
   auto-start a new evaluation (rule 3) rather than be refused (rule 4). The
   ordering holds naturally because the footer is computed in
   `_ensure_lsp_started()`, which runs first — but it must be written down so a
   later edit does not reorder it.
3. **The completion path goes through §4.8's `_finish_if_owner(client,
   evaluation, "complete")`** — write-once stamp, flag flip and
   `_cleanup_auto_opened` together, under `_evaluation_state_lock`. It must not
   call `complete()` alone. Every other terminal transition in the module pairs
   the flag with the cleanup, and skipping it here leaks: `_dependency_done`
   counts a *failed* dependency as done (`evaluation.py:126-127`), so completion
   is reachable with an auto-opened failed theory still open; afterwards both
   `evaluation_status` and `cancel_evaluation` short-circuit on
   `_no_pending_work`, so nothing ever closes it and it is pulled into
   `_relevant_files` for the rest of the session. §4.8's writer list therefore
   counts **four**, not three.
4. The check must **not** reuse `_build_status_snapshot`, which auto-opens
   failed theories as a side effect (`evaluation.py:274-291`) — a footer must
   not open documents.

Wording, because it decides what an implementer builds: the footer **flips the
outstanding-evaluation flag**; it does not clear the record. §4.8 requires
`current`, `file_path` and `destination_line` to survive a terminal transition,
since `cancel_evaluation:798` and `evaluation_status:652-661` read them
afterwards.

**Errors are the important case, and the middleware does not cover them.** When
a tool raises, `call_next` propagates and nothing is appended. Since the most
valuable place for the target is precisely a refusal, the refusal messages
compose their own status text rather than relying on the middleware. This also
keeps error text self-contained, which is what an agent reads first.

Computation happens once per call in `_ensure_lsp_started()`
(`server.py:133-145`), which every tool already calls, and is stashed for
whichever consumer needs it.

**Terminology.** Everything counted is a *command*. The existing messages are
inconsistent — "N command(s) running" but "M statement(s) failed"
(`evaluation.py:201-215`) — and are unified on "command" by this change.

**Plurals are computed, never parenthesised.** `command(s)`, `error(s)`,
`session(s)` and friends are banned from agent-facing text; the message picks
`1 command` or `2 commands` from the count. This is a rule for the whole server,
not just the footer. Current offenders, all to be fixed by this change or noted
where they are out of its scope:

- `evaluation.py:201-202` — `statement(s) still running` / `statement(s) failed`
- `evaluation.py:215` — `command(s) running`
- `lsp_client.py:1638` — `File has N error(s): …`
- `server.py:203-205` — `some session(s) in the dependency chain`,
  `Heap image(s) cannot be verified…`
- `unicode_guard.py:89` — `%d glyph(s) converted` (a log line, not agent-facing;
  fix for consistency only)

Not offenders: `lsp_client.py:431-436` and `utils/formatters.py:161-169` match
`(s)` inside text **emitted by Isabelle** (`Unfinished session(s):`,
`found N theorem(s)`), which must be matched verbatim and must not be touched.

**Which counts appear when.** Running commands are reported in every case that
has suffixes. **Failed commands are reported only while an evaluation is
outstanding**, never in the idle case (row five). Error decorations persist until
the file is edited and re-evaluated, so reporting them while idle would repeat
the same `M commands failed.` on every tool call indefinitely and train the agent
to ignore the footer entirely; while an evaluation is outstanding the same count
is progress information about that evaluation, and it stops as soon as the
evaluation does.

### 4.3 The guard, rewritten

`check_evaluation_guard` (`evaluation.py:816-851`) currently checks the global
`active` flag *before* looking at the requested position, and refuses
unconditionally. It becomes:

1. If the requested position is **processed and the decoration cache is fresh**,
   and the file is **already open**, serve it — regardless of `active`.
2. If the position is processed but still running, serve it with the
   incomplete-output note (unchanged behaviour).

   *Not "a forked proof" — measured.* `background_running1` is painted when
   `is_running`, i.e. `runs != 0` (`document_status.scala:240`): the command's own
   transition is executing. A command whose proof was FORKED has `runs == 0` and
   `forks != 0`, which `is_unprocessed` (`:239`) claims, so it paints
   `background_unprocessed1` instead. The two cases differ in what is available:
   a running transition has no state after it at all (`eval_result_state` gives
   `Fail "Unfinished lazy"`, §5.3), while a command with an outstanding fork has
   a final state and only an undecided verdict. Decoration cannot tell "never
   started" from "fork outstanding"; only the direct read can
   (`eval_finished` plus `Execution.snapshot`).
3. If the position is not yet processed and no evaluation is outstanding,
   auto-start one (unchanged behaviour, `evaluation.py:848`).
4. If the position is not yet processed and an evaluation **is** outstanding,
   refuse — with a message naming the target, the requested position, and the
   fact that the position has not been reached yet.
5. If the file is **not open**, refuse while an evaluation is outstanding.
   Opening it would send `didOpen`, which is a document-model change that
   globally invalidates decoration freshness (`processing.py:49-69`) and would
   perturb the running evaluation's completion detection. (Opening does not by
   itself cause execution — a node with no caret gets an empty perspective and
   no worker task, `vscode_model.scala:101-137`, `document.ML:551`.)

**Corrections to the rules above, all decided.**

*The `unknown` outcome is handled by waiting, not by refusing.* When the
decoration cache is inside the post-edit grace window and the tracker is
initialised with no unprocessed or running range covering the position, the
guard **sleeps out the remaining grace — bounded by `DECORATION_GRACE`, 2.0 s by
default — and re-tests**, then decides on the fresh answer. The tracker's wait
loops already wake at expiry (`processing.py:270-284`), so no new machinery is
needed. The agent gets a definite answer after at most two seconds instead of
either a spurious full re-evaluation or a refusal it cannot act on.

This is the adopted fix for a symptom that would otherwise be common: the guard
today conflates "cache not fresh" with "not processed" — `line_reached` returns
`False` for both (`processing.py:288-300`) — and falls through to a full
`evaluate_to`. Combined with moving the `open_document` call after the guard
(below), it removes the reported problem entirely.

**Rejected, and recorded so it is not retried: making the edit clock per-file
for a first `didOpen`.** The proposal was to invalidate only the opened file's
cached decorations, on the ground that a first open changes no file's content.
The premise is false. "First open" is a fact about the **client**; the server
routinely already holds a model for a node the client never opened, built from
disk when it was loaded as a dependency (`vscode_resources.scala:236-272`), and
`didOpen` is diffed against that model's text (`vscode_model.scala:160-181`).
The texts differ as a rule, because `open_document` runs the unicode guard,
which rewrites the file to Isabelle ASCII (`unicode_guard.py:58-99`) while the
server read the original. The resulting edit propagates: an importer A gets
`imports_result_changed` and **every one of its commands is re-created
unprocessed** (`document.ML:838-846`), so a query against A's still-trusted
cache would be served from a version that no longer exists, and
`isabelle_evaluate_to A` would report `complete, no errors` from the same stale
cache. That is the mirror image of the hole the global clock was introduced to
close (commit `6eaf308`, "cross-file invalidation (edit A, immediately evaluate
B-imports-A → stale 'complete')"). Two related gaps found while checking, each
deserving its own ticket: `close_document` does not bump the clock although the
server's `close_model` → `sync_models` can change that node's content
(`language_server.scala:230-236`); and no test covers a `didOpen` of a node the
server already holds as an external dependency — that is the first test to write
if anyone ever touches this invariant.

*The refusal below is the fallback for a position that is still `unknown` after
the wait.* It must never auto-start (that relocates the caret on a guess) and
must never be phrased as "not reached yet" (which may be false for a line that
finished minutes ago). Approved wording:

```
Cannot tell whether MyTheory.thy:42 has been evaluated: a file changed a
moment ago, so the processing state is not yet trustworthy. Retry in a few
seconds.
```

"**a** file", not "this file": the distrust comes from a *global* edit clock
(`processing.py:42-46`), so the change that armed it may have been to a
different file, and naming this one would be a false statement.

*An interrupted position must not be served as `processed`.* An interrupted
command emits `failed`, `finished`, `canceled` (`command.ML:243-248`), which
renders as `Rendering.Color.canceled` and is published as decoration type
`background_canceled` (`vscode_rendering.scala:215`) — a type absent from
`_TRACKED_TYPES` (`processing.py:76-79`) and therefore discarded. With no
unprocessed and no running range covering the line, the helper would answer
`processed`, so the guard would serve interrupted output as finished while
`isabelle_command_status` printed the definite word `processed` — and, because
the same command also carries `failed`, the very same call's footer would say
`1 command failed.` Fix: add `background_canceled` to `_TRACKED_TYPES` with an
accessor, give the helper a fifth outcome, and have the guard **serve with an
explicit note** rather than refuse — the output exists and is useful for
deciding what to re-run, it is only incomplete. Approved note:

```
The evaluation of the command at MyTheory.thy:42 was interrupted; its output
may be incomplete.
```

**Measured, and narrower than the paragraph above claims.** `Markup.CANCELED`
has exactly one emitter (`command.ML:248`) and fires only for a MAIN-THREAD
transition that returns NONE with no error message. A cancelled FORKED proof
takes another path entirely (`execution.ML:170-177`): it emits `failed`,
`finished` and `Markup.bad`, never `canceled` — and `Markup.Bad` outranks
`canceled` in `Rendering.background`, so `background_bad` is what arrives. A live
cancel on this machine produced `background_bad` and never once
`background_canceled`.

Two consequences. The `background_canceled` outcome is correct where it fires
but is not the common case, so treat it as unexercised. And the case that DOES
happen — a killed fork — still answers `processed`, because `position_state`
does not consult `_bad`. That hole cannot be closed from this channel: a killed
fork and an ordinary erroring command are both `failed` + `Markup.bad`, and
reporting an erroring command as `processed` is correct (it finished; its output
is what the agent wants). `force_interrupt` compounds it by appending a space to
line 0 right after the cancel, which invalidates the whole node and re-creates
the cancelled command.

**The three refusals, approved.** Positions render as `file:line`, relative to
the project root, the same form the footer uses.

```
MyTheory.thy:42 has not been evaluated yet. Evaluating towards Other.thy:120. Call isabelle_evaluation_status to check progress.

MyTheory.thy has not been opened yet, and opening it would disturb the evaluation in progress. Evaluating towards Other.thy:120. Call isabelle_evaluation_status to check progress.

This query cannot run while an evaluation is in progress. Evaluating towards Other.thy:120. Call isabelle_evaluation_status to check progress.
```

The third is the blanket refusal §4.4 keeps for the two caret-moving tools. It
names no position because the position is not why it is refused, and it says
nothing about the caret: the agent has no other exposure to that concept, so
naming it would explain a mechanism it cannot act on. It disappears with part B.

The running note is likewise `file:line`:

```
The command at MyTheory.thy:42 is still being executed; its output may be incomplete.
```

*Rule 5 cannot fire as the code stands, and the open must move.* All six query
tools call `client.open_document(file_path)` on the line immediately **before**
`check_evaluation_guard` (`tools/goal.py:22`/`:24`, `hover.py:42`/`:44`,
`definition.py:26`/`:28`, `local_occurrences.py:25`/`:27`,
`command_output.py:30`/`:32`, `find_theorems.py:141`/`:143`), and those six are
the guard's only callers — so the file is always open by the time rule 5 runs,
and the didOpen whose side effects rule 5 exists to prevent has already been
sent. Stage 1 step 3 therefore also moves that call: the open/not-open decision
is read from `client.open_documents` **before** any `open_document`, and only
the paths permitted to open do so (the rule-3 auto-start path opens via
`evaluate_to` anyway, `evaluation.py:560`).

Step 1 requires distinguishing "not processed" from "cache not fresh", which
`line_reached` deliberately conflates into `False` (`processing.py:295-296`).
A new helper must therefore check freshness *first* and report a distinct
"unknown" outcome; reusing `line_reached` blindly would report a stale cache as
"not processed".

This helper — position → one of processed / running / not processed / unknown —
is pure local computation, no I/O, and is the same helper the debugger work will
need later.

### 4.4 What this does and does not unblock

With §4.3, the four position-explicit tools become usable during an evaluation.
The two caret-moving tools still cannot be released, because releasing them
would put two writers on the global caret with no arbitration: the evaluation
sets the caret without holding `_caret_lock` (`evaluation.py:584` →
`lsp_client.py:1106-1118`), while the query paths hold it
(`lsp_client.py:1464`, `:1562`). Today the guard's blanket refusal *is* the
mutual exclusion. That is what part B removes.

### 4.5 New tool: `isabelle_command_status`

The position-state helper of §4.3 is also exposed as a tool, so the agent can ask
directly instead of discovering the answer by tripping over a refusal.

**Parameters.** `positions`: a list of `{file_path, line}`. No columns, per the
repository's convention.

**Result.** Text, one line per requested position. A line may cover several
commands — `lemma foo: "P" by auto` is two — so a position is answered by the
state of every command overlapping that line. When they agree, the shared state
is simply reported; only when they **differ** is the per-command breakdown
printed, because that is the only case where one state cannot speak for the line.

```
MyTheory.thy:42 — processed
MyTheory.thy:43 — 2 commands, states differ
  processed        have "P x" by blast
  running for 12s  by auto
MyTheory.thy:88 — running for 31s
MyTheory.thy:80 — not evaluated
Other.thy:7 — unknown, retry in a few seconds
Missing.thy:3 — file not open
```

Text, not a structured model: the result is a position-to-state table that is
typically requested in bulk, and JSON would cost several times the tokens for
the same information.

**State vocabulary** — approved, fixed, and used nowhere else with another
meaning: `processed`, `running for Ns`, `not evaluated`,
`cancelled, re-evaluate to get a result`, `unknown, retry in a few seconds`,
`no command`, `file not open`.

`unknown` means the decoration cache is inside the post-edit grace window
(`processing.py:176-177`) and therefore cannot be trusted — hence the retry
hint, which is the one place a bare state word would leave the agent stuck.
`cancelled` uses that word rather than "interrupted" because the agent has
already met it in `Evaluation cancelled.`, and one thing must not have two
names; its hint is needed for the same reason `unknown`'s is. `no command`
covers a blank line, a line wholly inside a comment, and trailing whitespace at
end of file, and takes no hint because there is no next step — nothing is there.
Command text in a breakdown is the command's first line, truncated.

A command spanning several lines is attributed to every line it covers, so
asking about a line in the middle of a proof reports that proof's command. That
is the intended reading: the question is "what is the state of the command
covering this line".

**Implementation — server-side enumeration, and therefore stage 4.** An earlier
draft claimed enumeration needed no Scala change: walk from the start of the line
by repeatedly calling `PIDE/command_at_position` and advancing to the returned
end position. **That does not work, verified.** `Outer_Syntax.parse_spans`
buffers ignored tokens separately and unconditionally
(`outer_syntax.scala:190`), pulling them into the current command only when the
*next* token is an ordinary one (`:196`); when the next token is a command
keyword it flushes first, shipping content and ignored as **two spans**
(`:185-186`). So `lemma foo: "P x"` newline-indent `by auto` is three spans, the
middle an `Ignored_Span`, and each becomes a real `Command`
(`thy_syntax.scala:250-253`) for which the fork returns `None`
(`language_server.scala:643`, literally `if (command.is_ignored) None`). Column 0
of the indented line falls *inside* that ignored span, so the first probe already
returns `None`, and `None` carries no range — the walk stalls at the boundary
between every pair of commands.

The enumeration therefore moves to the server, in one round trip: convert the
line's start and end to offsets with the existing `rendering_offset`
(`language_server.scala:208-212`), iterate with
`snapshot.node.command_iterator(offset)` (`document.scala:281-285`), skip
`is_ignored`, and take every command whose start precedes the line's end,
returning each one's range and source. This handles the cases the walk could
not: a blank or comment-only line yields an empty list; a command spanning
several lines is yielded first by the iterator when the queried line is in its
middle; a file opening with a comment block no longer stalls at offset 0. The
request takes one file and a list of lines, so the Python side groups the
requested positions by file and issues one request per file — typically one or
two for the whole call, however many positions were asked about.

Python's side is unchanged: intersect each returned range with the local
decoration cache to get its state, which costs no request at all.

**Decided: the whole tool ships in stage 4**, behind the jar rebuild, rather
than a reduced line-only version in stage 1 that would later change shape and
need a second approval. Note the split this makes: the *internal* position-state
helper of §4.3 stays in stage 1 — it judges by line from the decoration cache
and never enumerates commands, so the guard and the footer are unaffected.

This tool carries the footer (§4.2).

### 4.6 The three evaluation tools' own output

These tools render an evaluation view: an optional heap-warning block, a message,
then one section per relevant file (`evaluation.py:911-922`). The same rendering
also becomes the **error text** raised by the six query tools when the guard
auto-starts an evaluation that does not complete (`tools/goal.py:24-26`), so this
layout serves both.

None of these three carries the footer (§4.2), so the leading sentence has to
name the target itself.

**The leading sentence — approved, and identical to the footer's.** The same
three sentences, produced by the same helper so the two can never drift:

```
Evaluating towards MyTheory.thy:120.
Evaluation has arrived at MyTheory.thy:120.
Evaluation has completed up to MyTheory.thy:120.
```

Here the sentence stands **alone** — none of the footer's suffix sentences
follow it. The counts they carry are already below, with more detail and with
line numbers: running commands in the nested `running:` rows, failures in the
`errors:` row. Saying them twice is what the nested layout exists to avoid.

This replaces today's count-folding sentence, `Evaluation arrived at line 120
with N statement(s) still running and M statement(s) failed`
(`evaluation.py:200-203`), which names no file and is one of the `(s)`
offenders. Nothing is lost: both counts are readable, per line, in the sections.
§4.8's reference to "the ordinary `_complete_message`" decides *which case is
reported*, not its wording, and is unaffected.

**Approved layout.** Detail is nested under the row it belongs to, so nothing is
stated twice:

```
Evaluating towards MyTheory.thy:120.

MyTheory.thy:
  running:
    line 88 (14s) by (auto simp: field_simps)
    line 95 (12s) by blast
  pending: lines 89-120
  errors: line 45
  warnings: line 12

Aux.thy:
  running:
    line 12 (11s) lemma helper: "x + 0 = x"

Call isabelle_evaluation_status to check progress.
```

Rules:

- **Line spans say "line"/"lines".** Bare numbers today
  (`evaluation.py:872-873`, `:887-901`) make `warnings: 12` indistinguishable
  from "12 warnings". Singular and plural are computed.
- **A row is nested only when it carries information a line range cannot.**
  `running` does — the elapsed time — so it nests. `errors`, `warnings` and
  `pending` do not, so they stay on one line. This is why the treatment is
  asymmetric: the reason is information content, not preference.
- **Nested detail belongs to its own file section**, and does not repeat the file
  name — only `line N (Ns) <snippet>`.
- **Only commands past the 10s threshold get a nested detail line**; below it the
  row shows the range alone. The threshold is one named constant shared with the
  footer so the two can never diverge.
- **Failures never carry an elapsed time** — a failure is a terminal state, and
  no timestamp exists for it; only running ranges carry an onset.
- **A snippet need not be a whole command.** It is the text of the range,
  first line, truncated. This matters because the error decorations are a
  line-deduped union of two markup kinds, so slicing them need not yield exactly
  one command.
- **The trailing call to action appears only when there is something to watch**:
  any command past the threshold, or any failure. Otherwise it is omitted.

**The file set does not change.** `_relevant_files` (`evaluation.py:437-454`)
already covers the target, plus dependency theories auto-opened because
`theory_status` reported them not-ok, plus every other **open** document whose
tracker currently holds a bad, overview-error, overview-warning or running range.
Unprocessed ranges are deliberately not part of that test — an open but never
evaluated file is entirely unprocessed, so counting it would drag every opened
file into the output — and `pending` is computed for the target only, clipped to
the prefix before the destination (`_snapshot_files:465-469`). A file with none
of the four kinds renders as `<name>: clean`; a file with no decoration at all
falls back to `theory_status` counts, e.g. `Deps.thy: 3 errors (no line info)`.

**Errors show a line span and no text — and that is a deliberate, documented
decision, not an oversight.** In progress an error appears only as a line span:
no command text, no message. The message text *is* cached client-side
(`textDocument/publishDiagnostics` stored per file with position, severity and
text, `lsp_client.py:826-838`), but the evaluation result reads decoration
only. The rationale is recorded in `docs/TECH_NOTE.md` §5.2 and §6.6, added by
the very commit that removed diagnostics from the snapshot (`2c16611`):

- **Absence of a diagnostic is ambiguous; absence of a decoration is not.**
  `evaluate_to` was observed returning `status: complete, errors: []` while the
  same result's `theory_status` already showed `failed: 1` — the error's
  `publishDiagnostics` had not arrived yet. Rather than gate completion on
  diagnostics settling, the snapshot was moved to decoration, which has **no
  limbo window**: a pending fork stays covered by an unprocessed/running
  decoration until it flips to `background_bad` in the *same* push, so reading
  decoration at the frontier can never report a false "clean" (`CHANGELOG.md`
  0.1.4 records the same verification).

  Be precise about what lags what: diagnostics are **not** slower than
  decorations. Both are written by the same `flush_output` pass, behind the same
  `vscode_output_delay` debounce (default 0.5 s), computed from the same
  rendering, with diagnostics written first and the channel serialised
  (`vscode_resources.scala:300-330`, `:316`, `:318-320`,
  `language_server.scala:289-296`, `channel.scala:70-74`). The original race was
  against `theory_status`, which is a **request** and therefore immediate. What
  actually distinguishes the channels is expressiveness: the decoration channel
  has explicit `background_unprocessed1` / `background_running1` states, so
  "nothing reported yet" is distinguishable from "clean", whereas an empty
  diagnostics list is not.
- **Decoration is strictly richer for this purpose.** Plain warnings exist only
  on the decoration channel — diagnostics are roughly errors plus legacy
  warnings — and `sorry` is invisible to both diagnostics and theory_status,
  showing up only as `background_bad`. Meanwhile `text_overview_error` matches
  the diagnostics error set **line for line**, so nothing is lost on errors.
- **Diagnostics carry no document version**, so staleness cannot be detected,
  and they are published only when the list *changes* — a clean dependency sends
  nothing at all, making an empty cache indistinguishable from "no errors".

Consequences for this upgrade: the error **line spans stay decoration-derived**,
and nothing here changes that. If message text is ever wanted inline, the only
safe shape is *decoration decides what and where; diagnostics supply text
best-effort and are omitted when absent* — never letting diagnostics influence
classification or counts. That is out of scope for now.

Two small repairs worth making while here: cross-reference `TECH_NOTE.md` §5.2
from the docstring at `evaluation.py:264-270`, which states the what and points
only at `_build_file_snapshot`; and note that the removal commit's own message
buries the change as a folded-in working-tree edit, which is why the reason was
hard to find.

### 4.7 Defects found while investigating

Not caused by this upgrade, but adjacent to it and cheap to fix here.

**Every diagnostic arrives without a severity, and two call sites assume
otherwise.** The severity map keys on `Markup.LEGACY` / `Markup.ERROR`
(`vscode_rendering.scala:44-47`), but the lookup runs against the *result*
message, whose name has already been rewritten to `legacy_message` /
`error_message` (`command.scala:352` → `protocol.scala:198-199`,
`markup.scala:608-617`). The keys never match, so `severity` is `None` and the
field is omitted from the JSON. The repository already measured this without
tracing it: `tests/integration/test_file_sync_e2e.py:73` carries the comment
"Isabelle proof failures carry severity=None" and matches on message text
instead. Consequences:

- `lsp_client.py:1626-1637` filters `d.get("severity") in (1, 2)` and is
  therefore **dead code** — it always finds zero errors, so the enriched
  "File has N errors: …" timeout message never fires and the agent gets the bare
  timeout text. (This path is on the proof-state and find_theorems timeouts,
  both of which Part B replaces, so it may simply disappear.)
- `tools/hover.py:103` defaults `diag.get("severity", 1)`, so **every** attached
  diagnostic is labelled an error — a legacy warning shown on hover is reported
  to the agent as an error. `isabelle_hover` is one of the six tools in scope, so
  fix it here.

**A closed file keeps receiving diagnostics.** `close_model` only flips the model
to external, it does not remove it (`vscode_resources.scala:196-201`), so the
server goes on publishing diagnostics for a closed document while decorations
are cleared. The client drops its caches on close (`lsp_client.py:1126-1128`), so
a late publish would silently repopulate `diagnostic_cache` for a file with no
`open_documents` entry.

**Measured, and the guard was rejected on the evidence — do not add it.** The
late publish is real but does not follow from the close itself: a bare
`close_document` produced no publish in 100+ seconds. What produces one is
editing the closed file on disk, which the server's own File_Watcher picks up
through `sync_models` for external models. And that publish is not stale — it is
the server's current rendering of the re-read file, fresher than anything the
client held.

The obvious guard (ignore a publish whose file is not in `open_documents`) was
measured and costs more than it buys:

- The cache entry for a dependency is **load-bearing**. Every dependency theory
  with a problem publishes before it is ever opened
  (`publish_full` computes diagnostics regardless of `node_visible`,
  `vscode_model.scala:212-217`), and that entry is why the evaluation's
  auto-open of a failed dependency (`evaluation.py`, `_build_status_snapshot`)
  returns instantly. With the guard, no publish follows the didOpen either —
  `change_model` reuses a model whose `published_diagnostics` already match, so
  `flush_output` emits nothing — and `wait_for_first_diagnostics` burns its full
  timeout: a deterministic **+2 s per auto-opened failed dependency**, inside the
  evaluation's own poll budget.
- `_enrich_timeout_error` would then take its `if not diags` branch and tell the
  agent "No diagnostics received — file may not have been processed" about a file
  that was processed and did report errors.

Path keying and registration order were both checked and are *not* hazards:
published URIs re-print the client's own `st.models` key, and `open_document`
registers the document before it awaits the didOpen, with no checkpoint between.

The cache's actual contract is coherent: *the server's most recently published
diagnostics for a file, whether or not the client currently holds it open*. The
one genuine defect left is narrow — a stale entry surviving into a later re-open,
where `isabelle_hover` could show it against content the server has not caught up
with, the same window any open document has between a didChange and the next
publish. If that is ever worth closing, close it at the **reader** (have hover
ignore an entry older than the document's last didChange), never at the writer.

**Dead branch in the fork.** `Markup.BAD` is not in `diagnostics_elements`, so the
`Markup.Bad` case at `vscode_rendering.scala:145-146` is unreachable. Harmless;
remove or comment when next editing that file.

**Messages name tools that do not exist — deferred, do not fix here.** Four
agent-facing strings refer to tools without the `isabelle_` prefix every real
tool carries, so an agent following the instruction would call a name the server
does not expose:

- `evaluation.py:216`, `:217` and `:839` — `Call evaluation_status to check progress.`
- `evaluation.py:556-557` — `Call cancel_evaluation to cancel, or evaluation_status to check progress.`

The real names are `isabelle_evaluation_status` and `isabelle_cancel_evaluation`.
**Explicitly deferred**: this is to be discussed as its own topic after the plan
in this document is executed, not folded into it.

*One of them survives, and it was rewritten for a different reason.* Three of
the four strings were rewritten out of existence by stage 1
(`_in_progress_message` is gone, and the guard's refusals are new text using the
prefixed names). The fourth, `evaluate_to`'s "an evaluation is already in
progress" error, was reworded afterwards to say what unblocks the agent rather
than offer two calls without saying which helps:

```
An evaluation is already in progress. Call cancel_evaluation to cancel so you can request another evaluation.
```

That is approved text and pinned by a character-for-character test. It still
names `cancel_evaluation` unprefixed — the real tool is
`isabelle_cancel_evaluation` — so the deferred topic is now this one word, in
this one string, and nothing else.

**An unused fast path exists.** `PIDE/decoration_request` →
`force_decorations` (`vscode_resources.scala:367-373`) pushes decorations
immediately, bypassing the 0.5 s debounce. This client never sends it. It is not
proposed here — note that it discards the updated model copy, so the server's
published-decorations baseline is not advanced by it — but it is the obvious
lever if decoration latency ever becomes a problem for the footer.

### 4.8 A stopped evaluation must say why it stopped

**Approved.** This is a correctness fix, not a wording change, and it is the one
defect in this document that produces a confidently wrong answer today.

#### The defect

The server keeps exactly one record of the evaluation in flight: a boolean, the
target file and line, and the set of dependency files it auto-opened
(`evaluation.py:224-241`). `evaluate_to` deliberately holds no lock while it
waits — otherwise a cancel could never interleave — so it polls that boolean
(`:511-512`).

Three different writers clear it, and the boolean cannot say which:

- `isabelle_cancel_evaluation` — the agent cancelled (`:807`);
- `isabelle_evaluation_status` — it observed the evaluation reach its target and
  recorded completion the only way the server records it, by clearing the flag
  (`:687-689`). **The evaluation succeeded.**
- session teardown — `isabelle_terminate`, or `isabelle_launch` restarting the
  prover, via `_clear_session_state` (`lsp_client.py:637-639`).

The loop guesses "cancelled"; the message selector distinguishes only "complete"
from everything else (`:626-631`), so even that guess renders as
`Evaluation in progress.` And that string is what the six query tools raise as
their error text (`check_evaluation_guard:848-851`), so the agent is told
"in progress", follows the advice to poll `isabelle_evaluation_status`, and is
told "No evaluation in progress." Two tools contradicting each other about the
same instant, with the successful case reported as unfinished.

Two consequences beyond the message: the loop's early return of empty lists
poisons the file sections (`_snapshot_files` runs with no theories, `:615`), and
— because nothing holds a lock during the wait — a second `evaluate_to` can
start once the flag is clear, after which the first call's tail still clears the
flag and **closes the second evaluation's auto-opened documents**
(`:590`, `:607-608`, `:612`, `:619-621`, `:632-634`).

#### The fix: one handle per evaluation

`start()` mints a small object and keeps it in the shared record; the
`evaluate_to` call keeps its own reference. The object reference **is** the
identity, and its single field records **why** the run ended:

```python
@dataclass(eq=False)
class Evaluation:
    outcome: str = ""          # "" | "complete" | "cancelled"
```

`EvaluationState` gains `current: Evaluation | None`, an `owns(evaluation)`
predicate, and a private write-once `_stamp()`. `complete()` and `cancel()` keep
their exact zero-argument signatures — the three external writers are unchanged
— and each gains one stamping line. Write-once matters: a later cancel of a
lingering fork must not rewrite a finished run's story.

**`current` is deliberately never reset.** A run that ended with no successor
must still recognise itself as the owner and still run its cleanup, which is what
today's code does unconditionally. This is the same discipline `file_path` and
`destination_line` already follow — their stale values are relied on by
`cancel_evaluation:798` and `evaluation_status:652-661`.

A new `_finish_if_owner(client, evaluation, outcome)` replaces today's
`cancel()`/`complete()` + `_cleanup_auto_opened` pairs at four sites; it returns
immediately when the run no longer owns the state. The ownership test and the
flag flip are synchronous with no await between them, so
`_cleanup_auto_opened`'s atomicity guarantee and the "reset first, then close"
cancellation discipline are untouched.

The wait loop gains an `evaluation` parameter beside the existing `state` (kept,
so the cancellation-safety test stubs need signature changes only, not body
changes). Its exit becomes `if evaluation.outcome or not state.active`, returns
`evaluation.outcome or "cancelled"`, and carries out the last observed theories
plus a freshly read `client.get_all_running_commands()` — a plain synchronous
read that issues no request — instead of empty lists.

After the try/except, `if evaluation.outcome: status = evaluation.outcome` makes
the stamp authoritative. This closes a window nobody had noticed: `active` is
sampled only at the top of the loop body, while the loop also returns
`in_progress` from `:525` and `:528` without re-reading it, so a terminal
transition landing inside an iteration's awaits was reported as "in progress" for
a run that had already stopped.

**Defect 2 is in scope and fixed**, at all five write sites. Once the handle
exists for the message defect, the guard is the same predicate, and four of the
five sites *collapse* into `_finish_if_owner` rather than growing a line — the
net line count goes down. The two defects are the reader's half and the writer's
half of one sentence: *only the run that started this state may end it, and the
outcome belongs to the run that was ended.*

#### Approved agent-facing text

- Stopped by `isabelle_cancel_evaluation`, **or by a session teardown** →
  `Evaluation cancelled.` — the identical sentence `isabelle_cancel_evaluation`
  already prints, shared from one module constant so the two tools cannot drift.
  A teardown is a cancellation; no third outcome word is minted.
- Completed, but observed by a concurrent `isabelle_evaluation_status` → the
  ordinary `_complete_message`, e.g. `Evaluation complete, arrived at line 120.`
  This is the case the fix exists for.
- **Demotion, accepted:** `Evaluation abandoned: the file differs from its
  precompiled copy…` is shown only when a heap-divergent run genuinely times out
  on its own, no longer when another actor stopped it — claiming the heap
  divergence stopped a run that a cancel stopped would fabricate a cause, which
  is the defect class being removed. No advice is lost: the heap warning banner
  still prints above the message (`evaluation.py:917`,
  `lsp_client.py:505-516`), and it already states that the theory is precompiled,
  that Isabelle ignores edits to it, that every evaluation and query on it will
  fail, to treat it as read-only, and how to relaunch.

Explicitly rejected wording: anything asserting "No evaluation is in progress."
That is a claim about the whole world which a single run cannot make — a
lingering fork or a newly started evaluation both falsify it, recreating the very
contradiction being removed.

#### Also approved

`_cleanup_auto_opened` binds the auto-opened set **object** once before its loop
and discards from that, rather than re-reading the attribute each iteration
(`:487-494`): `start()` rebinds the attribute to a fresh set, so a cleanup
overlapping a newly started run can otherwise discard from the new run's set.
That function already has five call sites (`evaluation.py:608`, `:620`, `:634`,
`:689`, `:808`) and this change adds the footer as a sixth, so the one-liner is
taken now.

#### Rejected alternative, recorded so it is not retried

Deriving the outcome at exit instead of recording it — when the loop sees the
flag cleared, inspect the file's current state and decide "finished or cut
short?". It adds no data, and it is unsound: the post-edit grace gate it relies
on expires after `DECORATION_GRACE` (2.0 s, `processing.py:24-40`) while the loop
parks for up to 5 s (`:535`), so the gate contributes nothing in the ordinary
case; and past it, `_prefix_quiet` consults only unprocessed and running ranges
(`processing.py:179-189`), never `background_bad`, where killed commands may
land. A cancelled run would then look quiet and be reported **complete**, after
which `check_evaluation_guard` waves all six query tools onto interrupted
output. That trades today's misleading-but-conservative message for a confident
false success, on a premise about Isabelle's behaviour that nobody has measured.

#### Out of scope, each deserving its own ticket

Serialising overlapping `evaluate_to` calls (would need the lock held across the
wait, which self-deadlocks — this design makes the loser harmless, not
impossible); a pre-existing busy-spin when the target line is reached but an
import is not done (`:143-148` stays False while the tracker wait returns
immediately); and `start()` rebinding a non-empty auto-opened set rather than
closing it (reachable, and measured: a cancel can empty the set while the wait
loop is parked inside `_build_status_snapshot`, which then adds a dependency to
it; the next `start()` rebinds the attribute and the late `_finish_if_owner`
returns False, so nothing closes it. One stranded open document per occurrence).

### 4.9 Settled, not to be reopened

The footer's tool scope (§4.2). `isabelle_cancel_evaluation`'s output stays as it
is. The evaluation result's file set stays as it is (§4.6).
`isabelle_evaluation_status` omits the "call isabelle_evaluation_status" sentence
from its own output — it is the tool being called, so there is no next step to
point at — while `isabelle_evaluate_to` and the guard's refusals keep it.

Deliberately **not** unified: the evaluation result keeps `pending` and
`isabelle_command_status` keeps `not evaluated`. They come from the same
decoration but describe different things — a range of lines within the evaluated
prefix in one case, the state of the commands at one position in the other — and
they appear in different tools' output, so the two words do not collide in
practice. `file not open` is the §4.5 state word.

Path rendering stays inconsistent and that is accepted: after §4.6's nested
layout removes the running-command detail lines, the only remaining mixture in
one result is the heap warning banner, which is rare enough not to be worth the
churn.

## 5. Design, part B — position-explicit proof state and find_theorems

### 5.1 Shape

Two new ML protocol commands, defined in the prelude, plus one to cancel:

- `Isabelle_MCP.proof_state` — arguments: request id, node name, command id.
- `Isabelle_MCP.find_theorems` — arguments: request id, node name, command id,
  limit, allow-duplicates, query.
- `Isabelle_MCP.cancel_query` — argument: request id.

Each replies with a single-chunk protocol message whose **first** property is
`("function", "isabelle_mcp_query_result")` and whose second is the request id.

Three new LSP messages wrap them: `PIDE/proof_state_at_position` and
`PIDE/find_theorems_at_position`, both taking a text-document position and both
answering with the rendered HTML the Python parsers already consume, plus
`PIDE/query_cancel` carrying the request id.

**The LSP handler must never block the language server's loop.** That loop is
single-threaded — read one message, `handle` it inline, only then read the next
(`language_server.scala:781-792`) — and `channel.read()` is a blocking stdin
read. A handler that waited for the prover would queue *all* further LSP input
behind it, including the `PIDE/theory_status` request the evaluation poll depends
on, `PIDE/cancel_execution`, and document sync; and because §4.3 releases the
query tools to run *during* an evaluation, that collision is the normal case,
not an edge one. So the handler registers the request id and returns
immediately, and the `ResponseMessage` is written later from the protocol-handler
callback, or on the timeout. Read §3.9's "drained in `exit` so no caller hangs"
accordingly: it means the table is emptied and pending requests are answered with
an error, not that anything joins. The one existing blocking handler in the fork
(`language_server.scala:400-417`) is safe only because it runs at `initialize`,
with nothing else in flight; it is not a model to copy.

`PIDE/query_cancel` exists to keep a capability the client has today, not to add
one: the Python side cancels on every exit path including `CancelledError`
(`lsp_client.py:1586-1592`), and §3.7 requires that not to regress. The token
guard disappears — the request id replaces it — but the send-on-every-exit-path
discipline stays. The Scala per-request timeout fires the same cancel on expiry;
a cancel for an unknown or already-finished id is a silent no-op, and the table
entry is removed when the task replies, so the table cannot leak.

No overlay, no perspective change, no caret movement, and **no document update
at all**.

**The reply, exactly.** Settled while stage 3 wrote the prelude, and now the
contract the Scala adapter must decode. One protocol message per request, one
chunk per message:

```
properties  ("function", "isabelle_mcp_query_result")   (*must be first*)
            ("id", <request id>)
            ("status", <one of the nine words below>)
            ("comment", "true")   (*present only when status = ok*)
            ("forked", "true")    (*present only when status = ok*)
chunk       status = ok      the markup-wrapped result strings of §5.4
            status = failed  the error text, one string
            otherwise        empty
```

The nine statuses, and the §5.3 row each renders as: `ok` (serve it), `comment`
and `forked` (serve it with the corresponding note), `undefined`, `unfinished`,
`interrupted`, `no_proof_state`, `no_context`, `failed`, `cancelled`, `crashed`.

**ML sends no English and no position.** Every approved reply in §5.3 names a
`file:line`, and ML has no line to name (§3.9's fact 2), so the status word
travels and the sentence is rendered from it.

**The sentence is rendered in Python.** An earlier draft of this paragraph said
Scala; that was wrong, and stage 4 corrected it while wiring the two together.
Every other agent-facing string in this project lives in Python, where it is
unit-tested character-for-character in the style of `TestCompleteMessage` and can
be changed without rebuilding the jar; Python also already formats `file:line`
everywhere, and the two tools that differ in wording — "Reading the proof state
at …" is wrong for a find_theorems query — are two separate Python modules. So
the LSP reply carries four fields and no prose: `status`, `comment`, `forked`,
and `content` (the rendered HTML when the status is `ok`, the prover's error text
when it is `failed`, empty otherwise).

**Two statuses exist that the prelude never sends**, because only this side can
observe them: `no_command`, when the position resolves to no command at all, and
`timeout`. Both are in `Query` alongside the prelude's nine.

**The client supplies the correlation token and the deadline**, in the request.
The token because that is what a later `PIDE/query_cancel` names; the deadline
because the client is where the waiting actually happens, and §5.2's "no
prover-side timeout" and §6's "a per-request timeout so a lost reply cannot hang
the LSP request" are only compatible if there is exactly one policy and the
client owns it. On expiry the Scala side sends `Isabelle_MCP.cancel_query` and
answers `timeout`.

**Stage 4 made the `comment` note unreachable, deliberately.**
`Document.Snapshot.current_command` skips backward over ignored commands, so the
command it returns is never an ignored one and the prelude's `comment` flag never
comes back set. That is the better behaviour — asking on a blank line inside a
proof should answer with the enclosing proof's state, which is what jEdit does —
and the flag stays in both halves because it is correct, costs nothing, and would
be needed by any future caller that resolves positions differently.

**`no_context` is a tenth row of §5.3 that stage 3 found.** `Toplevel.context_of`
fails at the pristine toplevel, which is exactly where `end` leaves you, so a
find_theorems query aimed at a theory's final `end` has no search context at
all. `isabelle_goal` never sees it: at that command `Toplevel.is_proof` is
already false and the reply is `no_proof_state`. Measured, not deduced: the
stage-3 probe asked at `end` and got `no_context` back. Its agent-facing
sentence is approved and sits with the others in §5.3.

### 5.2 The ML side, and the discipline it must follow

Split by thread, deliberately:

**On the protocol thread** (fail fast, correlated errors, no expensive work):

1. Extract the request id **before anything that can raise**, so every later
   failure can still be reported against it (§3.6). A `fn [a,b,c] => …` handler
   raises `Match` on the wrong arity, and that `Match` is unattributable.
2. Capture `Document.state ()` once. It is a `Synchronized.var` returning an
   immutable value (`document.ML:920-923`, `synchronized.ML:43-51`), and while a
   protocol command runs no `Document.update` can interleave (§3.6). Capturing
   here — not inside the forked task — is what makes "the state as of the
   request" a precise statement.
3. Resolve `Document.command_exec`, wrapped in `Exn.capture`: an unknown node or
   command id **raises** `ERROR "Undefined command entry: N"` rather than
   returning `NONE` (`document.ML:238-241`), and `Debugger.breakpoint` does not
   guard against it.
4. Classify the command (§5.3) and reply immediately for every case that cannot
   produce a result.

**In a forked task** (the expensive part only):

5. Fork with an explicit group per request so it can be cancelled —
   `Future.forks {group = SOME (Future.new_group NONE), pri = Task_Queue.urgent_pri,
   interrupts = true, …}`. Urgent priority matters: at default priority a busy
   document keeps every worker and the reply waits behind proofs
   (`task_queue.ML:119`; the same choice `query_operation.ML:49` makes).
   Note that `group = NONE` from the protocol thread would create a fresh *root*
   group, unreachable from `Execution.cancel` — deliberate isolation, but it
   would also make the request uncancellable, so we pass our own group and keep
   it in an ML-side id→group table.
6. **The reply must not run under interruptible attributes.** `Future.forks`
   applies the job's attributes to the *whole* body, work and reply alike
   (`future.ML:441-455`), and `interrupts = true` maps to `private_interrupts`
   (`:473-476`, `thread_attributes.ML:77`), so a `cancel_query` arriving after
   the work succeeded is delivered asynchronously somewhere inside the reply
   code — the job dies and **no protocol message is sent**, hanging the request
   until the client's timeout. Either mirror `build.ML` exactly (fork with
   `interrupts = false`, wrapping only the work in `Future.interruptible_task`),
   or keep `Future.forks` for group cancellation and make the job body
   `Thread_Attributes.uninterruptible_body`, passing only the work through the
   `run` it hands you — the pattern Pure uses for this exact problem
   (`query_operation.ML:24`, `:38`), whose rendering §5.4 already copies.
7. **Reply unconditionally**, following `build.ML:107-118`: `Exn.capture_body`
   around the work, a second capture around the error formatting, and a
   last-resort crash reply. There must be no path that does not call
   `Output.protocol_message`.

No prover-side timeout is introduced. The client already bounds its own wait
(§3.7), and a second timeout policy in a second place would be one policy too
many.

`Isabelle_MCP.cancel_query` looks the id up in the group table and calls
`Future.cancel_group`. This is **parity work, not a new capability**:
cancellation exists today via overlay removal, and `Execution.cancel` cannot
reach a task forked from a protocol command (§3.9, §5.2 step 5), so without this
the move would lose it.

**Removing the table entry is the permission to reply, and stage 3 implemented
it that way.** An earlier reading of step 7 had the forked body's own capture
turn the interrupt into the "cancelled" reply. That leaves a hole: when a cancel
arrives before the task is dequeued, `Future.forks` never enters the job body at
all — `future_job` substitutes an interrupt result without running `e`
(`future.ML:441-455`) — so nothing replies and the request hangs until the
client's timeout. So every reply path goes through one atomic take-and-reply on
the id→group table: the worker takes the entry when it finishes, and
`Isabelle_MCP.cancel_query` takes it when it cancels, and whoever loses that race
stays silent. Exactly one reply per request, on every path, including the one
where the work never started. The table entry is also created before anything
that can raise, so a malformed request is still answered against its own id.

### 5.3 Failure modes that must be classified, not inherited

The direct read has failure modes that look like success. Each must produce a
distinct, honest reply — this is the single largest correctness risk in the
design.

| Condition | What ML observes | Required reply |
|---|---|---|
| Unknown node or command id | `ERROR "Undefined command entry: N"` **raised** (`document.ML:238-241`) | "no such command in the current execution" |
| Entry exists, no exec assigned | `command_exec` yields `NONE` | same as above |
| Command not finished | `eval_finished` false; calling `eval_result_state` gives `Fail "Unfinished lazy"` (`command.ML:162-163`, `lazy.ML:72-79`) | "not evaluated yet" |
| **Command interrupted** | `eval_finished` **true**; `eval_result_state` **raises** `Fail "Interrupt"` (`lazy.ML:110-113`, forced with `strict = true` at `command.ML:419-423`) | "the command's evaluation was interrupted" |
| Exception escaping eval | `eval_result_state` re-raises it into the handler | error reply carrying the message |
| **Ignored span** (comment/whitespace) | finishes successfully, state is the **predecessor's** (`outer_syntax.ML:271`, `toplevel.ML:419`) | serve the state, noting that the position is a comment and the state is the preceding command's — orientation for a mis-aimed position, not a safety measure |
| Not a proof state | `Toplevel.is_proof` false; `pretty_state` returns `[]` (`toplevel.ML:237-242`) | "no proof state here" — a definite answer, not a timeout |
| **No context at all** (find_theorems only) | `Toplevel.context_of` raises, which is where `end` leaves the state | "no theory context here, ask inside the theory" — a definite answer (§5.1) |
| Command has background work | `Execution.snapshot [Command.eval_exec_id eval] <> []` (the test `document.ML:727-736` uses) | serve the state, noting that forked work may still fail |

**A failed command is deliberately NOT a special case.** When a command fails,
Isabelle catches the error internally and the eval's state field holds the state
as it was *before* that command ran (`command.ML:239-247`), with no exported
accessor for the failure flag. An earlier draft treated this as a hazard and
proposed detecting it Scala-side. That was wrong and is withdrawn: the state
returned is the genuine state at that point in the proof — it is exactly what
jEdit's State panel shows for a failed command — and acting on it is correct,
since the agent's next move is to replace the failing command while facing that
goal. Nothing in the reply claims the command succeeded, and the agent has
already been told the line failed by the evaluation result that necessarily
preceded the query (`errors: line N` in the file section). The same reasoning
covers `isabelle_find_theorems` at a failed command: it searches the genuine
context of that point, which is what the agent wants.

**The replies, approved.** Positions render as `file:line`, the same form
everywhere else. The first four are raised as errors; the next two are served
with the state and a note; the rest cover cancellation, a crash and a timeout.
The "no theory context" line belongs to `isabelle_find_theorems` alone and was
approved after stage 3 measured the case; the others were approved before it.

```
The prover no longer holds a proof state for the command at MyTheory.thy:42. Evaluate the file again to get one.

The prover no longer holds a proof state for the command at MyTheory.thy:42 — the evaluation was cancelled. Evaluate the file again to get one.

The command at MyTheory.thy:42 has not finished evaluating, so it has no proof state yet. Retry in a few seconds.

The evaluation of the command at MyTheory.thy:42 was interrupted, so it has no proof state. Evaluate the file again to get one.

Reading the proof state at MyTheory.thy:42 failed: {message}

MyTheory.thy:42 is a comment or blank line; this is the proof state after the command before it.

The command at MyTheory.thy:42 is not a proof operation, so there is no proof state here.

There is no theory context at MyTheory.thy:42, so there is nothing to search here. Ask at a line inside the theory.

This command forked work that is still running, so a failure may still surface at MyTheory.thy:42.

The query was cancelled.

The prover could not answer this query and could not say why.

The prover did not answer this query within {n}s.
```

**`isabelle_find_theorems` says its own thing about the four command-state
failures.** The sentences above speak of a proof state, which is a non-sequitur
when the agent asked for theorems: what it lacks is somewhere to search. Approved
after stage 5 wired the tool, and living in `tools/find_theorems.py` because they
belong to that tool alone:

```
The prover no longer holds the context of the command at MyTheory.thy:42. Evaluate the file again to search there.

The prover no longer holds the context of the command at MyTheory.thy:42 — the evaluation was cancelled. Evaluate the file again to search there.

The command at MyTheory.thy:42 has not finished evaluating, so there is no context to search in yet. Retry in a few seconds.

The evaluation of the command at MyTheory.thy:42 was interrupted, so there is no context to search in. Evaluate the file again to search there.

Searching at MyTheory.thy:42 failed: {message}
```

Notes on three of them, decided:

- **"is not a proof operation"** is a *definite* answer, replacing today's timeout
  heuristic (§2 goal 4). It belongs to `isabelle_goal` only: at a non-proof
  command `isabelle_find_theorems` still searches the genuine context of that
  point, which is what the agent wants.
- **The forked-work note** says the state is usable and the command's verdict is
  not yet in — a forked proof that later fails surfaces its error at that line.
  It is rare in practice because the guard, judging by decoration, refuses that
  position first (`forks != 0, runs == 0` paints `background_unprocessed1`, §4.3
  rule 2). It is kept because it is correct, not because it is common.
- **`PIDE/query_cancel`'s own failure** (`Cancelling the query failed: {message}`)
  goes to the log, not to the agent: the cancel is fire-and-forget on an exit
  path, by which point the agent already holds its result or its error.

**No proof state is served for a command whose transition is still running, and
that is not a policy.** `eval_result_state` forces a lazy value that does not
exist until the command completes (`Fail "Unfinished lazy"`, row 3 above). The
only thing that could be served instead is the *preceding* command's state —
what jEdit's State panel shows — and answering a question the agent did not ask
is worse than refusing. Decided: refuse.

### 5.4 Rendering, exactly

ML produces the same bytes the existing paths produce:

- proof state: `Toplevel.pretty_state st |> Pretty.chunks |> Pretty.strings_of`
  wrapped with `Markup.markup_strings Markup.state`, mirroring
  `query_operation.ML:47-57`;
- find_theorems: `Pretty.strings_of (pretty_theorems (Find_Theorems.proof_state st) limit rem_dups criteria)`
  wrapped with `Markup.markup_strings Markup.writeln`, mirroring
  `find_theorems.ML:538-548` (whose `writelns_result` is exactly that wrapping,
  `query_operation.ML:28`).

Scala decodes with `Symbol.decode_yxml_failsafe` (**mandatory**, §3.5), re-wraps
each element with `Protocol.make_message(body, name, props)` so the HTML carries
the CSS classes the Python parsers key on, and calls
`render_query_html`. **`Pretty.formatted` is not applied to find_theorems**
(§3.5). For the proof state, matching today's output means applying
`Pretty.formatted(…, margin = resources.message_margin, metric = Symbol.Metric)`
as `pretty_text_panel.scala:41-43` does, with margin 80
(`vscode_message_margin`, `vscode_resources.scala:85`).

`find_theorems`' arguments keep their existing meaning and their existing trap:
`rem_dups = (allow_dups_arg = "false")` — inverted, and any string other than
exactly `"false"` means "keep duplicates" (`find_theorems.ML:542-544`). The
adapter must send the exact token, not a boolean rendering.

### 5.5 What the Python side stops doing

- `isabelle_goal` drops the caret cycle (`caret_update` → `state_init` → await
  `state_output` → `state_exit`), the 0.15 s sleep whose purpose is documented
  nowhere (`lsp_client.py:1566`), and the `STATE_OUTPUT_GRACE` heuristic
  (`:1546-1552`). "No proof state" becomes an observation.
- `isabelle_find_theorems` drops its caret update and its token-guard dance
  (`:1563-1592`); correlation moves to the request id.
- Both become servable under §4.3 like the other four, and `_caret_lock` loses
  two of its three users.

## 6. Implementation plan

Six stages, ordered by dependency. Stage 1 is entirely Python and needs no
component rebuild; the jar is rebuilt once, in stage 4.

**Stage 1 — everything in §4 (Python only, no rebuild). Done.** It was done in
this order, because each step lands in code the next one edits.

One thing this step list did not say, and the implementation had to supply: step
3 releases **four** tools, not six. `isabelle_goal`'s proof-state half and
`isabelle_find_theorems` read through the global caret that the evaluation is
steering, so §4.4 keeps them refused until part B removes the dependence. The
guard therefore takes a `moves_caret` flag, and those two callers pass it.

1. *The evaluation lifecycle fix* (§4.8). The `Evaluation` handle, `owns()`,
   write-once `_stamp()`, `_finish_if_owner`, the wait loop's new parameter and
   exit, the authoritative `if evaluation.outcome` read, and the
   `_cleanup_auto_opened` set-binding one-liner. This goes first: it rewrites the
   message-selection branch that step 4 then reshapes.
2. *The position-state helper* (§4.3) — position → processed / running / not
   evaluated / cancelled / unknown, with freshness checked **before** the
   reached test so a stale cache is not reported as "not evaluated". This step
   also adds `background_canceled` to `_TRACKED_TYPES` (`processing.py:76-79`)
   with an accessor, without which an interrupted command answers `processed`.
   The new decoration type is consumed by this helper only: the evaluation
   result's file sections are unchanged, since a cancelled command also carries
   `failed` and is therefore already counted and located there.
3. *The guard rewrite* (§4.3), releasing `isabelle_hover`,
   `isabelle_definition`, `isabelle_local_occurrences` and
   `isabelle_command_output` during an evaluation, plus the refusal messages that
   name the target and the requested position.
4. *The evaluation result layout* (§4.6) — nested detail under `running:`, the
   unit word on line spans, the shared 10s threshold, the conditional trailing
   call to action, and its suppression inside `isabelle_evaluation_status`.
5. *The footer* (§4.2) — computed in `_ensure_lsp_started`, appended by the
   middleware after the unicode warning, on the six query tools and
   `isabelle_command_status`; the single authoritative completion check that also
   clears the state.
6. *(moved out)* `isabelle_command_status` now ships in stage 4 — its
   enumeration must run server-side (§4.5). Only the internal position-state
   helper of step 2 stays here.
7. *Plurals* (§4.2) — the five offender sites; leave the two Isabelle-text
   matchers alone.
8. `models.py` (target file on the result model), `instructions.py` (teach the
   agent the evaluation target), and tests: unit tests for the helper's four
   outcomes, the nine ownership/outcome tests of §4.8, per-tool tests that a
   processed position is served while an evaluation is outstanding, and
   character-for-character message tests in the style of `TestCompleteMessage`.

Where stage 1 landed, for whoever picks this up next: `Evaluation` /
`EvaluationState.owns` / `_finish_if_owner` and the sentence constants in
`evaluation.py`; `ProcessingTracker.position_state` and the five state words in
`processing.py`; `position_state` / `_settled_position_state` /
`check_evaluation_guard` and `evaluation_footer` in `evaluation.py`; the
`_pending_footer` ContextVar and `_ensure_lsp_started(footer=True)` in
`server.py`; `plural` in `utils/core.py`. Tests: `TestEvaluationLifecycle`,
`TestGuardPositionDecision`, `TestResultLayout`, `TestEvaluationFooter`,
`TestLeadingSentence` in `tests/test_tools_evaluate.py`, `TestEvaluationGuard` in
`tests/test_edge_cases.py`, `TestFooterPlumbing` in `tests/test_server.py`, and
the `position_state` block in `tests/test_processing.py`.

**Stage 2 — the two gating probes. Done; both passed** (§9). The direct read
works, and it survives the perturbation the evaluation itself causes. The overlay
fallback of `CARET_AND_POSITION_RESEARCH.md` §2 is not needed.

**Stage 3 — the ML prelude. Done, and exercised against a live prover.** The
three protocol commands (`Isabelle_MCP.proof_state`, `.find_theorems`,
`.cancel_query`), the id→group table, the reply helper, and the §5.3
classification are in `ML/mcp_prelude.ML`; the version string is now `"2"`. The
comment at the foot of the file was wrong about why the startup banner uses
`TextIO.print` — `Output.system_message` is *not* dropped at `--use` time,
`init_channels` has already pointed it at physical stdout — and now records the
real constraint, that `Output.protocol_message` raises `Protocol_Message` until
`Isabelle_Process.init` installs the channel.

What the live run measured, beyond the classification itself (§9, probe run of
stage 3):

- **Every gap between two commands is its own ignored command.** A 20-line
  theory yields 20 commands, half of them ignored spans covering the whitespace
  and comments between the real ones. So the `comment` note is not exotic: any
  position that does not land inside a command's own span gets it. §5.3's
  approved sentence already says "comment or blank line", which is what this is.
  In practice stage 4's `Document.Snapshot.current_command` skips backward over
  them first, so the note should stay rare.
- **A running command makes every later command in the file `unfinished` too**,
  which is what the §4.3 guard already assumes.
- **After `isabelle_cancel_evaluation`, the cancelled node keeps almost nothing.**
  Not one finished command of a 14-command node survived except the `theory`
  header: the read answers `undefined`, never `interrupted`. That follows from
  §3.10's mechanism — entries are carried into the new execution version only
  while `Command.eval_running` holds, and `Execution.discontinue` makes that
  false for all of them — but it means `undefined`'s approved sentence ("a file
  changed while this query was in flight") describes a cancel imprecisely.
  **Measured in stage 4: it does reach it, and the sentence was rewritten.**
  After cancelling an evaluation mid-file, `isabelle_command_output` at a line
  that had finished *before* the cancel is served normally — the guard sees
  processed decoration, and that tool reads the snapshot's markup rather than
  the execution version. So `isabelle_goal` at the same line passes the same
  guard and gets `undefined`. The old sentence blamed a file change and said
  "Retry", when nothing had changed and retrying cannot help; §5.3 now carries
  the approved replacement, which names cancellation and sends the agent to
  re-evaluate — the same instruction, in the same words, as the `interrupted`
  reply next to it.

  **Then stage 5 wired the tool and disproved the inference.** The measurement
  above was of `isabelle_command_output`, and the conclusion that
  `isabelle_goal` would therefore answer `undefined` was a deduction, not an
  observation. With the tool actually wired, a cancel followed by a query at an
  already-finished line was tried at 0s, 0.5s and 3s and answered correctly
  every time. The reason is that cancelling goes through `force_interrupt`,
  which sends a **synthetic edit** (a space appended to line 0) to trigger a
  restricted-perspective update. That edit both re-creates the command ids and
  marks the decoration cache stale, so the guard does not serve the position —
  it re-evaluates, and the query then succeeds. The two halves move together
  because the same server updates both.

  **So the cause is not knowable from the status, and the reply no longer
  asserts one.** `undefined` states the fact and gives the instruction, which is
  right whatever the cause. But the *client* does know one thing the prover
  cannot: whether the last evaluation was cancelled
  (`evaluation.last_evaluation_was_cancelled`, from the write-once outcome on
  the evaluation handle). When it knows, the reply names the cause; when it does
  not, it stops at the fact. §5.3 carries both forms, approved. The rendering
  key for the second is `UNDEFINED_AFTER_CANCEL`, which is not a wire status.
- **`Execution.snapshot` does detect outstanding forked work** — a `by` whose
  proof was still running reported three tasks. It rode along with no reply
  here, because a `by` has no proof state to serve it with.
- **`ML_command` is a diagnostic command**: its eval finishes at once and the
  work runs as a print, so it cannot be used to manufacture a slow command. Use
  `ML ‹…›`, whose eval really does run the code.

**Stage 4 — the Scala adapter and the jar rebuild. Done.** `src/query.scala`
holds `Query` (the status vocabulary and the reply record) and `Query_Handler`
(the id→consumer table, taken atomically so a reply, a cancel, a timeout and the
shutdown drain cannot answer one LSP request twice). `language_server.scala`
holds the three request handlers, the command enumeration, and the version gate;
`lsp.scala` holds the four new messages. The jar was rebuilt by the §7 recipe of
`COMPONENT_INSTALL_PLAN.md` and `scripts/check_component.py` passes with 15
declared sources.

Measured against a live prover, on a theory with a three-step `apply` proof:
every line answered with its own command's state — `lemma` one subgoal,
`apply (induct x)` two, the first `apply simp` one, the second none — and
`definition`, `done`, `by` and `end` answered `no_proof_state`, find_theorems at
an `apply` answered `ok` with its hits, find_theorems at `end` answered
`no_context`, a line past the end of the file answered `no_command`, a
`query_cancel` for a token nobody registered was a silent no-op, and
`commands_at_lines` returned exactly the commands on each requested line and
nothing for a blank one. The HTML carries the `state_message` and
`writeln_message` classes the Python parsers key on.

**The column matters, and the repository already had the rule.**
`utils.isabelle_tokens.resolve_caret` anchors on the line's **last non-blank
character**, because column 0 of an indented line sits inside the ignored span
that precedes the command, and `current_command` skips backward from there onto
the *previous* command. A first probe run asked at column 0 and got every line's
predecessor. Stage 5 must pass `resolve_caret`'s position, exactly as
`goal.py` and `command_output.py` already do.

**The version gate was tested by breaking it**: with the prelude renumbered to
`"9"` against a jar that speaks `"2"`, startup refuses with both numbers named.

`isabelle_command_status` shipped with it, in
`tools/command_status.py` (the state vocabulary, the answer, and the layout),
`models.py` (`LinePosition`, `CommandStatusLine`, `CommandStatusPosition`),
`lsp_client.get_commands_at_lines`, and `server.py` with `footer=True`. Its
per-command state comes from `ProcessingTracker.range_state`, which is
`position_state` generalised from a line to a span and additionally returns how
long a running command has been running; `position_state` is now the one-line
case of it, so the precedence and freshness rules exist once. Eleven unit tests
in `tests/test_command_status.py`, two more in `tests/test_processing.py`. Live
against a running prover it answered `processed`, `running for 26s`,
`not evaluated`, `no command` and `file not open` for one call spanning two
files. `evaluation._relativize` became `relativize` so the new tool could reuse
the path-display rule rather than copy it.

Also ships
`isabelle_command_status` and its server-side command enumeration (§4.5), and
the third LSP message `PIDE/query_cancel` (§5.1); handlers must not block the
language server's loop (§5.1). A protocol handler with a
`synchronized` id→promise table and an `exit` that drains it
(`scala.scala:292-351` as the model); the two LSP requests in `lsp.scala` and
their handlers in `language_server.scala`; position→command resolution via the
existing `rendering_offset` (`language_server.scala:208-212`) plus
`Document.Snapshot.current_command` (`document.scala:777-786`), which also does
the backward skip over ignored commands that the fork's own
`command_at_position` lacks; decode, re-wrap, render (§5.4); a per-request
timeout so a lost reply cannot hang the LSP request; and the prelude/jar version
check of §8. Rebuild via the copy-with-`no_build`-removed recipe and pass
`scripts/check_component.py`.

**Stage 5 — rewire the Python client and release the last two tools.** Replace
the two caret cycles with the new requests; delete the 0.15 s sleep and the
`STATE_OUTPUT_GRACE` heuristic; extend §4.3's guard to all six tools.

**Stage 6 — the remaining probes, integration tests, docs. Done.** Probes 3–7
all passed (§9), and probe 3 found a real defect on the way: a `query_cancel`
for a query still in flight orphaned its LSP request. Nine integration tests in
`tests/integration/test_query_tools_e2e.py` cover both tools answering during an
active evaluation, each step of a proof reporting its own state, and the
`no_proof_state`, `no_context`, `no_command`, `unfinished`, `interrupted`,
`cancelled` and `timeout` rows of §5.3, plus the Unicode round trip. They use
two theories against one prover — one evaluable to its last line, one containing
a command that runs for ninety seconds — which is also what keeps the suite to
93 seconds.

`README.md`, `instructions.py` and `CHANGELOG.md` are updated. So are
`SPECIFICATION.md`, `API_DESIGN.md` and `ARCHITECTURE.md`, which this plan had
not listed: all three described `isabelle_goal` as the caret cycle, with worked
examples and two diagrams. The dead code that cycle left behind — the caret
lock, `get_dynamic_output`, `_enrich_timeout_error` and the dynamic-output
waiter machinery — is deleted, 178 lines.

## 7. Cost

**Code.** Scala: one protocol handler plus two request handlers and their LSP
objects, roughly 150–200 lines including the correlation table and rendering.
ML prelude: roughly 120–150 lines of plain SML, most of it the §5.3
classification and the unconditional-reply discipline. Python: the guard and
status-line work is small; the two rewires mostly *delete* code (two caret
cycles, a sleep, a grace heuristic).

**Build.** Editing `ML/mcp_prelude.ML` needs **no rebuild** — it is not listed in
the component's `sources` and is read from disk at server start
(`mcp_main.scala:25`, `:28`). Any `src/*.scala` change needs the jar rebuilt via
the copy-with-`no_build`-removed recipe (`docs/COMPONENT_INSTALL_PLAN.md` §7),
gated by `scripts/check_component.py`, which compares the jar's recorded
per-source SHA-1s. A *new* Scala file must also be added to `sources` or the
gate fails on the set mismatch.

**Runtime.** A proof-state request costs one message each way plus a pretty-print
at urgent priority. A find_theorems request costs the same round trip plus the
same full fact-space scan it costs today (§3.7) — unchanged, by design. Neither
produces a document update, so neither perturbs an evaluation.

**Risk retired.** The caret stops being written by anything but evaluation;
`_caret_lock` contention disappears; and the two tools become usable during
evaluation and (later) during a debugger stop.

## 8. Reliability

The design's reliability argument rests on four things, in decreasing order of
confidence.

1. **The read path is a sanctioned pattern.** `Debugger.breakpoint` performs the
   identical lookup from a protocol command (`debugger.ML:277-283`). What is new
   is doing it for a *state* rather than a breakpoint table, and doing the
   expensive part in a forked task.
2. **The content is provably identical to today's.** Same `Toplevel.state`, same
   `pretty_state`, same global print options (§3.4), same markup wrapping, same
   renderer (§5.4). The remaining variable is the margin, which is a Scala-side
   constant we control.
3. **Every failure mode is enumerated and must be classified** (§5.3). The three
   dangerous ones — failed command, interrupted command, ignored span — all look
   like success and all return or raise something plausible. The design treats
   them as first-class outcomes rather than edge cases.
4. **The reply is unconditional by construction** (§5.2 step 7), because the
   alternative is a request that hangs with no correlatable trace (§3.6).

**A gap this upgrade creates, and must close.** Editing the prelude needs no jar
rebuild, and `scripts/check_component.py` checks only that
`ML/mcp_prelude.ML` *exists* — it is absent from `build.props: sources`, so it
is absent from the jar's recorded hashes and nothing detects an edited, stale or
reverted prelude. Today that is tolerable because the prelude and the Scala side
share almost no contract. After this upgrade they share three commands and a
reply format, so a version skew becomes a silent hang. The prelude already
carries a version string that travels back in the pong
(`mcp_prelude.ML:17`, `:60-61`), and `Prelude_Handler.handle_pong` only checks
that the text is non-empty (`language_server.scala:33-36`). **Stage 4 compares
that version against a constant compiled into the jar and refuses to start on
mismatch — decided.** A warning would be ignored, and the failure it guards
against is a request that hangs with no correlatable trace (§3.6).

## 9. Must be measured before this is trusted

Source reading cannot settle these, and this project's rule is to measure.

**When.** Probes 1 and 2 are hard gates and run as stage 2, before any of Part B
is written — they decide whether the direct read works at all. Probes 3–7 run
alongside the implementation, in stage 6. Decided.

1. **Does the direct read work at all** — a protocol command that resolves a
   finished command and returns its proof state, compared byte-for-byte against
   what `isabelle_goal` returns today for the same position.
2. **Does it survive updates** — the same request after a caret move, after an
   edit later in the file, after an edit earlier in the file (expected to fail
   cleanly), and after an edit in an imported theory (expected to fail cleanly).
   §3.10 predicts each outcome; confirm each.
**Probe 1: passed** (measured). A finished command's `Toplevel.state` is
reachable from ML and pretty-prints to the same subgoals `isabelle_goal`
returns; the two differences are the `proof (prove) / goal (N subgoals):` header
the state panel strips and ASCII-vs-unicode symbols, i.e. exactly the two
rendering steps §5.4 and §3.5 already plan for.

Two facts the probe cost time to learn, recorded so nobody pays twice. Command
ids are allocated by the JVM side, whose counter ticks BACKWARDS — they are
negative (`counter.scala`, `document_id.scala`). And a command's
`Toplevel.pos_of` carries no absolute line, so ML cannot map a position to a
command: the public `DOCUMENT` signature exposes only
`command_exec: state -> node -> id -> exec option`. The Scala-resolves-the-id
split of §5.1 is forced by the API, not a preference.

**Probe 2: passed** (measured, two rounds, identical). The criterion is command
id **and exec id and state text**, all three: a first attempt keyed on "does the
id still resolve" could not decide anything, because PIDE keeps a command's id
across a re-parse while re-creating its eval, and a re-executed command yields
the same state text.

| after | measured |
|---|---|
| a caret move | reachable, byte-identical (id, exec and state) |
| an edit later in the same file | every id in the file re-created |
| an edit earlier in the same file | every id in the file re-created |
| an edit in an imported theory | ids survive, every exec re-created |

The middle two contradict §3.10's before/after distinction, and §3.10 now
records why: this client's `didChange` replaces the whole document, so the edit's
position cannot matter.

This is the answer the gate needed. What the direct read must survive is the
perturbation the evaluation itself causes — perspective changes — and that
survives byte-for-byte. Every case where it stops resolving is a case where the
command genuinely no longer exists, and it fails the two ways §5.2 and §5.3
already require to be classified rather than raised: an absent entry
(`Undefined command entry`) or a replaced exec. Nothing here forces the overlay
fallback of §10.

The probe vehicle, for whoever repeats this: a read-only observer thread added
to `ML/mcp_prelude.ML`, triggered by a file, scanning negative ids through
`Document.command_exec` and dumping each entry's name, finished flag and
`Toplevel.pretty_state`. It needs no Scala change and no jar rebuild, and
`isabelle ML_process -l HOL -f FILE` type-checks it in the same raw-ML context
the prelude is compiled in. Remove it afterwards — a prelude that fails to
compile is fatal to every prover start on the machine.

**Stage 3's run: most of probe 3, measured early.** Writing the prelude made the
classification cheap to test, so it was tested rather than assumed. The vehicle
is probe 2's observer thread, extended to call `Protocol_Command.run` on the new
commands and to tee `Private_Output.protocol_message_fn` so the replies could be
read without a jar that handles them. Against a live `HOL` prover and a
hand-built theory:

| asked at | status |
|---|---|
| `lemma`, `apply`, `apply`, `apply` | `ok`, with the same subgoals the file's proof shows |
| the whitespace and comments between commands | `ok` plus the `comment` note, carrying the preceding command's state |
| `theory`, `definition`, `done`, `by`, `end` | `no_proof_state` |
| find_theorems at `apply` | `ok`, twelve hits, five displayed |
| find_theorems at `end` | `no_context` (the new row, §5.1) |
| a command whose evaluation had not finished | `unfinished`, and so was every command after it |
| a command id that does not exist, and an unknown node | `undefined` |
| two arguments instead of three | `failed`, "bad arguments" |
| find_theorems with the unparsable query `(((` | `failed`, carrying the outer-syntax error |
| `cancel_query` for an id nobody registered | silent, no crash, no reply |

Three statuses are still unmeasured and stay with probe 3 in stage 6:
`interrupted`, `cancelled`, and `crashed`. `interrupted` in particular resisted
construction — cancelling an evaluation destroys the node's finished commands
outright, so the read answers `undefined` instead (§6 stage 3 records this, and
what stage 4 must decide about it).

3. **The three §5.3 rows stage 3 could not construct** — an interrupted command,
   a cancelled query, and the last-resort crash reply.

   **Interrupted: passed** (measured). Stage 5 had failed to construct it because
   `cancel_evaluation` goes through `force_interrupt`, whose synthetic edit
   re-creates every command id — leaving the entry *gone* rather than
   interrupted. Cancelling execution alone skips that edit: the running `ML`
   command then answers `interrupted` (at 0.5s and again at 3s), and every
   command that had finished before the cancel still answers `ok`. That is the
   §5.3 row exactly, and the reason the tool-level path shows `undefined`
   instead is the edit, not the cancel.

   **Cancelled: passed, after fixing a defect it exposed.** A `query_cancel` for
   a query still in flight took the request out of the adapter's table and never
   answered it, so the LSP request hung forever. The client only ever cancels
   after its request has returned, so the hole was invisible in normal use — the
   probe found it by cancelling a live query. Fixed: whoever takes the request
   out of the table owes it a reply, on that side as much as in the prelude. The
   status now comes back `cancelled` within the round trip.

   **Crashed: not constructed, and not worth constructing.** It is the
   last-resort reply for a status this side does not recognise or an error
   formatter that itself fails — reachable only by fault injection. The Python
   half is unit-tested
   (`test_a_status_this_side_does_not_know_is_reported_as_a_crash`); the Scala
   half is the shutdown drain.
4. **Rendering equality: passed, byte for byte.** The state panel and the
   find_theorems query operation still exist server-side — only the client
   stopped using them — so both were driven over raw LSP and diffed against the
   new path. A two-subgoal state (3503 bytes), a one-subgoal state (1744), a
   finished proof (246) and a find_theorems result with 1371 hits and 40
   displayed (43376) all came back **identical**, not merely equivalent. The
   `item`-class trap of §3.5 is therefore not a trap: not applying
   `Pretty.formatted` to find_theorems is what keeps it identical.
5. **Forked-task behaviour: passed.** With three sleeping proofs occupying
   workers and the evaluation reporting lines 6, 9 and 12 all running, a proof
   state query answered in **0.04s** — urgent priority does beat a busy
   document, decisively. The prover-side backstop fires and replies: a query
   sent with `timeout: 0.5` came back `timeout` after 0.5s. Cancellation via our
   own group reaches the body, which is what the `cancelled` result of probe 3
   demonstrates.
6. **Symbol decoding: passed.** A goal stated with `\<forall>`, `\<le>` and
   `\<longrightarrow>` comes back as `∀x y. x ≤ y ⟶ x ≤ y + 1`, with no
   `\<…>` anywhere in the parsed subgoals.
7. **The footer's cost: passed.** `evaluation_footer` takes a median of
   0.059 ms and at worst 0.42 ms, against tool calls measured in tens of
   milliseconds. It reads local state and issues no request, which is why.

## 10. Out of scope

- Changing how evaluation is driven; the caret remains the evaluation anchor.
- Widening `vscode_caret_perspective`. Setting it to 0 means "unrestricted", not
  "no window": a bare `didOpen` would then execute the whole file to EOF
  (`vscode_model.scala:122`), destroying the evaluate-only-to-the-caret model.
- The overlay-plus-query-operation route. It remains the documented fallback if
  the direct read fails a probe in §9; see `CARET_AND_POSITION_RESEARCH.md` §2.
- The debugger work, which is shelved
  ([`DEBUGGER_REVIEW_AND_DECISIONS.md`](DEBUGGER_REVIEW_AND_DECISIONS.md)) and
  whose open question about queries during a stop this upgrade answers.
