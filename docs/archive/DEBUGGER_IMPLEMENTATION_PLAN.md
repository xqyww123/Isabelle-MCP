# ML Debugger Support — Implementation Plan

Status: **active, rewritten 2026-08-13** alongside the specification. Companion
to [`DEBUGGER_DESIGN.md`](DEBUGGER_DESIGN.md), which is authoritative for
*what* is built; this document plans *how*: what to verify first, which files
change, in what order. Where the two disagree, the specification wins.
[`DEBUGGER_REVIEW_AND_DECISIONS.md`](DEBUGGER_REVIEW_AND_DECISIONS.md) is the
historical record behind both.

Standing constraints: never add `-c` to `isabelle build`; rebuilding our own
jar is treated as costless; the Isabelle distribution is never modified; the
prelude and the jar version-check each other, so prelude changes bump
`mcp_prelude_version` and the Scala constant together; after editing `.ML`
sources, restart the REPL/server rather than rebuilding heaps.

## Execution status (2026-08-14) and Phase A hand-off

Done: Phase Y (commit `c1feaa1`); the specification rewrite plus two full
adversarial review rounds and a targeted third pass on the post-review
decisions, all folded (commits `14dc54c` and successors); **Phase A itself**
— the Scala requests, the prelude wrapper with the `PolyML` re-exposure and
the locals printer, the `lsp_client.py` notification branches, a rebuilt jar
passing `check_component.py`, and the probes as integration tests
(`tests/integration/test_debugger_probes.py`, 7 tests, all green in one run;
unit suite 495 green) — commit `62008cf`. Probe results and the discoveries
they forced are in "Phase A probe results (2026-08-14)" below.

**Current position: the Phase A repair round is IMPLEMENTED AND COMMITTED
(2026-08-14).** Commits, each with a rebuilt jar, `check_component.py`
green, the full probe file green in one process run, and the unit suite
green: `da0d043` (R1 dispatcher-confined backstop + R2's Scala abort check
+ R7 clear_output + R8 prover_exit-via-dispatcher), `f62c18f` (R3
self-compiling wrapper), `fb51934` (R4 acknowledged toggle), `9062072` (R5
prover-truth listing states; prelude version → "4" both sides), `e55c362`
(R6 not_stopped refusal), plus the R9 ranged-didChange commit and the R10
docs commit that follow it in history.  R9 landed `document_diff.py`
(UTF-16 converter + descending multi-hunk emitter), the `sync_dirty_files`
integration, the rejected-didChange full-text recovery hook, 17 unit tests
(500-case property test included), and
`tests/integration/test_ranged_sync_probes.py` (prefix reuse; multi-hunk
sync) — probe 6 was rewritten to the new reality in the same commit (a
downstream edit now PRESERVES armed serials; only an upstream edit
invalidates).  R11's probes landed incrementally with their items (R2's
indebted-abort probe included).  Still open from the round: R2's Python
retry loop ships with the Phase C abort tool.

**A post-review fix round followed (2026-08-17, all committed).** A 17-agent
two-turn adversarial review of the repair round produced four accepted
findings (six raw survivors, two pairs being duplicate discoveries); a
9-agent verification round then adjusted every fix before implementation.
The commits, each gated (jar/check_component where Scala changed, full
probe battery in one process run, unit suite):

- `ee2ed65` — F3: the listing answers `outdated` on a snapshot with pending
  edits (mirrors the toggle's pre-check; §7.1's status enumeration updated
  with the honest scope: ok-with-empty still occurs for not-yet-compiled
  commands and does not mean "no sites").
- `7a6b0dd` — F4: `force_interrupt`'s synthetic-space position goes through
  `document_diff.utf16_position` (the last raw position-emission site);
  astral-first-line unit test.
- `999a68a` — F1: the two-distant-edits probe now WITNESSES both hunks
  (frame eval `n * 100 = 500`; `output_at_position` read-back of
  `ranged_other = 8`; `ev._failed_count == 0`; whole-test caplog scan for
  "Failed to apply document change" — the originally planned
  `needs_full_sync` assertion was proven VACUOUS: the settle polling's own
  resync heals the flag before it can be read).  F2: the R11 discriminating
  leg via a sentinel-file-gated command (define; gate; call): while the gate
  holds the command unfinished its site lists as `unfinished`, a toggle is
  refused `unfinished`, and after the gate opens the ref reads False with
  the control toggle answering ok/was:False.  On mechanism absence the
  probe HARD-FAILS by user decision (a skip would silently retire the
  contract check).  `_breakpoints` gained the bounded retry on `outdated`.

Findings killed in that review (do not re-report): CR/`\r` divergence
(excluded by text-mode `sanitize_read`); timer-callback channel IO on the
Timer thread (real but contract-mandated pattern, copied from
`query_at_position`); shared-Query_Handler token collision (contract-
mandated, unreachable sequentially); one bad triple failing the whole
`breakpoint_states` batch (own-code-only input, replied `failed`);
timer-armed-before-register with non-positive timeout (client always sends
positive); recovery-hook flag clobber by the in-flight sync (causally
impossible on the single-threaded loop).

**Phase B landed 2026-08-18** as two commits: `82eaff6` (the six client
wrappers for the section-7.1 requests — wire params, tokens from the shared
query counter, replies returned as-is, non-dict replies collapsed to
`{status: crashed}`; both probe files migrated off raw `client.request` in
the same commit) and `05e8937` (the `debug` launch parameter: `-o
ML_debugger=true` in the spawn argv, the launch-identity error of spec §2.1
in `isabelle_launch`, `debug` in `SessionInfo`; probe fixtures switched to
`debug=True` so the battery exercises the plumbing; agent-facing wording
user-approved 2026-08-18).  A 12-agent two-turn adversarial review
(4 finder lenses, 2 default-refute skeptics per finding) confirmed ZERO
findings.  The four killed (do not re-report): `extra_args` can still
hand-write `-o ML_debugger=true` past the identity check (the contract keys
on the launch parameter; `extra_args` is the documented unmodeled escape
hatch); wrapper default timeout 30 s vs the design's 180 s (the 180 s lives
in the Phase C tool schemas; §7.1 pins no wrapper default); the
progress-monitored default wait vs long silent evals (unreachable — 30 s
prover default answers well inside the 120 s stall window; pre-existing
recorded wait policy); stale `debugger_threads`/histories surviving prover
teardown (predates Phase B, no Phase B consumer; the design already
mandates teardown clearing — a Phase C wiring duty, noted in Phase C
below).

**Phase C landed 2026-08-18** — registry, tools, instructions (details in
the Phase C section below, including the user decisions that overrode the
specification during the wording review).  A two-workflow adversarial review
on 2026-08-19 confirmed four defects; **the fix round of "Phase C review
round (2026-08-19)" below landed the same day** (implementation notes at the
end of that section).  **Phases D and E landed in full
2026-08-19** — D1 (commit `4863066`), D2 + D3 + the sentence retrofit
(`64b52ee`), the post-landing review fixes (`4fb89be`), then Phase E's
e2e tests and documentation (see both sections' notes). The debugger
plan is complete.  Still open
with the user: when to push (push only on explicit order; the parent-repo
gitlink bump follows the usual recipe).  The repair
round's contract remains in the "Phase A repair round" section; the full
review verdict of the FIRST review is archived in
[`DEBUGGER_REPAIR_REVIEW_VERDICT.md`](DEBUGGER_REPAIR_REVIEW_VERDICT.md).
Current gate numbers: unit suite 683 passing; integration battery 25
passing (tests/integration in one process run, incl. 13 debugger probes +
2 ranged probes and the file-sync/query e2e tests).

Concrete pointers a fresh context needs:

- Version gate: `ML/mcp_prelude.ML` (`val mcp_prelude_version`) and
  `src/language_server.scala` (`val prelude_version`) — currently `"4"`;
  bump BOTH together with any prelude change.
- Jar release recipe: `docs/COMPONENT_INSTALL_PLAN.md` §7 ("Release recipe
  for the jar") — scratch `USER_HOME`, `isabelle scala_build`, copy back,
  `scripts/check_component.py` gate. Never `-f`, never `-c`.
- New Scala source files must be added to
  `src/isabelle_mcp/scala/Isabelle2025-2/etc/build.props` `sources`.
- Probe tests run with:
  `PATH=…/contrib/Isabelle2025-2/bin:$PATH pytest tests/integration -m integration`
  (a bare `pytest` deselects them via `addopts`).
- The unit suite must stay green:
  `PATH=…/bin:$PATH python -m pytest tests/ -q` (512 passing as of the
  repair round).
- Commit on `master` directly (shared working tree; no branches, no stash,
  no `git clean`); push only `origin`, and only when asked.

---

## Phase Y — the YAML output change (precondition) — **done, commit c1feaa1**

Before any debugger code: the seven structured tools drop `output_schema` and
return YAML text (spec §4 preamble; decisions §2.9). Own change, own commit —
so the debugger tools are born into a settled output convention. `pyyaml`
joins `pyproject.toml` and the conda recipe; serialisation lives in one helper
in `utils/formatters.py`; `allow_unicode=True` is mandatory and pinned by a
test (Unicode round-trip precedent exists in the integration suite).

## Phase A — Scala requests, prelude wrapper, probes (former Phases 0 and 1, merged)

There is no throw-away probe scaffolding. The real Scala requests are written
first — they are thin adapters over `session.debugger` by design — and the
probes drive them raw from Python as **integration tests**, the way
`tests/integration/test_query_tools_e2e.py` drives
`PIDE/find_theorems_at_position` directly with no MCP tool in between. The
probes stay in the repository permanently: the assumptions they pin are
exactly what an Isabelle upgrade would silently break. They live in
`tests/integration/`, marked `integration`, deselected by default —
made true by `addopts = -m "not integration"` in `pyproject.toml` (until
2026-08-14 the claim was false: only CI's explicit flag deselected them, and
a bare `pytest` on a machine with `isabelle` on PATH ran them, whose global
state leaked into a unit test during Phase Y) — and skipped without
`isabelle` on PATH. Running them is explicit: `pytest tests/integration
-m integration`. To let the probes observe the server-pushed
`PIDE/debugger_state` / `PIDE/debugger_output` notifications, **the two
notification branches in `lsp_client.py` land in this phase** (they were
Phase B work; the rest of Phase B stays put). Launching with debugging needs
no new code: `isabelle-mcp -- -o ML_debugger=true` via the CLI extra-args
escape hatch.

Probe policy (project rule: measure, do not infer from source): every probe
uses an observable side effect plus a positive control, and waits generously
(≥60 s) before concluding a negative.

### Scala fork changes

All under `src/isabelle_mcp/scala/Isabelle2025-2/` (package `isabelle.mcp`).

- `src/lsp.scala` — extractor/emitter objects for the §7.1 messages, next to
  the existing `PIDE/*` extensions. Timeouts as JSON numbers of seconds,
  client-chosen, like `Query_Params`.
- `src/debugger.scala` (new) — the request table and message consumer of spec
  §7.2: `Synchronized` per-thread pending map (take-is-permission, verbatim
  from `query.scala`'s invariant), per-thread debt counters, output
  accumulation, `Event_Timer` backstop arm/cancel idiom. Written alongside
  `query.scala`, not merged into it; `query.scala` is not touched.
- `src/language_server.scala` — dispatch arms for the §7.1 requests; a
  consumer on `session.all_messages` (NOT `session.debugger_updates` — no
  payload, coalesced; and NOT a protocol handler — `Debugger.Handler` already
  claims both function names and duplicate registration throws) matching
  cheaply on `Markup.Debugger_State` / `Markup.Debugger_Output`; the
  check-register-send step executed dispatcher-side so the hit's entry
  `debugger_state` cannot be mistaken for a completion; implicit
  `session.debugger.init` before the first debugger action, re-issued by the
  session-ready hook; a functionless `Session.Protocol_Handler` registered
  for its `exit` drain (answer orphans as crashed).
- `src/vscode_rendering.scala` — a `breakpoint(range)` lookup mirroring
  `jedit_rendering.scala`'s, yielding `(Command, serial)` for toggling.
- `PIDE/debugger_breakpoints` returns the markup ranges and serials **as
  found** — the one-symbol shift correction of spec §3.3 (anchor at the
  range's end, line from the corrected position) is client-side, done by the
  Python anchor computation of Phase C, per spec §3.2/§7.1.
- `ML/mcp_prelude.ML` — `Isabelle_MCP.debug_eval` (spec §7.3): thread
  registration table keyed by the debugger's thread-name string, explicit
  `Thread_Attributes.private_interrupts`, **one outermost interrupt
  classifier** (deadline or abort → ordinary exception; else re-raise),
  deregistration mutually excluded with the abort sender, pending interrupts
  drained under `no_interrupts` on every exit path, the abort flag living in
  the registration entry, deadline/per-value bounds as raw
  `Event_Timer`/elapsed checks (never nested `Timeout.apply`, never scaled);
  a protocol command for the abort flag; the **`PolyML` re-exposure** (spec
  §4.10: compile `structure Isabelle_MCP_PolyML = PolyML` once under
  `Context.Theory (Thy_Info.get_theory "ML_Bootstrap")` — the raw namespace
  has only the four-entry stub); and the **locals printer** (`debugState` +
  `debugLocalNameSpace` + `printWithType` at `ML_print_depth`, per-value
  checks against the one envelope, `<printing timed out>` placeholders,
  emitted via `Debugger.writeln_message`). Bump `mcp_prelude_version` and
  the Scala constant together.
- Jar rebuild: the usual release recipe (`isabelle scala_build` against a
  scratch `USER_HOME`, copy back, `scripts/check_component.py` gate; never
  `-f`, never `-c`).

### Probes

Gates — if 1, 2 or 3 fails, stop and revisit the specification:

1. **Sites are visible and where we think they are.** With `ML_debugger=true`,
   evaluate an `ML ‹…›` block containing an indented statement, a column-1
   statement, and top-level `val`/`fun` declarations. Assert: `ML_breakpoint`
   markup is retrievable from the snapshot (positive control: decorations);
   every range is a single symbol; the indented site's range covers the last
   indentation space and the column-1 site's range covers the previous line's
   newline (the one-symbol shift) — or, if both cover the statement's first
   letter, the shift correction must be removed instead; top-level
   declarations yield no sites.
2. **A breakpoint stops a thread.** Enable one site, run code crossing it,
   assert a `debugger_state` with a non-empty stack arrives through our
   `all_messages` consumer.
3. **Go/no-go for the timeout design.** At a real hit, evaluate an
   **allocating** runaway under a 5 s `Timeout.apply` (e.g.
   `let fun f xs = f (1 :: xs) in f [] end`). Assert the round trip ends with
   a TIMEOUT error message and the thread is still parked (follow-up
   `print_vals` answers). Repeat on the same thread (the asynch-once re-arm
   via explicit attributes). If this fails, the eval/abort design of spec
   §7.3-§7.4 must be reconsidered before Phase C.
   The **allocation-free** worst case
   (`let fun f (i:int) = f (i+1) in f 0 end`) is a separate refinement
   measurement, not a gate: spec §7.4 already ships it as a stated known
   limit, and this probe only fixes the limit's wording — or deletes it, if
   the cut-off does land.

Refinement probes (wording, bounds, bookkeeping logic):

4. **One `debugger_state` per input, always last.** Drive `print_vals`, a
   normal eval, a raising eval, then `continue`; assert exactly one state per
   input, output-before-state, and thread absence after resume. A zero-output
   eval (`eval "()"`) completes as an empty success, not a timeout.
5. **Registration ordering.** A request issued immediately after the hit must
   not complete instantly off the entry `debugger_state`. The negative arm
   ("show the guard matters by removing it") is a **one-off design-time
   experiment** with a variant jar, recorded in this file's results section,
   not a permanent test — a checked-in test cannot run against an unguarded
   jar under the `check_component.py` gate, and the race window is not
   drivable from Python. The permanent residue is a Scala-side ordering
   assertion.
6. **Recompilation invalidates serials; explicit re-arming works.** Edit
   before the enclosing block and re-evaluate: serials change, stale toggling
   errors, entries are demoted with notices; then evaluate up to the definer,
   `isabelle_enable_all_breakpoints`, evaluate onward — the caller hits
   (§2.2 motion 3). Also confirm motion 2: an edit strictly after the definer
   leaves the breakpoint armed and the re-run caller hits with no re-enable.
7. **Cancellation's synthetic edit.** After `isabelle_cancel_evaluation`,
   check whether all serials died (decides the demote-after-cancel
   trigger of spec §5); assert threads left the hit table.
8. **Query tools under parked workers.** With N threads stopped (N up to the
   worker count), `isabelle_goal` / `isabelle_find_theorems` on processed
   lines must answer promptly; if they starve, implement the fail-fast
   sentence of spec §6.1 instead.
9. **Stepping.** Step off the last statement of a block: the did-not-stop
   outcome reports and retires the hit. Check whether the stepping flag leaks
   onto a later unrelated task of the same worker (stray-halt anomaly path).
10. **Status of a stopped command.** Record decorations /
    `PIDE/theory_status` during a hit — fixes the "paused" wording in
    `isabelle_evaluation_status`.
11. **Abort flag.** An on-demand abort ends a slow (allocating) eval with an
    error, thread still parked; refused with the no-evaluation error when
    nothing is being evaluated; a stale abort never reaches the next
    evaluation (flag lives in the registration entry).
11bis. **Locals via the eval verb — gates the locals design.** First the
    re-exposure: `Thy_Info.get_theory "ML_Bootstrap"` resolves at `--use`
    time in every launchable heap and the compiled
    `Isabelle_MCP_PolyML.DebuggerInterface` is the real one. Then: the
    printer sees the halted stack from within an eval (live stack = the
    loop-entry capture, same frame numbering), and its output matches stock
    `print_vals` byte-for-byte on the same frame **after stripping the
    `val it = (): unit` echo**; a value with a deliberately slow printer
    yields `<printing timed out>` while the rest print, and the outer
    deadline still fires during a slow value (the per-value check must not
    mask it). **If the re-exposure probe fails, spec §4.10's fallback
    applies** (stock `print_vals` verb, no prover-side locals timeout) —
    decide before Phase C.
12. **`all_messages` consumer cost** under a large evaluation.
13. **Frame position resolution.** Which frames resolve to `file:line` on the
    Scala side; how library-code frames look (fixes the placeholder wording).
14. **Edits while a thread is parked.** A background file-save resync edits
    the document during a hit: observe whether the stopped command's
    execution is discarded and the parked thread disturbed. (Restores the
    caret-move probe of the shelving record in its surviving form — no tool
    moves the caret during a hit any more, but background resyncs still edit
    the document.)

## Phase A probe results (2026-08-14)

All probes ran against the real prover (`tests/integration/test_debugger_probes.py`;
`PATH=…/contrib/Isabelle2025-2/bin:$PATH pytest tests/integration/test_debugger_probes.py -m integration`).
7 tests, all green in one process run (179 s). What they measured:

**Gate 1 — confirmed as designed.** `ML_breakpoint` markup is retrievable
through `PIDE/debugger_breakpoints`; every range is a single symbol; the
one-symbol shift of spec §3.3 is real (an indented statement's range covers
the last indentation space; a column-1 statement's range covers the previous
line's newline, crossing lines); top-level `val`/`fun` declarations have no
sites; inner lambdas contribute extra sites on the same line (e.g. the body
of `fn i => i + n` — the anchor-snippet machinery must expect several sites
per line as the norm).

**Gate 2 — confirmed.** An enabled site stops the thread; the
`debugger_state` arrives through the `all_messages` consumer and is forwarded.

**Gate 3 — confirmed, twice on the same thread.** An allocating runaway under
a 5 s envelope ends in an `Isabelle_MCP.debug_eval: TIMEOUT` error message
with the thread still parked and answering; the second round proves the
explicit `private_interrupts` re-arm. The allocation-free worst case was not
measured (separate refinement, spec §7.4 keeps the stated limit).

**Probe 11bis — the locals design holds, with one correction.** The
`PolyML` re-exposure works only after forcing `ML_write_global` back to true
(it is FALSE in theory `ML_Bootstrap`'s final context — the design doc's
"still true there" was refuted; spec §4.10 updated, prelude does
`Config.put_generic ML_Env.ML_write_global true` for the one compilation).
Locals through the eval verb match stock `print_vals` byte for byte after
the unit-echo strip. The per-value 5 s bound fires (`<printing timed out>`
while the rest print), and the outer deadline still cuts through a slow
value (3 s outer < 5 s per-value ends the whole listing as TIMEOUT). Note:
a 2e6-node raw term printed in well under 5 s — depth pruning is effective
on raw terms, so the slow-value scenario was manufactured with a printer
installed via the re-exposed `addPrettyPrinter` (`ML_system_pp` is a no-op
stub in user theory ML; the re-exposure is ALSO the only way user code can
install a raw printer — noted, not a goal).

**Probe 4 — confirmed.** Exactly one `debugger_state` per input, after the
output; a zero-output eval is an empty success. (The adapter emits the state
notification BEFORE the completion reply so the client's thread map is
current when a request returns — ordering chosen after a race surfaced.)

**Probe 6 — REFUTES spec §2.2 motion 2 under the current client.** Any disk
edit reaches the prover as a whole-document `didChange`, and the Scala model
turns range-less text into remove-all + insert-all (`Text.Edit.replace` in
`PIDE/text.scala` is not a minimal diff). Consequence, measured: after ANY
edit to the file, every command is re-created, the definer recompiles, every
breakpoint serial dies, the fresh sites come back unarmed, and the re-run
caller runs through the previously-armed site without stopping. Toggling the
old serial errors (`unknown breakpoint serial`). Motion 1 on the edited file
(evaluate to definer, arm the new serial, evaluate onward) is the working
recovery and is verified.

**Resolution (decided 2026-08-14, user-approved): the client will send
range-based `didChange`.** A three-arm follow-up experiment
(`scratchpad ranged_sync_probe.py`, run against the real prover) settled it:
with a RANGED edit of only the caller line, the definer's breakpoint serial
survived and stayed armed, a 20 s upstream command did NOT re-run, and the
still-armed breakpoint hit in **0.2 s** with no re-arming — motion 2 works
exactly as §2.2 describes once the edit is ranged. The control arm (ranged
edit strictly before the definer) re-ran the sleep and killed the serials —
chained downstream execution, bounding the win as prefix-only, again as §2.2
describes. So PIDE's own granularity was never the problem; the disk-edit
sync path was. Implementation lands in Phase B: `sync_dirty_files` computes
a minimal diff (old `doc.content` vs new disk text) and sends ranged
`contentChanges` instead of the whole document; spec §2.2 stays as written.
A source-and-archive verification pass also traced the whole pipeline
(remove-all+insert-all → one `Malformed_Span` → `chop_common` matches
nothing → empty reused prefix in `document.ML`'s `last_common`) and found
the two documents that had wrongly asserted otherwise about this client
(`docs/PIDE_MCP_COMPARISON.md` "neither re-runs a whole theory on every
edit"; spec §2.2 motion 2 as previously measured) — both were source-derived
projections never measured against this client's sync path.

**Probe 7 — confirmed.** Cancellation sweeps the parked thread out of the
hit table (`threads=[]`); the pre-cancel serial afterwards answers
"document snapshot is outdated" (the synthetic edit leaves the snapshot
outdated until a re-evaluation).

**Probe 9 — the stepping caveat is real.** Stepping works (`step` from
`val xs` stops again inside instrumented code, frame labels like
`probe_target(1)xs-(1)`). After stepping off the end the thread can stop
again without any armed breakpoint (observed inside the same function's
inner lambda after the loop momentarily saw the thread absent — the
empty-state/new-state race is real, and the thread-local stepping flag is
cleared only by `continue`). Sending `continue` recovers, exactly as spec
§4.12's anomaly wording assumes.

**Probe 10 — measured.** During a hit the theory reports plain
`running: 1, percentage: 68, ok: true` — nothing marks "paused"; the
"paused at a breakpoint" wording of `isabelle_evaluation_status` must come
entirely from our own hit table.

**Probe 13 — measured.** Hit-stack frame positions carry only
`offset`/`end_offset`/`id` properties (no `file`/`line`); the `id` is a
command/exec id, so file:line resolution needs a Scala-side snapshot lookup
(Phase C/D work; the notification schema already forwards the raw
properties).

**Probe 14 — measured.** An edit while a thread is parked (whole-document
sync of an append at the very end) retires the hit: the parked thread is
released and the thread map empties. Matches the hit-retirement trigger
"edit-superseded execution" in spec §6.1.

**Probe 5 and probe 12** — the registration-ordering negative arm (variant
jar) and the `all_messages` consumer-cost measurement were NOT run; the
permanent residue of probe 5 (dispatcher-side check-register-send) is in the
adapter, and no consumer-cost symptom appeared during the runs. Both remain
open as refinements, not gates.

**Test-harness findings worth keeping:** the probes bypass the MCP tool
layer, so disk edits must be pushed explicitly
(`client.resync_changed_open_documents()`) — without it nothing reaches the
prover and a probe can "pass" on a stale document (this produced a
false-positive motion-2 result in the first draft); and the evaluation
bookkeeping is module-global, so the fixture resets `ev.evaluation_state`
per test.

## Phase A repair round (2026-08-14, user-approved — THE NEXT ACTION)

The adversarial code review of commit `62008cf` (22 findings, 15 surviving three
refuters) plus three verification workflows produced this repair round. Every
item below was itself adversarially verified (37 attack findings, 29 surviving
two refuters, per-item judgment archived in full in
[`DEBUGGER_REPAIR_REVIEW_VERDICT.md`](DEBUGGER_REPAIR_REVIEW_VERDICT.md) — read
it for the reasoning behind any item; THIS section is the actionable contract).
The user approved the whole round on 2026-08-14; implementation starts here.
All source line references are as of `62008cf`.

Standing constraints (unchanged): never run `isabelle build` in any form
(binds every subagent); jar rebuilds via the release recipe only
(`docs/COMPONENT_INSTALL_PLAN.md` §7: copy component to scratch, strip
`no_build`, scratch `USER_HOME`, `isabelle scala_build`, copy jar back,
`scripts/check_component.py` gate; a working scratch setup may exist at the
session scratchpad's `jar-build/`); commit on `master`, never branch, never
stash/clean; push only when asked, only `origin`; agent-facing sentences are
drafted at implementation and shown to the user for approval before commit.

### User decisions recorded this round (do not re-open)

1. **Acknowledged toggle**: approved. Absolute-semantics prelude protocol
   command through the existing query-reply machinery.
2. **Abort**: outcome-based bounded retry, and after the verification team
   killed the Scala-side state machine (wrong-target kill — see R2), the
   retry loop lives in the PYTHON abort tool; Scala stays stateless.
3. **Listing state truth**: the user chose real prover reads over a
   registry-projection display — each listing resolves enabled-states from
   the actual breakpoint refs (R5). The Python registry remains the
   bookkeeping of record for REGISTERED breakpoints; a mismatch between
   registry expectation and prover truth becomes a debugger notice.
4. **`Isabelle_MCP_PolyML` global exposure**: accepted and documented in §7.4
   (any user ML could re-derive it in five lines; hiding is cosmetic; the
   probes rely on it).
5. **Ranged `didChange`**: approved, and approved to land WITH this round as
   its own commit (not deferred to Phase B). Spec §2.2 stays as written.
6. Autonomy granted for the abort item's implementation once reworked; all
   agent-facing copy still goes to the user verbatim before commit.

### R1 — dispatcher-side backstop bookkeeping  [debugger.scala]

Move the ENTIRE Event_Timer backstop callback body into
`session.send_dispatcher { ... }`, so all four mutators of `pending`/`debt`
(all_messages consumer, `start_eval`'s check-register-send, the backstop, and
R8's `prover_exit`) are confined to the session dispatcher thread. Identity
check: give `Pending` a fresh Scala-side serial (`Counter` — NOT the client
token, which has no uniqueness guarantee); the timer closure captures the
serial it armed for and, dispatcher-side, does look-up-COMPARE-remove:
`pending.value.get(thread)` → if the serial matches, remove + respond TIMEOUT
+ increment debt; else do nothing (no take-then-re-insert). This closes all
five confirmed races (stolen replies, stranded debt, busy-fence hole,
wrong-request TIMEOUT, channel IO on the shared Timer thread — Event_Timer
cancel returns false once fired, so a fired closure can run arbitrarily late).

### R2 — abort: stateless Scala + Python retry loop

Scala (`debugger.scala` `abort`, lands with R1's commit — two lines): the
current handler consults only `pending` (:311-323) and wrongly answers
`no_evaluation` for an indebted thread — precisely the runaway case abort
exists for. New check: thread has neither pending entry nor debt →
`no_evaluation`; else send `Isabelle_MCP.debug_abort` ONCE and reply
`aborting` immediately. No timers, no waiters, no outcome classification in
the reply (an aborted completion is wire-identical to a failed one; the
eval's own LSP reply carries how it ended).

Python (lands with the Phase C abort tool; wire is ready after R1): retry
loop in the tool — send `PIDE/debugger_abort`; wait min(~2 s, the TARGETED
eval's own outstanding reply); if that reply arrived, stop re-sending and
report settled (this pins the abort to its target and structurally closes
the wrong-target window: the tool never re-sends after the target settles,
and the caller is blocked inside the tool so no eval#2 can start); in the
debt case (eval already answered TIMEOUT by the backstop) keep re-sending
until the abort reply flips to `no_evaluation` (debt cleared — safe, the
busy fence refuses new evals while debt is owed); bound ~30 s total, then
report honestly ("abort requested repeatedly; the evaluation has not
ended" — never claim delivery; allocation-free-loop limit + global cancel
named). The ML-side silent no-op for an unregistered thread is load-bearing
staleness protection and stays. REJECTED alternatives (do not re-litigate):
prover-side pre-abort table (stale-abort landmine, needs token threading);
Scala-side retry state machine (re-sends across settlement boundaries kill
the next evaluation — the ML table is keyed by thread name only); raw
interrupt in the window (escapes error_wrapper, kills the command, poisons
the theory tail).

### R3 — self-compiling wrapper  [mcp_prelude.ML + debugger.scala]

Composed eval text becomes constant-shape; the agent's expression travels as
an ML string literal and is compiled INSIDE the protection:

- Eval: `val _ = Isabelle_MCP.debug_eval_string (Time.fromSeconds N) "<literal>";`
- Locals: `val _ = Isabelle_MCP.debug_eval (Time.fromSeconds N) (fn () => Isabelle_MCP.debug_locals F);`
- Prelude addition (flags record verified to compile field-for-field on
  2025-2):
  ```sml
  fun debug_eval_string timeout source =
    debug_eval timeout (fn () =>
      ML_Context.eval
        {environment = ML_Env.Isabelle, redirect = false, verbose = true,
         catch_all = false, debug = SOME false,
         writeln = Debugger.writeln_message, warning = Debugger.warning_message}
        Position.none
        (ML_Lex.read "val it = (" @ ML_Lex.read_source (Input.string source) @
         ML_Lex.read ");"));
  ```
- Literal encoder in Scala next to `print_vals_text`: `Symbol.encode` first,
  then per UTF-8 byte: 32-126 except `"` and `\` verbatim, everything else
  `\ddd` (exactly three decimal digits). Output is pure ASCII, so the later
  `Symbol.encode` in the input call is a no-op.
- DELETE the `strip_unit_echo`/`UNIT_ECHO` machinery entirely
  (debugger.scala:63,73,140,269,298-302): the `val _` envelope emits no echo,
  and a genuine `val it = (): unit` from the inner eval is legitimate output
  the old filter would have eaten. The design's Python token-balance
  pre-check is dropped (the literal closes the escape hole structurally; a
  token-unbalanced expression can at worst smuggle declarations that run
  INSIDE the protection and whose bindings die with the discarded context —
  honest §4.9 note).
- Two guards: a prelude comment stating that the envelope's silence rests on
  `verbose = true` flushing one EMPTY writeln that only
  `Debugger.writeln_message`'s `if msg = "" then ()` drops (probe pins it);
  and the Python tool layer refuses empty/whitespace-only `expr` (empty now
  compiles to valid `val it = ( );`).
- Registration entry, classifier, drain discipline: UNCHANGED.
- Residual window, stated in §7.3: before registration there remains the
  input-queue round trip + a linear lexer scan of the composed text + the
  frame-scope merge; an abort landing there is acknowledged and lost (the
  Python retry re-covers it within one period); the window can no longer be
  extended by expression content.

### R4 — acknowledged toggle  [mcp_prelude.ML + debugger.scala + lsp.scala]

Prelude protocol command via the existing `mcp_define` machinery, INLINE on
the protocol thread (no fork):
`Isabelle_MCP.toggle_breakpoint id node_name command_id serial state`.
Absolute semantics (`b := Value.parse_bool state`; idempotent retry). Resolve
via `mcp_resolve`; breakpoint via
`ML_Env.get_breakpoint (Context.Proof (Toplevel.presentation_context st))
(Value.parse_int serial)`; `NONE` → status `unknown_breakpoint` (the one new
word); `SOME (b, _)` → read previous value, write, `mcp_finish id "ok" []`
with the PREVIOUS value in the reply chunk (`query.scala` stays untouched —
`Query.Result.text` delivers it). Other statuses: existing query vocabulary
(`undefined`/`unfinished`/`interrupted`/`failed`/`crashed`; `cancelled` is
unreachable inline — not advertised).

Scala: `toggle_breakpoint` keeps `ensure_init` and the pre-checks, promoted
to statuses `file_not_open` / `outdated` / `unknown_breakpoint` (dual meaning
with the ML case — noted in §7.1); then the `query_at_position` pattern:
token registered in **the existing `query_handler` instance** — expose it to
`Debugger_Adapter` (a SECOND `Query_Handler` is impossible: duplicate handler
class/function registration throws at init, protocol_handlers.scala:24-28) —
Event_Timer at the client-chosen timeout answering `timeout`,
`protocol_command_args` send. All `session.debugger.toggle_breakpoint` /
`breakpoint_state` uses deleted; the `session.debugger.init`/`ready`
lifecycle STAYS (it installs the break hook, debugger.ML:246-257).
`lsp.scala`: `PIDE/debugger_toggle_breakpoint` gains `token` + `timeout`
params; reply `{status, was?}`. Existing probe helpers/assertions
(test_debugger_probes.py `_toggle`, `_enable_site_at`, :148-168, :185, :265)
migrate IN THE SAME COMMIT.

### R5 — prover-truth listing states  [mcp_prelude.ML + debugger.scala + lsp.scala]

New prelude protocol command, same machinery, batch:
`Isabelle_MCP.breakpoint_states id (node_name command_id serial)*` — for each
triple `mcp_resolve` + `ML_Env.get_breakpoint` + read `! b`; one reply chunk
encoding per-serial `true`/`false`/unresolvable(status-word). Folded INTO the
listing: `PIDE/debugger_breakpoints` gains `token` + `timeout` (the async
pattern requires them; today's synchronous reply would hang forever on a
wedged prover), becomes async via the same shared `query_handler` +
Event_Timer, and replies `{status, open, breakpoints:[{range, serial,
state}]}` — top-level status ok/timeout/crashed/outdated (outdated added in
the post-review fix round, mirroring the toggle's pre-check), `state` ∈
true/false/unresolvable(word). Serials/ranges still AS FOUND (shift correction stays
client-side). Registry discipline unchanged (armed recorded only on toggle
`ok`; mismatch with prover truth → debugger notice).

### R6 — refuse eval/print_vals on a not-stopped thread  [debugger.scala]

Dispatcher-side, before registering: `threads.value` has no entry for the
name → reply the NEW wire status `not_stopped` (do NOT reuse `resumed`,
which means "input delivered, expression may have run" — sharing the word
would lie about side effects). No registration, nothing sent. §7.4 gains the
two honest races: the narrowing does not close the resume-between-check-and-
dequeue window; and the benign inverse (an eval racing a brand-new hit whose
state has not arrived is refused although stopped — impossible when the
client acts on a received hit notification, since the state callback
precedes the check on the same dispatcher).

### R7 — bound the stock output buffer  [debugger.scala]

`session.debugger.clear_output(thread)` at THREE points: after a completed
round trip (handle_state answering a pending entry), when a thread leaves
the map (resume), and IN THE DEBT-CLEARING BRANCH (:143-151 — the indebted
thread is the abandoned runaway whose buffered output is most plausibly
huge). Verified protocol-free and ordering-safe.

### R8 — prover_exit via dispatcher  [debugger.scala]

`Exit_Handler.exit` body becomes `session.send_dispatcher { prover_exit() }`
(ghost-thread race: exit runs on the manager thread, session.scala:623-625,
and can be overtaken by queued state callbacks). Verified: every teardown
path still runs the posted closure (dispatcher drains its mailbox before the
shutdown sentinel; `adapter.exit()` runs before `session.stop()`).

### R9 — ranged didChange  [lsp_client.py; independent, parallel-safe]

Direction: `sync_dirty_files` computes a line-level diff
(`difflib.SequenceMatcher`) between `doc.content` (the sanitized old text —
exactly what the server holds) and the new disk text, and sends ONE
`didChange` carrying one ranged contentChange PER non-equal opcode, hunks in
DESCENDING position order (the server applies changes sequentially, each
against the already-edited model — verified vscode_resources.scala:186-200),
one version bump, one `note_edit_sent`. Single-contiguous-range was REJECTED
(two distant hunks — exactly what stat-backstop batching produces — would
re-run everything between them). Hard requirements, all from the verdict:

1. **UTF-16 columns**: LSP character offsets are UTF-16 code units and the
   server does Java String arithmetic (line.scala:164-235); astral glyphs DO
   reach doc.content through the unicode guard's warn-only paths. Build ONE
   shared offset→(line, utf16-column) converter; every emitted position goes
   through it; unit-test with an astral glyph before the edit point.
   Implement and test the converter FIRST — every EOF/clamp case falls out
   of it.
2. **Silent-rejection recovery**: a rejected ranged didChange is dropped
   server-side with only a window/logMessage (didChange has no reply) while
   the client has already committed `doc.content` — permanent silent
   divergence. KEEP the full-text didChange path as the recovery form, and
   add a divergence hook in `_surface_server_message`: on a type=1 message
   containing "Failed to apply document change" (stable text,
   vscode_model.scala:172), drop `stat_sig` and force a full-text resync of
   open documents (the force_interrupt self-healing pattern, :1225-1227).
   This also covers partial application of a multi-hunk list.
3. **Overlap clamp**: trim common prefix first, then common suffix over at
   most min(len(old),len(new))−prefix chars ("aba"→"ababa" otherwise emits
   start>stop, which the server rejects and the silent-drop swallows).
4. **EOF anchoring**: newline is separator, not terminator (line.scala:99);
   a hunk whose old side reaches EOF-without-trailing-newline anchors at
   (last_kept_line, utf16-length) carrying/omitting the leading "\n".
   Enumerate delete/replace-last-line, append-without-newline,
   add/remove-trailing-newline in unit tests.
5. **Wire-shape pin**: a malformed range object silently decodes as the
   FULL-DOCUMENT form (lsp.scala:293-295) — one unit test asserts the exact
   emitted JSON, plus a comment at the emission site.
6. **Property test**: randomized old/new pairs, diff emitter vs a Python
   reimplementation of `Line.Document.change` semantics.
7. Settled (no further verification needed): CRLF safe end-to-end; the diff
   base is `doc.content`; force_interrupt's synthetic-space healing now
   emits a minimal hunk (strictly better); the `content != doc.content`
   gate stays as the empty-diff guard.
8. Integration: move the ranged-sync experiment (scratchpad
   `ranged_sync_probe.py` — motion 2 restored 0.2 s, prefix reuse, upstream
   edit still invalidates) into `tests/integration/` as permanent probes;
   update `test_resync_detects_and_pushes_change` to assert the ranged
   shape explicitly; add a two-distant-edits e2e case asserting both apply
   and the middle commands did not re-run; `test_file_sync_e2e.py` stays
   green.

### R10 — docs and version (LAST, after wire shapes stop moving)

- §7.1: implemented reply shapes — eval `{status, messages:[{kind,text}]}`;
  listing `{status, open, breakpoints:[{range, serial, state}]}` (state
  present per R5 — an earlier draft said the opposite; R5 wins); toggle
  `{status, was?}` + token/timeout params on both; new words `not_stopped`,
  `unknown_breakpoint` (with its dual meaning noted), `aborting`.
- §4.13: rewritten to stateless-Scala + Python-retry abort; outcome
  classification deleted from the reply vocabulary.
- §7.3: residual-window sentence (R3). §4.9: smuggling note + wrapper-token
  compile errors + empty-expr refusal. §4.10: strip clause removed. §7.4:
  `Isabelle_MCP_PolyML` exposure note + R6's two races. §2.2: UNCHANGED.
- `mcp_prelude_version` → `"4"` and `Language_Server.prelude_version`
  together, ONCE, in the same commit as the round's LAST prelude change
  (R3/R4/R5 all touch the prelude — sequence them contiguously), with the
  jar rebuild and `check_component.py` gate in that commit.
- Also fix `docs/PIDE_MCP_COMPARISON.md`'s false claim ("neither re-runs a
  whole theory on every edit") — true again only after R9 lands.

### R11 — probes

The verdict's 20 live-breakpoint probes (envelope/echo 1-4 incl. the
writeln-empty-drop guard, frame semantics 5-9 incl. the end-to-end unicode
literal round trip, compile-phase containment 10-13, malformed input 14-15,
acknowledged toggle 16-20), plus: abort on an indebted thread replies
`aborting` (pins the R2 Scala fix); abort during the compile window succeeds
via the Python retry within ~2 periods (Phase C timing); later-poll
`no_evaluation` = settled; `not_stopped` refusal; `breakpoint_states` truth
(armed → true; after a refused toggle on a running command → false);
R9's test battery (see R9). Existing probes migrate with each wire change,
never after.

### Commit sequence

1. R1 + R8 + R7 + R2's two-line Scala check fix — one commit (dispatcher
   confinement is the foundation; everything else assumes it).
2. R3 (touches `Pending` alongside R1's serial — adjacent avoids churn).
3. R4, then R5 (R4 lands the query_handler exposure; R5 reuses it). Version
   bump + jar rebuild with the last of R3/R4/R5. Probe migrations in the
   same commits.
4. R6 (needs R1's dispatcher-side threads read).
5. R9 in parallel at any point (independent; converter + property tests
   before the emitter, then the recovery hook, then e2e).
6. R10 last. R2's Python loop ships with the Phase C abort tool.

Definition of done for the round: all commits on `master`; jar gate green;
FULL probe file green in one process run
(`PATH=…/contrib/Isabelle2025-2/bin:$PATH pytest tests/integration/test_debugger_probes.py -m integration`);
unit suite green (`python -m pytest tests/ -q`, 495+ tests); file-sync e2e
green; agent-facing sentences approved by the user before their commit.

## Phase B — Python protocol layer

Files: `src/isabelle_mcp/lsp_client.py`, `server.py`, `models.py`,
`tools/session.py`.

- Request wrappers for the §7.1 requests in `lsp_client.py`
  (request/response correlation is generic; the two notification branches
  already landed in Phase A).
- `debug` plumbing: `-o ML_debugger=true` in the spawn argv and the flag
  recorded on the client (`lsp_client.py`); **the launch-identity error** of
  spec §2.1 in `isabelle_launch` (`server.py` — reuse only when session name
  matches AND debug matches; a differing debug value on the same session
  errors both ways; `session_dirs` unchanged); `debug` in `SessionInfo`
  (`models.py`, populated by `tools/session.py`).

## Phase C — registry, tools, instructions

- New `src/isabelle_mcp/debugger.py` — registry (two states, manual arming,
  §5) with **one registry lock** across every read-check-mutate sequence,
  anchor snippet computation (whole tokens, unique-in-line, from the
  corrected position), `at_text` resolution with the resolves-differently
  refusal, the demote-and-notify bookkeeping (no background toggles),
  arming-time site resolution with same-site merging, hit table and `hit_id`
  lifecycle (cleared on every prover teardown), the notice buffer, and the
  refusal/report sentences (unit-tested verbatim, in the `query.py` style).
- `src/isabelle_mcp/server.py` — the twelve tools of spec §4, all
  `output_schema=None` with formatters in `utils/formatters.py`; notice
  delivery through the existing middleware pattern.
- `src/isabelle_mcp/instructions.py` — the debugger section teaching hit /
  frame / breakable site, the three working motions of spec §2.2 (and the
  one losing move), the manual arming rule of spec §5, the single-expression
  rule, and the discovery-first workflow (draft at implementation;
  user-visible text).

Both hand-off notes from the Phase B review are DONE: every debugger
request passes `request_timeout` (= prover timeout + 60 s, so the
progress-monitored wait can never flag a long silent eval as a stall), and
`_clear_session_state` now clears `debugger_threads` / the two histories and
calls `registry.on_prover_teardown()`.

### Phase C as landed (2026-08-18)

Implemented: `src/isabelle_mcp/debugger.py` (registry with one lock, anchor
snippets, `at_text` resolution, demote-and-notify, hit table + `hit_id`
lifecycle cleared on every prover teardown, notice buffer, the whole
sentence catalogue); ELEVEN tools in `server.py` (all `output_schema=None`,
text results); notice delivery + hit sync in `UnicodeWarningMiddleware`;
`cartouche`/`indent_rows`/`format_call_stack` in `utils/formatters.py`;
`BreakpointRef` input model in `models.py` (schema for
`isabelle_del_breakpoints`' list argument; the specification's "models.py
gains nothing" is about RESULT models — the twelve tools return text, and
that holds); the "ML debugger" section in `instructions.py`.  R2's Python
abort retry loop is implemented in `debugger.abort_eval_at_breakpoint`.
Gates: unit suite 615, integration battery 25, both green.

**User decisions during the wording review (2026-08-18) that OVERRIDE the
specification — do not "restore" these from §4:**

- `isabelle_abort_eval_at_breakpoint` is implemented but **not registered**
  as an MCP tool: an agent's own eval call blocks, so the tool only serves
  parallel tool-call clients; expose it when such a client exists.  The
  three sentences that used to name it were rewritten accordingly.
- §4.2's "the command is stopped at a breakpoint" refusal variant is
  **deleted**: the client can only guess the cause (parallel workers make
  the guess wrong), while the plain "still evaluating, retry" is never
  misleading, and a parked run surfaces through the hit notices anyway.
- §4.4's requirement that the tool description explain the reason tags is
  **dropped**, and the tags themselves are short: `not evaluated yet`,
  `still evaluating`, `code not found`, `state unknown, internal failure`
  (the last one covers both a lost request and an unusable prover answer).
  The listing output is self-explanatory.
- The single-expression rule of §4.9 is **taught nowhere** (neither the tool
  description nor the instructions): a bare declaration costs one compile
  error, which is cheaper than a preventive sentence on every call.
- Anchor snippets carry **at least three word tokens** (`ANCHOR_MIN_WORDS`;
  symbols ride along uncounted), not merely the shortest line-unique run —
  a bare `val` reads the same on every line of a `let` block.
- A failed site listing tags the file's breakpoints `state unknown,
  internal failure`; only a prover reply saying the file is not open tags
  them `not evaluated yet` (`FileNotOpenInProver` distinguishes the two).
- Demotion is worded `no longer works ({tag})`, never "demoted to pending";
  `armed` / `set` / `no longer works` are the agent-facing state words.
- Hit reports carry `hit_id` + thread name and NO source position: frame
  positions resolve to `file:line` only when the prover's own frame
  properties carry them.  §4.8's `Foo.thy:14 before ‹…›` headers and the
  `(library code)` placeholder wait for Phase D's Scala-side position
  resolution.

Timing policy (user-approved 2026-08-18): listing/toggle 30 s prover-side;
eval/locals 180 s by default (agent-chosen); `request_timeout` = prover
timeout + 60 s on every request; step and continue wait 30 s; the abort
loop re-sends every 2 s for at most 30 s.

Known gap for Phase E: the `.ML` listing path has never been measured
against a real prover (the specification marks it "(probe)"); its sentences
are written but untested.

### Phase C review round (2026-08-19) — THE NEXT ACTION, user-approved

Two adversarial-review workflows over the Phase C commits (`3b48367..HEAD`):
16 agents (4 finder lenses, then 2 default-refute skeptics per finding) then
12 agents (skeptics for the uncapped findings + 2 validators per proposed
fix); 9 raw findings, 4 distinct defects survived.  The fixes below are the
VALIDATED versions — each was attacked by two agents and hardened where they
found a hole.  All line numbers are as of commit `3e35c0f`.

**Sequencing:** F1 (identity equality) FIRST — F2's hardened form relies on
it.  Then F2, F3, F4.  One commit for the four fixes plus their tests, one
for the plan-doc update.  Gates: unit suite (from 615) and the FULL
integration battery in one process run; the battery must be re-run because
`step_at_breakpoint` changes.

#### F1 — `Breakpoint` must compare by identity  [debugger.py:491]

Defect (confirmed, HIGH): `Breakpoint` is a plain `@dataclass`, so it has
value equality, and `list.remove(x)` deletes the FIRST equal element.  For
two field-identical entries that is the entry the code intends to KEEP:
`_record_armed` (:825) then mutates a detached object while the stale twin
survives in the registry, so the prover site is ON with no armed entry
behind it — `disable_all_breakpoints` skips it and sends no toggle, breaking
the design's projection invariant.  Same pattern at `_merge_same_site`
(:1149) and `del_breakpoints` (:892).

Fix: `@dataclass(eq=False)` on `Breakpoint`, with a comment saying the three
removal sites depend on identity semantics.  Validated by both agents as
strictly better than deleting by an `is`-found index (one change fixes all
three sites and turns `entry in registry.entries` at :1148 into the identity
test it was always meant to be).  Verified: nothing in `src/` or `tests/`
relies on value equality of `Breakpoint`; the class merely becomes hashable
(unused).  `Hit` and `Site` keep value equality — they are never removed
from a list by value.

#### F2 — `_record_armed` mints undeletable duplicates  [debugger.py:815-830]

Defect (confirmed, HIGH): the duplicate-detection predicate matches an
existing entry only by equal serial, or by line+anchor when the entry is
PENDING.  An ARMED entry whose serial died (normal after any re-execution of
the enclosing command; nothing in Phase C demotes it — that is Phase D)
matches neither, so a second entry with the identical file, line and anchor
is appended.  `del_breakpoints` then refuses both as ambiguous, and its
advice to pass `at_text` cannot disambiguate.  (Recovery exists —
`enable_all_breakpoints` re-resolves and merges — but nothing tells the
agent that.)

Fix (HARDENED per both validators — the naive "drop the PENDING conjunct"
form leaks a live site):

1. Match on `e.file_path == file_path and e.anchor and (e.serial ==
   site.serial or (e.line == site.line and e.anchor == site.anchor))` —
   i.e. drop the `e.state == PENDING` conjunct AND require a non-empty
   anchor, so the degenerate empty snippet (`anchor_snippet` returns `""`
   when the client's line text is shorter than the site's offset, e.g. a
   drifted `.ML` blob) can never act as a wildcard key.
2. BEFORE rebinding or removing any matched entry, switch off every
   abandoned LIVE site: for each matched `e` with `e.serial is not None and
   e.serial != site.serial`, send `_toggle_site(client, e.file_path,
   e.serial, False)` best-effort (suppress `IsabelleToolError`, log it).
   Without this step the hardened predicate can swallow an entry whose old
   serial is still live, leaving an enabled site with no entry — exactly the
   invariant break F1 fixes elsewhere.
3. Remove the extras by identity (free once F1 lands).

Deliberately NOT fixed here (root cause is Phase D's demote-on-evaluation
hooks, and heuristics would be worse than the disease): a duplicate can
still be minted when the recorded LINE has drifted (an edit above the
breakpoint) or when the ANCHOR text changed for the same statement.  Record
this in the Phase D section as a task the demote hooks subsume.

#### F3 — a successful step reports a dead hit id and a stale stack  [debugger.py:1377-1381]

Defect (confirmed, HIGH): `hit.stepping = False` runs BEFORE
`registry.sync_hits(client)`, so the step's own transient "thread absent"
notification (Isabelle's debugger loop always emits one on the step verb) is
replayed with the flag already cleared: `sync_hits` retires the hit with a
false "hit ended" notice and the following re-stop notification mints a NEW
hit.  The tool still returns `Hit h1 stopped again.` for the retired id and
renders h1's PRE-step call stack.  Happens on EVERY successful step, in all
three modes.

Fix (per both validators — MOVE, do not delete): in the "stopped again"
branch, call `registry.sync_hits(client)` FIRST (the transient absence is
suppressed while `stepping` is still True, and the re-stop entry refreshes
`hit.stack` and clears `stepping` itself), THEN set `hit.stepping = False`
as a belt-and-braces clear, then render.  Deleting the assignment outright
was rejected: if no state notification arrives within the wait, `stepping`
would stay True forever and the hit could never be retired.

Also in the same commit (both validators raised it independently):
`hit.stepping = True` at :1359 is set before `await client.debugger_input`
at :1361 with no protection — a raising verb request leaves the flag set
forever.  Wrap so the flag is cleared on that failure path.

The "did not stop" branch keeps its current order (clearing before the sync
is intended there: the absence must retire the hit as `ENDED_STEP_LEFT`).

#### F4 — wording and dead code

- `NOTICE_MERGED` (:295) ends "merged into one **entry**"; `entry` is the
  internal word the user removed everywhere else.  Change to "merged into
  one **breakpoint**".
- The same-position merge (which F2 makes the normal shape of a merge) reads
  as a self-referential sentence.  Add a second constant, user-approved
  VERBATIM 2026-08-19:
  `duplicate breakpoint at {where} before {anchor} merged into one`
  and use it when the two merged entries share file, line and anchor; the
  existing sentence stays for the different-position case (design §5's
  original motivation: two entries resolving to one site after an edit).
- Delete `NOTICE_STRAY_HALT` (:304): no code path emits it, and the
  measured stray halt is absorbed by the step tool's `stepping` flag.  (The
  finding that it constitutes a lying notice was REFUTED; deleting the
  unused constant is housekeeping, not a behaviour change.)
- Fix the comment at :1273-1274 (`create_task so a cancelled tool call
  leaves the round trip running`): awaiting a task propagates cancellation
  into it, so that claim is false.  The real reason is that the handle is
  published for a CONCURRENT caller — `_check_eval_fence` (:1232) and
  `abort_eval_at_breakpoint` (:1402).  Comment only; the behaviour is
  correct because the authoritative one-evaluation-per-thread fence is
  prover-side (`scala/.../debugger.scala:405-406`).

#### Tests to add or fix with the round

- `tests/test_debugger.py`'s `FakeDebugClient` pushes only the re-stop state
  on a step verb; that low fidelity is why F3 escaped.  It must push the
  transient EMPTY state first, then the re-stop state, and the step test
  must assert the hit id is UNCHANGED and the rendered stack is the NEW one.
- F2: setting a breakpoint twice across a re-compilation (fresh serials)
  leaves ONE entry; the abandoned live site is toggled off; an empty anchor
  never matches.
- F1: two field-identical entries merge to the one the tool then reports as
  armed, and a following `disable_all_breakpoints` really sends the toggle.
- F4: the two merge sentences pinned verbatim; `NOTICE_STRAY_HALT` gone.

#### Findings killed in this round (do NOT re-report)

- A non-ok toggle demotes an entry although the prover site is untouched
  (serials are minted per compilation, so no live site is left behind).
- The eval/locals round trip dies with a cancelled tool call (true, but the
  authoritative busy fence is prover-side; the agent gets the accurate
  `EVAL_BUSY` sentence — only the comment was wrong, see F4).
- `NOTICE_STRAY_HALT` produces a lying notice (the stray halt is absorbed by
  the stepping flag; only the dead constant is real).
- Anchor prefix matching silently arms an unrelated site (not silent — the
  resolved line and anchor are printed in the same result and in the
  listing; the pick is decided by the approved nearest-recorded-line rule,
  which behaves identically under exact matching).
- `continue_breakpoint`'s level-triggered wait can burn the full 30 s when
  the thread re-stops immediately (found twice, real, LATENCY ONLY — the
  returned text is correct).  The user reviewed this on 2026-08-18 with the
  evidence and decided to KEEP the current implementation; do not re-open.

#### As landed (2026-08-19)

All four fixes applied as specified, with three implementation notes:

- `_record_armed` became **async** (F2's disarm-before-rebind step sends
  toggles); its one caller (`set_breakpoint`, under the registry lock)
  awaits it.
- The merge notice compares and prints the (line, anchor) pairs **as
  recorded before re-arming rewrote them**: `_arm_entry` overwrites
  `entry.line`/`entry.anchor` with the resolved site's values before
  `_merge_same_site` runs, so a post-arming comparison would classify every
  enable-all merge as a duplicate and made the different-position sentence
  print itself twice (the self-referential form the user flagged).
  `enable_all_breakpoints` snapshots the recorded pairs before its arming
  loop and passes them to `_merge_same_site`; the shared
  `_add_merge_notice` helper picks between the two approved sentences.
  (F1's identity semantics is what lets entries key that snapshot dict.)
- `FakeDebugClient.debugger_input` now emits the thread-absent full map
  itself on every resume verb (the wire fact), which subsumed and deleted
  the `_resume_on_continue` test helper.

Each fix was verified to be pinned by its new test: reverting F1, F2 or F3
in isolation makes the corresponding test fail.  Gates: unit suite 621
passing (615 + 6 new); integration battery 25 passing in one process run.

## Phase D — evaluation and cancellation integration

**Design finalized and user-approved 2026-08-19** (every decision below was
either taken verbatim from the specification or individually approved by the
user; the two open spec gaps and the demote-observation mechanism were
adversarially reviewed — provenance at the end of this section).
**Implementation awaits the user's explicit go-ahead.**

### D1 — frame position resolution (LANDED, commit `4863066`)

`thread_json` in `debugger.scala` resolves command-relative frame positions
(id/offset, probe 13) to file:line via
`Document.Snapshot.find_command_position` at forwarding time; unresolvable
frames keep no file/line. Jar rebuilt per COMPONENT_INSTALL_PLAN §7;
probe 13 asserts frame 0 resolves to the probe theory. Gates: unit 621,
battery 25.

### D2 — the demote-and-notify bookkeeping (the reviewed "Scheme A", final)

Spec §5 pins WHAT (observe site death → demote + one notice per state
change; never toggle; never arm); the HOW below was designed here and
reviewed by a 36-agent adversarial workflow (4 lenses → 2 default-refute
skeptics per finding; 9 killed, 4 distinct defects folded in).

**Event classification rule.** Events where site death is CERTAIN demote
directly at the event, wire-free, exactly the `on_prover_teardown` shape:
prover teardown (existing) and `isabelle_cancel_evaluation` (probe 7:
the synthetic edit invalidates EVERY serial; the listing answers
`outdated` until a re-evaluation, so reconciling by listing would burn the
full 20-retry loop per file and then mistag). Tag: `not evaluated yet`.
Events where death is UNCERTAIN (edits — probe 6: a downstream edit
preserves serials) verify by listing before any demotion.

**Dirty marks.** A registry-held set of realpaths. Marked by: every
didChange actually sent for an open document (both resync paths); a `.ML`
dependency blob whose stat signature changed (`_dependency_freshness_wait`
already detects it). Propagation: marking F also marks every file that
transitively imports F and holds armed entries (import graph from
theory_status). The `.ML` blast radius (accepted over-approximation, user
2026-08-19 "这点 dirty 是可以接受的"): a blob change marks ALL files
holding armed entries; theory-edit propagation additionally marks all
armed-entry `.ML` files (theory_status carries no blob↔loader edges — the
graph cannot express them).

**Consumed marks, never cleared afterwards.** Reconciliation POPS the dirty
set synchronously (before its first await); a concurrent event during the
listing round trip sets a fresh mark that survives to the next pass. This
shape removes the mark-clearing race outright (no generation counters).

**Reconciliation.** At the next tool call, in the middleware, after
`sync_hits`, BEFORE notices are drained: for each popped file that holds
armed entries, under the registry lock, `fetch_sites`; an armed entry stays
armed iff its recorded serial appears in the listing. Demotion tags:
serial absent from an ok listing → `not evaluated yet`; file not open →
`not evaluated yet`; the listing request itself failed →
`state unknown, internal failure`. `code not found` stays RESERVED for a
failed arming attempt inside the explicit tools (this keeps the fence's
exclusion of `code not found` sound). Reconciliation only demotes.

### D3 — evaluation integration (spec §6 verbatim; gap-fills approved)

**The current evaluation's theory set** (approved after two adversarial
verification rounds): the union of (a) realpath(target file); (b)
`auto_opened_files` (already realpathed); (c) realpath(node_name) of every
theory in the target's import closure (`_find_theory_name` →
`_get_recursive_dependencies` → `theory_map[dep].node_name`; deps absent
from the snapshot drop out — heap-precompiled code cannot hit); (d)
realpath(node_name) of every theory_status entry flagged `external` whose
`theory_name` is EMPTY — exactly the `ML_file`-loaded blobs ("all external"
was refuted: the external flag is never cleared, so it converges on
"everything not currently open"). Computed LAZILY — only when a new hit
must be classified — from the iteration's snapshot; when a new hit's
frame-0 file is NOT in the set, re-fetch theory_status ONCE and recompute
before finalizing "elsewhere" (closes the auto-open-await staleness
window). Classification: frame-0 file absent → fail open (end the wait);
realpath(frame-0 file) ∈ set → end the wait, lead with the hit report;
else → buffered notice, keep waiting. Wrap the classification realpath —
`ValueError` (embedded null byte) fails open, not into the
`BaseException` teardown.

**Two pinned orderings** (verifier-mandated): in `evaluate_to`'s entry,
`sync_hits` + the §6.1 hits-live refusal run BEFORE the active-evaluation
refusal (else the agent is told to cancel, which destroys the hits); the
hit-led wait-loop exit is a DISTINCT internal loop outcome checked before
the heap-abandonment branch (the agent-visible outcome stays the
`in_progress` family — user-approved; the bare status word never reaches
output).

**Hit-report anchor sourcing** (approved + verified, amendment folded in):
the `before ‹anchor›` phrase comes from the registry entry matching the
stop position — EXACTLY ONE armed entry at (realpath(frame-0 file),
frame-0 line), AND its recorded anchor text must occur on the current text
of that line (`_file_lines`; the guard converts the stale-armed-entry
wrong-anchor path into omission); zero or several matches, or the anchor
absent from the line → omit the phrase. Never computed fresh (only the
line is known, not the site). Render order: final `sync_hits`, then ALL
hit headers (including anchor decisions) in ONE synchronous pass, THEN the
concurrent implicit locals fetches; `registry.lock` is NOT held across the
fetches. Accepted residue (documented, no cheap fix): an orphaned enabled
site left by a logged best-effort disarm failure, on the same line as
exactly one armed entry at a different statement, prints that entry's
anchor.

**Implicit frame-0 locals** (spec §6.1): fetched for every hit in the
report, concurrently, one 10 s never-destructive bound (the prover-side
`debug_eval` deadline); registered in `hit.eval_task` (abortable, fences
agent evals); per-value 5 s bound inside.

**Cancellation** (spec §6.4 + approvals): path unchanged. Live hits get
`pending_ending` BEFORE the interrupt, so their retirement is SILENT (the
attributed-ending precedent — `continue` emits no notice either); the
ending clause survives only in the stale-id refusal. The cancel result
gains one line when hits were swept (sentence below). Afterwards the
certain-death demote-all of D2 runs.

**`evaluation_status`**: the paused section (sentences below) leads the
result whenever hits are live; the existing progress report follows.

**Fence** (spec §5 verbatim): trigger over the target's import closure —
enabled pending entries except `code not found`, plus armed entries whose
recorded position is no longer processed (`.thy` via the decoration
tracker; `.ML` via the blob's stat signature changed since arming — a new
`Breakpoint` field records the arming-time signature). Emitted as a result
line AND a debugger notice.

### The approved sentence catalogue (verbatim; unify before commit)

Global rules (user 2026-08-19): tool names in ALL runtime sentences carry
backticks (retrofit the whole Phase C catalogue; verbatim test pins updated
as a conscious edit); every concrete hit number is written `hit id {hit_id}`
(retrofit table below); manual pluralization everywhere.

New block shapes:

- Hit block header (replaces `Hit {hit_id}: thread {thread}.` everywhere):
  two lines `Hit id: {hit_id}` ␤ `thread {thread}`.
- Stack header (replaces the Phase C line; instructions and the `frame`
  parameter descriptions drop "innermost"/"where execution stopped" for the
  same words): `Call stack (frame 0 is the innermost, top of the stack;
  outer frames follow):`
- Several-hits refusal rows: `  hit id {hit_id} — thread {thread}`
  (header `Several hits are live; pass hit_id:` unchanged).

Phase C retrofit table (hit id + backticks; meanings unchanged; each
line below is one sentence, recorded verbatim — the backticks are part of
the sentence):

    There is no hit id {hit_id}. Call `isabelle_debug_state` for the live hits.
    Hit id {hit_id} has ended: {ending} Call `isabelle_debug_state` for the live hits.
    The thread of hit id {hit_id} is not stopped any more — the expression was never sent. Call `isabelle_debug_state` for the live hits.
    Resumed hit id {hit_id} (thread {thread}).
    Hit id {hit_id} did not resume within {seconds}s — the thread is still stopped. Call `isabelle_debug_state`.
    Hit id {hit_id} stopped again.
    thread {thread} stopped at a breakpoint — hit id {hit_id}; inspect with `isabelle_debug_state`
    hit id {hit_id} ended: {ending}

Backtick-only retrofits (DEBUG_OFF, ENDED_CONTINUED, the timeout
sentences, …) follow the global rule mechanically; the full catalogue diff
is shown to the user at gate time.

Phase D sentences (all user-approved verbatim 2026-08-19; backticks are
part of the sentences):

1. `evaluate_to` hit report, per hit — headline (anchor omitted per the
   sourcing rule drops the before-phrase; no position drops the location):

        Breakpoint hit: {file}:{line} before ‹{anchor}›.
        Breakpoint hit: {file}:{line}.
        Breakpoint hit.

   then the hit block (two-line header + stack), then `Locals of frame 0:`
   + rows. Tail once, after all blocks:

        Evaluation is paused, NOT finished. Inspect with `isabelle_eval_at_breakpoint` / `isabelle_locals_at_breakpoint` / `isabelle_step_at_breakpoint`, or resume with `isabelle_continue_breakpoint`.

2. Whole-fetch timeout (replaces the Locals section):

        Locals of frame 0 could not be fetched in time — use `isabelle_locals_at_breakpoint` to fetch them.

3. `evaluate_to` refusal while hits are live (leads with the hits; consumes
   their queued new-hit notices; anchor degradation as in 1; plural lead
   enumerates: `Evaluation is paused at 2 breakpoints — hit id h1 at …,
   hit id h2 at ….`):

        Evaluation is paused at a breakpoint — hit id h1 at Foo.thy:14 before ‹fold upd args›. `isabelle_evaluate_to` cannot run while a hit is live. Inspect with `isabelle_debug_state`, resume breakpoints with `isabelle_continue_breakpoint`.

4. Implicit-fetch collision refusal (the spec draft's abort-tool mention
   dropped — that tool is not exposed):

        An implicit locals fetch is still running on this hit — retry in a few seconds.

5. Fence warning (result line AND notice; "the run" as subject in both
   numbers):

        {N} breakpoints in the files this run executes are not armed — the run will not stop at them. Call `isabelle_enable_all_breakpoints` to arm them.
        1 breakpoint in the files this run executes is not armed — the run will not stop at it. Call `isabelle_enable_all_breakpoints` to arm it.

6. Cancellation ending clause (stale-id refusal ONLY — no delayed notice,
   the attributed-ending rule):

        it was swept up by `isabelle_cancel_evaluation`.

7. Cancel result line when hits were swept:

        2 hits were swept up; their threads are no longer stopped.
        1 hit was swept up; its thread is no longer stopped.

8. `evaluation_status` paused section (leads the result; hit blocks as in
   `isabelle_debug_state`; first line, then the blocks, then the closing
   line):

        Evaluation is paused at a breakpoint; it will not progress until the hit is resumed with `isabelle_continue_breakpoint`.
        Evaluation is paused at {N} breakpoints; it will not progress until the hits are resumed with `isabelle_continue_breakpoint`.
        Inspect with `isabelle_eval_at_breakpoint` / `isabelle_locals_at_breakpoint`, or step with `isabelle_step_at_breakpoint`.

### Implementation notes (landed 2026-08-19)

Micro-decisions taken while implementing, all below the design's waterline
(none touches an approved sentence or a pinned mechanism):

- The hit report consumes the reported hits' queued new-hit notices, the
  refusal's §6.1 rationale extended verbatim ("delivering the same hit
  twice in two formats would only confuse") — the report leads with those
  same hits.
- Implicit locals: a non-timeout failure (resumed/busy/crashed), and an ok
  reply with empty output, omit the Locals section — only the timeout has
  an approved replacement sentence, and nothing true was available to say.
- Fence bullet 2 (`.thy`): only NOT_EVALUATED and CANCELLED count as "no
  longer processed"; UNKNOWN (the global post-edit grace) and RUNNING do
  not — a warning that fires on every healthy run stops being read. A
  theory_status failure skips the fence for that run (logged).
- The fence reuses the current evaluation's theory set (one concept, one
  definition; auto-opened is empty at run start by construction).
- Dirty-mark propagation runs at reconciliation time from one fresh
  theory_status (marking stays synchronous — no wire call on any push
  path); a theory_status failure there over-approximates to every
  armed-entry file (the listing verification guards against false
  demotion either way).
- `on_prover_teardown` also clears the dirty set (all entries are pending
  then; the marks refer to a dead prover's serials).
- The cancel sweep waits a bounded `CANCEL_SWEEP_WAIT = 5 s` for the swept
  threads to leave the map before counting the result line; a hit still
  stopped after that stays live with its attributed ending standing. The
  demote-all runs BEFORE that wait, so a re-delivered cancel cannot cost
  it its turn.
- `_HitWatch`: when the elsewhere-verdict's one re-fetch itself fails, the
  stale verdict stands (the notice still delivers; degraded, not wrong).
- The hit exit is checked before the frontier decision in the wait loop —
  a parked fork keeps the prefix busy, and the frontier's plain
  in_progress return would bury the hit in a notice.

### Post-landing review round (2026-08-19, user-approved fixes)

A 24-agent two-turn adversarial debate (4 lenses → crude dedupe → 2
default-refute skeptics per finding; 16 raw → 10 debated → 6 upheld = 4
distinct concerns, 4 killed) reviewed the landed commit. The user approved
all four fixes:

- **A. `_HitWatch` baseline** — the classified set now starts EMPTY: the
  entry refusal proved the hit table empty, so every hit the wait loop
  sees is this run's to classify by construction; seeding from the live
  table at loop start silently exempted hits landing during the awaits
  between the refusal and the loop (open_document's diagnostics wait is
  the dominant window). Cost of the old shape: one degraded result and a
  wasted poll interval, not a lost hit.
- **B. Transactional dirty marks** — `reconcile_dirty` re-marks
  (`remark_dirty`) everything not yet verified when the pass exits early
  (the realistic trigger is a cancelled request mid-listing); a popped
  mark can now only vanish once its file was actually verified.
- **C. Wiring pins** — the three production `mark_dirty`/reconcile call
  sites (didChange in `sync_dirty_files`, the dependency-stat path, the
  middleware slot) were unpinned: a mutation deleting all three left the
  whole suite green. Three cheap pins added in the existing harnesses;
  each verified red with its wiring reverted.
- **D. `Breakpoint.arm(site)`** — the six-field armed-state transition
  (incl. the fence-critical `ml_sig`) was spelled out in two places; now
  one method on the dataclass, the counterpart of `registry.demote`.

Killed (do NOT re-report): the auto-start-path refusal-ordering claim
(spec §6.1 prescribes the behaviour); an elegance variant of B refuted on
misread evidence (OSError cannot escape — the wire wraps failures in
IsabelleToolError); a `_transitive_importers`-should-use-TheoryStatus
claim (false premise); render-order-unpinned (true, Phase E scope).

### Second post-landing review round (2026-08-20, user-approved fixes)

A second 24-agent two-turn adversarial debate reviewed the review-fix round
and Phase E (commits `4fb89be` + `53933ce`), this time with a dedicated
over-rigidity lens hunting constraint relaxations (the three user-rejected
relaxations fenced off). Outcome: 2 upheld, 8 killed, and the relaxation
lens produced ZERO proposals — every examined constraint's rationale
outweighed its cost. The user approved both fixes:

- **Doc tool counts aligned** — API_DESIGN §1 and ARCHITECTURE's header
  note + component diagram said "11 MCP tools" (stale since before the
  debugger: the base count was already 13; the diagram was missing
  `isabelle_find_theorems` and `isabelle_command_status`); all now say 24
  (2 lifecycle + 3 evaluation + 8 query + 11 ML-debugger), matching
  SPECIFICATION. `docs/PIDE_MCP_COMPARISON.md` is another agent's
  untracked file and was deliberately not touched.
- **`on_prover_teardown` delegates to `demote()`** — it had open-coded the
  pending transition behind a comment whose premise was false (path
  display needs only client.project_root and the filesystem, nothing the
  dying prover invalidates). `demote()` is now the only writer of the
  pending transition, the counterpart of `Breakpoint.arm`; teardown
  demotion notices gained the project-relative paths every other notice
  uses (pinned by a new test assertion).

Killed (do NOT re-report): pop-outside-registry-lock (trigger unreachable);
two variants of "unverified.discard is repeated" (the proposed reshapes
invert the transaction from fail-safe to fail-unsafe); the abort e2e 2 s
race (misreads the mechanism — registration does not need the stopped
thread to wake); ARCHITECTURE "stateless forwarding" wording (verified
accurate); fork_prover fixture duplication; the wiring pins' pop-based
isolation (cannot leak).

### Review provenance and killed findings (do NOT re-report)

Scheme A workflow (36 agents): killed — lock convoy over all tools;
theory_status-at-mark-time lock conflict; reconcile-after-call_next
staleness; document-close as a missing event; ok-with-empty demotion
wrongness (it is correct); fence-sees-pre-demotion-state ordering;
stale-`code not found` fence suppression; dirty-set-as-shadow-state;
no-correct-middleware-slot. Folded in — cancel direct demote (found by 3
lenses); `.ML` blob edges missing (2 lenses; resolved by the accepted
over-approximation, NOT by new wire edges); mark-clearing race (resolved
by consumed marks, simpler than the proposed generation counter).
Anchor verification (1 agent): CONFIRMED + the anchor-present-on-line
guard + realpath + the synchronous-render ordering; the orphaned-site
residue accepted. Theory-set verification (2 agents, adversarial second
pass): "all external" refuted (never-cleared flag), empty-`theory_name`
discriminator adopted; classification staleness re-fetch; the two pinned
orderings; the realpath `ValueError` fail-open guard. First-verifier
claims refuted by the second: "leftover external hits are rare" (they are
the steady state), "the iteration's snapshot is fresh enough" (the
auto-open awaits open a multi-second window).

## Phase E — tests and documentation (LANDED 2026-08-19)

- Unit tests: registry resolution and demote-and-notify bookkeeping, anchor
  snippets, sentence catalogue — landed across Phases C/D (683 as of the
  review-fix round).
- Integration tests beyond the probes — landed as
  `tests/integration/test_debugger_e2e.py` (7 tests driving the TOOL BODIES
  against a real prover, sentences asserted via the module constants):
  set → hit report → locals → eval → continue; step modes;
  enable/disable-all idempotence; motion 3 with the fence (the didChange
  dirty mark, the middleware's reconciliation pass invoked directly, the
  listing-verified demotion, re-arm, then the hit); cancel-while-stopped
  (swept line, silent attributed retirement, demote-all);
  backstop-timeout → busy fence → debt clearing → abort on a live runaway;
  two simultaneous hits via `Future.fork` (several-hits refusal rows,
  continue-all). Two empirical lessons recorded in the tests' docstrings:
  the hit-led exit needs the CALLER's line as the destination (with a
  farther destination PIDE can mark the destination line reached while the
  parked command sits earlier in the prefix — the wait then ends "arrived,
  not quiet" before the hit lands, and the hit correctly surfaces per §6.2
  as notice + paused section); and the fence after an upstream edit fires
  through bullet 1 via the D2 demotion, not bullet 2 (PIDE re-processes a
  small edited block in the background within seconds).
- Documentation — landed: `README.md` (debugger paragraph under Tools),
  `CHANGELOG.md` (the ML-debugging entry), `SPECIFICATION.md` (§3.4 catalog,
  the launch (session, debug) identity in §4.3.1, §6, §7 counts),
  `API_DESIGN.md` (§2.4 wire table), `ARCHITECTURE.md` (§2.7 subsystem) —
  each at pointer altitude, with `docs/archive/DEBUGGER_DESIGN.md` the
  authoritative specification, not duplicated. The MCP instructions were
  deliberately NOT extended: the user confirmed (2026-08-19) that the
  pause-on-hit behaviour is taught in-band by the approved runtime
  sentences (the report tail, the refusal, the paused section) and needs no
  pre-teaching.

## Ordering and gates

Phase Y, then A → E in order; each phase is a working increment. Explicit
gates:

- Phase A's probes 1–3 gate everything; failure means revisiting the
  specification, and the thin Scala layer is the only sunk cost.
- Phase A is complete only with a rebuilt jar passing
  `scripts/check_component.py` and the probe tests green.
- Phases B–D land behind the `debug=false` default: with debugging off, every
  new code path is inert.
