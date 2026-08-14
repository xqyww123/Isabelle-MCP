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
decisions, all folded (commits `14dc54c` and successors). The design is
settled; **the next action is Phase A below.** All review rounds have
reported and been folded; no reviewer is outstanding. The one design element
still gated on measurement is the `PolyML` re-exposure for the locals
printer (probe 11bis — its fallback is stated in spec §4.10 and must be
decided before Phase C if the probe fails).

Concrete pointers a fresh context needs:

- Version gate: `ML/mcp_prelude.ML:17` (`val mcp_prelude_version = "2"`) and
  `src/language_server.scala:31` (`val prelude_version = "2"`) — bump BOTH to
  `"3"` with the prelude changes.
- Jar release recipe: `docs/COMPONENT_INSTALL_PLAN.md` §7 ("Release recipe
  for the jar") — scratch `USER_HOME`, `isabelle scala_build`, copy back,
  `scripts/check_component.py` gate. Never `-f`, never `-c`.
- New Scala source files must be added to
  `src/isabelle_mcp/scala/Isabelle2025-2/etc/build.props` `sources`.
- Probe tests run with:
  `PATH=…/contrib/Isabelle2025-2/bin:$PATH pytest tests/integration -m integration`
  (a bare `pytest` deselects them via `addopts`).
- The unit suite must stay green:
  `PATH=…/bin:$PATH python -m pytest tests/ -q` (516 tests as of Phase Y).
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

## Phase D — evaluation and cancellation integration

`src/isabelle_mcp/evaluation.py`:

- Third exit condition in the `evaluate_to` wait loop: a hit **in the current
  evaluation's theory set** (Scala-side position→node resolution); other hits
  become notices; unattributable hits fail open. The result leads with the
  hit report, including the implicit 10 s frame-0 locals fetch.
- "Paused at a breakpoint" section in `evaluation_status` (wording from
  probe 10).
- `evaluate_to` refused while any hit is live (the refusal lives in
  `evaluate_to` itself, so the query tools' auto-start inherits it), leading
  with the live hits and consuming their queued notices; plus the
  forgotten-re-enable fence of spec §5 — trigger computed over the target's
  import closure (excluding `code not found` entries, including armed
  entries at no-longer-processed positions), delivered both as a result line
  and as a debugger notice so the guard's auto-start path cannot drop it.
- Demote-and-notify hooks: on evaluation events, on resync, after relaunch,
  after cancellation (per probe 7). No background arming (spec §5).
- Cancellation itself unchanged (spec §6.4); hits retired as swept-up, the
  hit table cleared on every prover teardown path.

## Phase E — tests and documentation

- Unit tests: registry resolution and demote-and-notify bookkeeping, anchor snippets, sentence
  catalogue — pure Python, no prover.
- Integration tests beyond the probes: set → hit → locals → eval → continue;
  step modes; enable/disable-all idempotence; the three motions of §2.2
  (incl. explicit re-arming after an upstream edit); cancel-while-stopped;
  timeout and abort end-to-end; two hits at once.
- `README.md`, MCP instructions, `CHANGELOG.md`, and the three design docs
  (`SPECIFICATION.md`, `API_DESIGN.md`, `ARCHITECTURE.md`) updated for the
  twelve tools and the changed launch.

## Ordering and gates

Phase Y, then A → E in order; each phase is a working increment. Explicit
gates:

- Phase A's probes 1–3 gate everything; failure means revisiting the
  specification, and the thin Scala layer is the only sunk cost.
- Phase A is complete only with a rebuilt jar passing
  `scripts/check_component.py` and the probe tests green.
- Phases B–D land behind the `debug=false` default: with debugging off, every
  new code path is inert.
