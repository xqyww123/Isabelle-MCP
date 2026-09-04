# Changelog

## Unreleased

- **Cancellation leaves no corpse.** `isabelle_cancel_evaluation` is now one
  server-side request that stops the prover, retracts every perspective and
  *retires* each interrupted command — a zero-length edit on the stable version
  re-mints its id, so the prover cancels and purges the old execution and the
  command is genuinely back to unevaluated — repeating until none is left. The
  interrupted commands re-run on the next evaluation that reaches them; nothing
  finished is lost, the text on the prover stays byte-identical, and nothing is
  written to disk (the old "append a space" edit is gone). Two outcomes:
  `retired` (with the list of reset commands) and `nothing_running`. Other tool
  calls wait while a cancellation is in progress. Breakpoint demotion after a
  cancel is targeted: only sites from the first retired command of a theory
  onward are demoted.
- **One catastrophe for the whole server.** When the prover cannot be stopped
  cleanly — the server's 120 s budget or the request's 150 s budget runs out,
  the prover stops answering, or an internal failure — the session is
  terminated through the single teardown path shared with `isabelle_terminate`,
  and every tool answers with one sentence: *"The Isabelle session hit an
  internal failure and has been terminated; call isabelle_launch to start a new
  one. Details are in the server log."* An evaluation still in flight reports
  *"Evaluation stopped: the Isabelle session is no longer running. Call
  isabelle_launch to start a new one."*
- **Evaluation targets must be `.thy` files.** A `.ML`/`.sml` target with exactly
  one known load command is redirected to that command (the reply's first line
  says so); otherwise it is refused with a pointer to the loading command(s).
  Any other suffix is refused. Query tools refuse, never redirect. Load-command
  positions are now converted through pending edits, so a pointer or redirect
  is right even while the loading theory has unparsed edits.
- Prelude version 6 (the jar checks it at launch); the prebuilt jar is rebuilt.
- **`line=-1` resolves to the real last line.** It used to resolve to the
  phantom empty line after a trailing newline, so `-1` with `after_text`
  could never match anything. Line counting now agrees with `wc -l` and
  editors.
- **A second `isabelle_evaluate_to` on the file being evaluated joins the
  run instead of being refused.** The target only moves forward (the max of
  the two lines), and every joined request is answered against the run's
  real target, not the line it happened to ask for. A run ends early only
  when its last still-waiting request is aborted — a client interrupting one
  of several waiters no longer kills the shared run. Query tools
  (`isabelle_goal` etc.) on the file being evaluated are no longer refused
  either: they wait for that run to reach the line. They never advance it — a
  query past the run's target gets the not-evaluated error instead.
- **Honest cancellation caveat.** "Already-processed results remain valid
  for querying" overstated what survives a cancel; the tool description and
  the specification now read "Results before the first unfinished command
  remain valid for querying."
- **`isabelle_evaluation_status` no longer hides failures when idle.** After
  a run ended it used to answer just "No evaluation in progress." even when
  commands had failed. It now reports the errors that remain anywhere the
  prover holds a theory, with line numbers, leading with "No evaluation in
  progress. Nothing is running and no errors remain." or "No evaluation in
  progress. Nothing is running, but {N} failed commands remain."
- **One completion verdict, one wording.** The moment an evaluation is
  internally complete, every outlet (`isabelle_evaluate_to`,
  `isabelle_evaluation_status`, the status footer) says "Evaluation has
  completed up to …" — the old downgrade that kept saying "arrived at"
  while unrelated commands still ran or old failures existed is gone. A
  completion with failures appends "{N} failed commands remain. Call
  isabelle_evaluation_status for details."
- **Editing a heap-precompiled file is refused outright.** A file compiled
  into the running session's heap cannot be re-evaluated by editing it — the
  prover keeps using the heap version. Syncing such an edit now raises an
  error saying exactly that and pointing at a relaunch with a smaller base
  session, instead of evaluating stale text and warning after the fact.
  Internally the affected evaluation ends as `abandoned`, a third outcome
  besides complete/cancelled, so it is no longer misreported as a cancel.
- **`isabelle_launch` restarts instead of refusing.** The launch identity is
  the pair (session, debug). The same pair answers "… is already running …
  Nothing changed."; a different session or debug value restarts the prover
  and says what replaced what; a dead prover is simply started. A restart is
  refused while a thread is stopped at a breakpoint — resume or terminate
  first. (This supersedes 0.4.0's "changing `debug` needs
  `isabelle_terminate` first".)
- **Breakpoint pause messages lead with the cause.** A hit now leads with
  "Breakpoint hit: {hits}. The affected evaluation is paused." (plural
  "Breakpoints hit:" for several); `isabelle_evaluate_to` during a live hit
  is refused with "… cannot run while a thread is stopped at a breakpoint.";
  and the status footer appends the pause line plus "Call
  isabelle_debug_state for the hit details." while a hit is live, instead of
  the misleading "has been running for N s" activity.
- **Leaner server instructions, plus an installable skill.** The
  always-loaded server instructions were cut to fit the 2048-character
  budget: four paragraphs moved verbatim into the docstrings of the tools
  they describe, and the command-line section (getenv, ROOT/ROOTS,
  components, settings, build flags) became the bundled agent skill
  `isabelle-command-line`.
- **`isabelle-mcp install` installs the bundled skills.** Every bundled
  skill (currently `isabelle-command-line`) is copied into
  `~/.claude/skills` and `~/.codex/skills` for each client the server was
  registered into; `isabelle-mcp uninstall` removes them again. A copy still
  carrying the `managed-by: isabelle-mcp` frontmatter marker is ours and is
  overwritten on upgrade or deleted on uninstall; a hand-edited copy is left
  alone with a warning. `--no-skills` opts out.
- **Warnings are no longer reported.** The per-file snapshot had a `warnings:`
  row beside `errors:`, the counts-only fallback said "2 warnings", and a file
  whose only marks were warnings counted as worth mentioning. All of it is
  gone: no report — `isabelle_evaluate_to`, `isabelle_evaluation_status`, or a
  query tool's footer — mentions warnings at all, and a file whose only marks
  are warnings now counts as clean. The prover's own `[warning]` output lines
  are untouched; ask `isabelle_command_output` and you still get them.
- **`sorry` shows up as `sorry`, not as an error.** A `sorry` used to be
  indistinguishable from a failed proof — both landed in the `errors:` row,
  both were counted as failed commands — so a theory full of `sorry` looked
  broken and a real failure hid among them. `errors:` now means the
  `text_overview_error` decoration and nothing else, and `sorry` gets a row of
  its own: `sorry: line 5`, `sorry: lines 12, 40`. A `sorry` is never counted
  as a failure, never keeps a file from being closed, and never earns a call to
  action: an evaluation whose only mark is a `sorry` draws no call to action
  and no failure sentence in the footer, an idle report still says `no errors
  remain`, and the line is simply listed. A cancelled command is not an error
  either — cancelling leaves no mark of any kind, so a killed command is back
  to not evaluated. What stayed invisible stays invisible:
  `Skip_Proof.cheat_tac` and `oops` leave no trace on any channel, and the
  report cannot tell you about them.
- **Failure counts say what remains.** `{N} command(s) failed.` became
  `{N} failed commands remain.` (`1 failed command remains.` in the singular)
  everywhere it appears — the completion sentence's suffix, the idle first
  line, and the query footer. The count was never "this run's failures": it is
  every failure still standing anywhere the server is looking, the same set the
  all-clear sentence "… and no errors remain." reports as empty.
- **Errors in dependencies are reported, and stay reported until fixed.** An
  error in an imported theory used to be invisible — the file was never opened,
  so it had no line numbers, and once a run finished the report forgot it
  entirely. Every report now covers every theory the prover holds that has a
  failure or a running command, not just the import closure of the current
  target: with line numbers where the prover has published them, and a
  counts-only line (`Imported.thy: 1 error (no line info)`) where it has not. A
  broken dependency reappears in every idle report until the day it is fixed.
- **Theories still to be processed are one line, not one block each.**
  Reporting now covers every theory the prover holds, and while a session that
  is not precompiled loads, that can be hundreds at once — during one
  `HOL-Analysis.Analysis` import, 150 in a single poll. Rather than a block
  each, the imports of the open documents that still have unprocessed commands
  are counted: `{N} imported theories are not yet processed.` (`1 imported
  theory is not yet processed.`). A file you evaluated to a mid-file line, a
  cancelled evaluation's target, or an open file nothing evaluated is not an
  import and is not counted. Theories with an actual failure or a running
  command still get their own block.
- **Open files look after themselves.** Tool calls now close the files that are
  settled — no errors, no breakpoints registered in them, and not something you
  evaluated yourself (a file you evaluated stays open for the rest of the
  session) — so a long session no longer accumulates dependency documents
  nobody is working on, and the errors that are left open are the report. A
  file the prover still holds is silently reopened the first time a tool asks
  about a position in it again: about half a second when the file was not
  touched meanwhile, up to two seconds when it was; no proofs re-run, and the
  caret does not move. (One exception: a file that still contains Unicode
  symbols is normalised to ASCII on that first reopen, and that edit does cost
  one re-check, once per file.) `isabelle_command_status`,
  `isabelle_set_breakpoint` and `isabelle_list_breakable_sites` used to
  dead-end on a theory the prover holds but this server does not have open — a
  dependency you never opened by hand, and now also a file the sweep tidied
  away: `command_status` answered `file not open`, `set_breakpoint` refused
  with "not open in the prover", and `list_breakable_sites` reported fully
  compiled ML code as not evaluated. All three reopen the file and answer.
- **A closed file's cache can no longer answer for it.** Closing a file made
  the prover publish an "erase" decoration push, from which this server
  rebuilt an empty cache that read every line as processed. With the prover
  busy on another file, a query on a just-reopened file could be served from
  that empty cache. A cache is now built only from a full decoration push;
  until one arrives the position is reported as not evaluated.
- **The first edit to a newly loaded dependency is no longer missed.** An
  import loaded by the last evaluation and edited before the next call used
  to be trusted as unchanged, because there was no earlier record to compare
  against. A dependency seen for the first time now counts as changed.
- **Reopening an untouched file no longer costs the two-second grace
  window.** A file the unified close tidied away and nobody touched since is
  reopened without distrusting the decoration cache: the bytes pushed and the
  file's stat are exactly what they were at the close.
- **No tool starts an evaluation except `isabelle_evaluate_to`.** The six query
  tools — `isabelle_goal`, `isabelle_hover`, `isabelle_definition`,
  `isabelle_command_output`, `isabelle_find_theorems`,
  `isabelle_local_occurrences` — used to evaluate the line for you when it had
  not been evaluated, so a single query could spend minutes of prover time
  nobody asked for, on a target nobody chose. They now answer only about
  positions that have already been evaluated, and say so plainly when they
  cannot: "{file}:{line} has not been evaluated. Evaluate up to that line with
  isabelle_evaluate_to, then ask again." The one exception is a wait, not a
  start: a query on the file an evaluation is already running towards, at a
  line that run is already going to reach, waits for it (up to ten seconds,
  then a progress report). The run's target does not move and neither does the
  caret; a query past that target gets the error like any other. The debugger
  tools also stop short of evaluating, in the wording they already had, and
  they never wait. And `isabelle_command_status` needs no error for any of
  this: "not evaluated" is one of its answers, and it is now the answer for a
  `.thy` file the prover does not hold. The state-word list is unchanged;
  `file not open` has simply become rare — no `.thy` the prover holds produces
  it any more.

## 0.4.0

- **ML breakpoint debugging.** `isabelle_launch(session, debug=true)` starts
  the prover with Poly/ML debugger instrumentation (newly compiled ML gets
  breakable sites; heap-precompiled code is unaffected and no heap is
  invalidated), and eleven new tools drive it: `isabelle_set_breakpoint`,
  `isabelle_del_breakpoints`, `isabelle_list_breakpoints`,
  `isabelle_list_breakable_sites`, `isabelle_enable_all_breakpoints`,
  `isabelle_disable_all_breakpoints`, `isabelle_debug_state`,
  `isabelle_eval_at_breakpoint`, `isabelle_locals_at_breakpoint`,
  `isabelle_continue_breakpoint`, `isabelle_step_at_breakpoint`.

  Breakpoints are addressed by line plus an anchor snippet (no columns),
  survive recompilation as registry entries that re-arm on explicit
  `isabelle_enable_all_breakpoints` (nothing re-arms in the background), and
  stop working when their code is recompiled, the prover is relaunched, or an
  evaluation is cancelled — each demotion reported once as a *debugger
  notice* appended to the next tool result. A thread stopping at a breakpoint
  is a **hit** (`h1`, `h2`, …): `isabelle_evaluate_to` then returns early,
  leading with a hit report (position, call stack, implicit frame-0 locals),
  the evaluation stays paused until the hit is resumed, and
  `isabelle_evaluation_status` leads with a paused section. A run that cannot
  stop where the registry says it should warns up front
  ("N breakpoints … are not armed"). `isabelle_cancel_evaluation` sweeps
  stopped threads and says so. The launch identity is now the pair
  (session, debug): changing `debug` needs `isabelle_terminate` first.

- **The seven model-shaped tools now answer with YAML text instead of JSON
  structured output.** `isabelle_launch`, `isabelle_session_info`,
  `isabelle_hover`, `isabelle_definition`, `isabelle_local_occurrences`,
  `isabelle_goal` and `isabelle_find_theorems` drop their output schemas and
  render their result models as YAML — Unicode kept verbatim
  (`allow_unicode`), `None` fields omitted, key order preserved, long
  statements never folded across lines. One serializer
  (`utils/formatters.model_to_yaml`) behind all of them; the tool functions
  still return their models internally. The six narrative tools are unchanged.
  New runtime dependency: `pyyaml`.

- **`isabelle_goal` and `isabelle_find_theorems` now work while an evaluation is
  running.** Both used to be refused outright for as long as one was in flight,
  and the reason was mechanical: they read the proof state through Isabelle's
  *global caret*, which the evaluation is also steering, and there is no
  arbitration between two writers of one caret.

  They no longer touch the caret. An ML prelude injected into the prover defines
  protocol commands that resolve a command in the document state and read that
  command's own state directly; the Scala side resolves the position to a command
  and correlates the reply. One request, one response, no overlay, no document
  update. The rendered output is byte-for-byte what the state panel produced.

- **A command with no proof state now says so, in one round trip.** The old path
  concluded "no goals" from ten seconds of silence — `by`, `done` and `qed`
  produce no state output at all, and neither does a slow prover. The reply is
  now `The command at MyTheory.thy:42 is not a proof operation, so there is no
  proof state here.` The whole unit suite runs in half the time it did, because
  that grace period is gone from it too.

- Every other way a query can fail to produce a result now has its own answer
  rather than a timeout: the command has not finished evaluating, its evaluation
  was interrupted, the prover no longer holds its state, there is no theory
  context to search at all (which is where `end` leaves you), and so on. Each
  names the position it is about.

- New tool: **`isabelle_command_status`** — ask what state the command(s)
  covering each of several lines are in, in bulk, instead of discovering it by
  tripping over a refusal. One line per requested position: `processed`,
  `running for Ns`, `not evaluated`, `cancelled, re-evaluate to get a result`,
  `unknown, retry in a few seconds`, `no command`, `file not open`. A line may
  hold several commands — `lemma foo: "P" by auto` is two — and when they
  disagree the per-command breakdown is printed.

- Every query tool now carries a footer naming the evaluation in flight, if any,
  so a query answered mid-evaluation says what else is happening.

- The evaluation result says which file and line it was evaluating towards, and
  a stopped evaluation says why it stopped rather than reporting the last thing
  it saw.

- The Isabelle-MCP component's ML prelude and its jar now check each other's
  version at startup and refuse to serve on a mismatch. They share three protocol
  commands and a reply format, and the prelude is not covered by the jar's
  recorded source hashes, so a skew between them would otherwise be a request
  that hangs with no trace.

- The extra text blocks appended to a tool result — debugger notices, the
  non-ASCII warning, the evaluation footer — now lead with a blank line, so
  clients that join a result's text blocks without a separator still render
  them as their own paragraphs.

- `anyio` is now a declared dependency. The code has always imported it
  directly (evaluation shielding uses `anyio.move_on_after`), but it only
  arrived transitively via `mcp`.

## 0.3.1

- The file watcher's inotify headroom check is now Linux-only. Off Linux the
  probe read a /proc path that does not exist, concluded "exhausted", and
  silently disabled event-driven file sync on every healthy macOS and Windows
  machine.

## 0.3.0

- **Isabelle-MCP no longer requires a patched Isabelle.** It ships its own Isabelle
  Scala component — `isabelle mcp_server`, a fork of Isabelle2025-2's `vscode_server`
  sources carrying the PIDE requests the stock one lacks — as a package asset, and
  registers it with Isabelle before the first session launch.

  The component declares `no_build = true` and carries a prebuilt jar, so
  `isabelle scala_build` skips it entirely: **nothing is compiled on the user's machine**
  (`site-packages` may be read-only — `sudo pip install`, Docker, Nix all work) and **no
  session heap is invalidated** (patching `src/Pure/**.ML` used to force a rebuild of Pure,
  HOL and every AFP session on the machine).

  Global proof cancellation — which needed a Pure ML patch — now comes from an ML prelude
  injected into the prover at startup (`ML_Process` `use_prelude`), built from the public
  `EXECUTION` API alone. Verified on a fully un-patched Isabelle: a runaway proof in an
  *imported* theory falls from ~3.2 cores to ~0.03 on cancel, which the perspective-restriction
  fallback provably cannot do. See `scala/Isabelle2025-2/docs/CANCELLATION.md`.

  Consequently the `my-better-isabelle-prover` dependency, the launch-time patch check and
  `--skip-patch-check` are **gone**.

  Upgrading from 0.2.x leaves `my-better-isabelle-prover` installed: pip does not remove a
  package just because nothing depends on it any more. It is inert — it only patches an Isabelle
  when you run it — but `pip uninstall my-better-isabelle-prover` is safe if you want it gone.
  Any patches it already applied stay applied; Isabelle-MCP works with a patched Isabelle too,
  it just no longer needs one.

- New: `isabelle-mcp uninstall`, which removes the component registration. `pip` cannot run
  uninstall hooks, so removing the package without it leaves a dangling entry — harmless
  (exit code stays 0) but Isabelle then prints `### Missing Isabelle component: …` on the
  stderr of every command until `isabelle components -x <path>`.

- **Isabelle2024 is no longer supported** — the fork is cut from 2025-2's VSCode sources, three
  of which do not exist in 2024. The last supporting commit is tagged
  `last-isabelle2024-support`.

- A prover that dies before the LSP handshake now reports its own words instead of a
  content-free 30 s `initialize` timeout.

## 0.2.1

- `isabelle_launch` no longer surfaces Isabelle's opaque `Return code: 127
  (COMMAND NOT FOUND)` sentinel when the prover dies before the PIDE
  handshake. That sentinel is a placeholder for the prover's real exit code,
  not a missing shell command; the usual cause is a missing, outdated, or
  incompatible heap somewhere in the session's dependency chain. On failure
  the launch path now consults the concurrent build probe and reports either
  the actionable `isabelle build -b ...` rebuild message (when the probe names
  unfinished sessions) or a generic "Isabelle failed to start the prover"
  message listing likely causes. Errors for undefined sessions are unchanged,
  and the happy-path probe/start overlap is preserved.

## 0.2.0

- New `isabelle_find_theorems` tool: search the theorem database in the
  proof/theory context at a position (like Isabelle's `find_theorems`), with
  name/pattern/intro/elim/dest/solves/simp criteria. Requires
  `my-better-isabelle-prover>=0.1.1`, which ships the `PIDE/find_theorems`
  query patch the tool drives.
- Tool-call cancellation is now leak- and orphan-free. MCP runs each tool
  handler in an anyio cancel scope that re-delivers the cancellation at every
  checkpoint; the evaluation paths previously left `evaluation_state.active`
  stuck `True` on a cancel (wedging every later `isabelle_evaluate_to`) and
  could orphan auto-opened dependency documents on the server. Evaluation state
  is now reset synchronously before any cleanup await (covering the heap grace
  re-check and `cancel_evaluation`'s `force_interrupt`), auto-opened documents
  are tracked before the opening await and closed under a bounded shield so a
  cancel can neither skip nor hang their cleanup, and `open_document` registers
  the document before sending `didOpen` so a cancel there cannot orphan it.

## 0.1.4

- `isabelle_evaluate_to`/`isabelle_evaluation_status` no longer report a file
  `clean`/`complete` while a proof in it is still being checked. Completion was
  gated only on the destination line being reached, which ignores forked proofs
  still running earlier in the evaluated prefix; with the target at end-of-file
  the frontier could "arrive" while a mid-file proof was in flight, so the
  snapshot was taken before its failure surfaced — intermittently summarising a
  file as `clean` that actually had a failing `qed`/proof.
  Completion now additionally requires the whole evaluated prefix `[0, dest]` to
  be quiet (no running/unprocessed command). On reaching the destination the
  result is `complete` only if the prefix is quiet; otherwise it returns
  `in_progress` immediately, listing the still-busy lines as `running:` and a new
  `pending:` field (`FileSnapshot.pending`, the unprocessed prefix clipped to the
  destination), and the caller polls `evaluation_status` to convergence. A proof
  that ultimately fails now always surfaces its error in the final `complete`,
  never `clean`. Verified against a real `vscode_server` that PIDE delivers
  "leave running/unprocessed" and "become error" in the same decoration push, so
  a quiet prefix can never hide a just-failed command.

## 0.1.3

- Per-file snapshots now clamp decoration ranges to the current document length,
  so a tracker whose ranges outlive a file shrink can no longer surface phantom
  error/warning/running spans past EOF (e.g. the "cancel reports no evaluation in
  progress but the snapshot still lists running:N" contradiction). The start-skip
  + end-clamp is extracted into `processing.clip_line_range` and shared by
  `_build_file_snapshot`/`_line_spans` and `get_all_running_commands`.

## 0.1.2

- `isabelle_launch` now fails fast (~5s) when the session is not ready,
  instead of `vscode_server` silently building a missing heap for up to hours
  (it now runs with `-n`) or silently loading a stale one:
  - missing heap in the chain → the server's pre-handshake "Missing heap
    image" error is surfaced on the `initialize` request (previously a blind
    30s timeout), with the exact `isabelle build -b ...` command to run;
  - outdated/unverifiable heap → rejected after the handshake via the launch
    probe (`isabelle build -n -b -v -l`, a strict dry run), naming the
    unfinished sessions; bypassed with a warning when `-R`/`-A` is in the
    server's extra args (requirements-only mode needs no own heap);
  - undefined session name → the JSON-RPC error reply is reported as-is;
  - the probe failing to run at all (OSError/timeout) is now a launch error
    (fail-closed) rather than a silent degradation.
  The MCP server never builds sessions itself. Launch failures are cleaned up
  cancellation-safely (kill-first), so a half-started server can no longer be
  mistaken for a running one by the next launch; a crashed server is likewise
  detected and restarted instead of returning a stale no-op success.

- Decoration freshness is now a single GLOBAL edit clock instead of per-file
  stamps (review follow-up to the 0.1.1 latch fix). Any edit-send — didOpen,
  didChange (including force_interrupt's synthetic edit), or a detected
  change/deletion of an external import/.ML dependency (synced by the server's
  own File_Watcher; detected at tool-call entry) — distrusts every file's
  cached decorations for `ISABELLE_MCP_DECORATION_GRACE` seconds (default
  2.0: covers both the didChange publish chain ~0.6s and the external-dep
  worst chain ~1.1s with margin). This closes two holes in 0.1.1: editing A
  then immediately evaluating B, which imports A, could return a stale
  "complete" (a dep edited while an evaluation is already mid-flight is
  instead caught by the live theory_status dependency gate), and the stamp
  being silently dropped when a didChange preceded the file's first
  decoration push. An invalid grace env value now logs a warning and falls
  back to the default instead of crashing at import. A heap-precompiled
  file's evaluation now re-checks once past the grace gate before declaring
  the file divergent, so a concurrent edit elsewhere can no longer trigger a
  spurious "Evaluation abandoned" on an unmodified precompiled file.

- Unicode guard on every MCP push path: content read from disk
  (`open_document` didOpen, `sync_dirty_files` didChange — both the
  event-driven watcher sink and the tool-call stat backstop funnel through it;
  dependency files synced by the server's own File_Watcher are not covered) is
  checked for non-ASCII, off the event loop. Policy is ASCII-or-nothing: when
  converting every glyph with an Isabelle ASCII notation (`α`→`\<alpha>`,
  `⟹`→`\<Longrightarrow>`, `x₁`→`x\<^sub>1`, leading UTF-8 BOM stripped)
  yields a fully ASCII result, the file is atomically rewritten on disk via
  compare-and-replace (a concurrent external write aborts the rename instead
  of being clobbered — the modified-since-read fence), so disk, document
  model, and prover stay byte-identical (column positions included; the
  rewrite matches what the vscode_server's `Symbol.encode` already fed the
  prover, i.e. jEdit's save canonicalization, and also normalizes CRLF to LF).
  When non-ASCII remains after conversion (no symbol-table entry — e.g. CJK
  comments), the file is left untouched and the original is pushed; never
  writing a non-ASCII result makes rewrite feedback loops impossible. Each
  event queues a warning that a new server middleware appends to the next
  tool response, instructing the agent to write Isabelle ASCII directly and
  to re-read rewritten files; warn-only bullets are deduplicated per file
  until its non-ASCII character set changes. The server instructions now
  state the ASCII convention up front.

## 0.1.1

- Fixed the "Evaluation in progress" latch: completion checking used to demand
  a decoration push strictly newer than the evaluation start
  (`require_fresh_update`), but the server never re-sends unchanged
  decorations, so an evaluation whose decorations did not change reported
  "in progress" forever (snapshot showing `clean`, zero running commands),
  survived `cancel_evaluation`, and only a session switch recovered.
  Decoration-cache freshness now recovers by clock instead: every `didChange`
  we send stamps the file's tracker, and the cache is distrusted only for
  `ISABELLE_MCP_DECORATION_GRACE` seconds (default 1.0, covering the server's
  `vscode_input_delay` + `vscode_output_delay`) after the last stamp.
  Caret-only moves no longer invalidate the cache — stale decorations can only
  over-report unprocessed regions there, never fake completion — so
  re-evaluating an unchanged file completes immediately.

- `isabelle_launch` now verifies the my-better-isabelle-prover patches before
  spawning `isabelle vscode_server` and refuses to start an unpatched Isabelle
  (run `my-better-isabelle patch` to fix). The check runs the patch manager
  from the server's own environment (`python -m my_better_isabelle_prover`),
  so it does not depend on `my-better-isabelle` being on `PATH`. Skip with
  `isabelle-mcp --skip-patch-check` (for hand-patched setups the patch manager
  cannot recognize); `scripts/install.sh --skip-patch-check` passes it through.

## 0.1.0 (MVP)

- 10 MCP tools: 5 standard LSP + 3 PIDE extensions + 2 session management
- JSON-RPC 2.0 client for `isabelle vscode_server`
- Event-driven document open (waits for first publishDiagnostics)
- Pydantic structured outputs with 1-indexed positions
