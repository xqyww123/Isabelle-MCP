# ML Debugger Support — Implementation Plan

Status: **draft, shelved as of 2026-08-11.** Companion to
[`DEBUGGER_DESIGN.md`](DEBUGGER_DESIGN.md), which is the authoritative
specification (tools, semantics, wire protocol, breakpoint lifecycle). This
document only plans *how* to implement that specification: what to verify
first, which files change, and in what order. Where this document and the
specification disagree, the specification wins.

> Read [`DEBUGGER_REVIEW_AND_DECISIONS.md`](DEBUGGER_REVIEW_AND_DECISIONS.md)
> before acting on this plan: it revises the Phase 0 probe list, and its review
> concern (9) records a defect in Phase 1 below — the toggle is described as a
> bare flip with the state argument dropped, which would make a second
> `isabelle_enable_all_breakpoints` disable everything.

Section references (§) are into `DEBUGGER_DESIGN.md` unless said otherwise.

---

## Phase 0 — Probe experiments

The specification is derived from source study of Isabelle2025-2 and of this
repository. Project policy: Isabelle-MCP behavior must be measured, not
inferred from source. Each load-bearing assumption below gets a probe before
any implementation is built on it; every probe uses an observable side effect
plus a positive control, and waits generously (≥60 s) before concluding a
negative.

1. **Instrumentation reaches dynamically evaluated ML.** Launch with
   `-o ML_debugger=true` (already possible today via the CLI `--` extra-args
   escape hatch, no code change needed), evaluate a theory containing an
   `ML ‹…›` block, and confirm `ML_breakpoint` markup is retrievable from the
   snapshot on the Scala side. Positive control: markup of a kind we already
   consume (e.g. decorations) is present for the same region.
2. **A breakpoint actually stops a thread.** After `Debugger.init` and
   enabling one site, run ML that crosses the site; confirm a
   `debugger_state` protocol message with a non-empty stack arrives at the
   Scala `Debugger.Handler` (observable via `session.debugger.status`).
3. **What the rest of PIDE reports while a thread is stopped.** Record what
   `PIDE/decoration` and `PIDE/theory_status` say for the affected region
   during a stop — the "paused at breakpoint" wording of §6.1 and the wait
   loop's exit condition depend on this.
4. **Eval round-trip.** `eval` / `print_vals` output arrives as
   `debugger_output` protocol messages keyed by the thread name, within a
   bounded delay; measure a realistic timeout for the §7 rendezvous.
5. **Cancellation interplay.** With a thread stopped, confirm the existing
   cancellation path is indeed wedged, and that resuming all threads
   un-wedges it (§6.4).
6. **Recompilation invalidates serials.** Edit and re-evaluate the enclosing
   ML block; confirm the site serials change and `Debugger.breakpoint` on the
   stale serial fails the way §5(2) assumes.

Probes 1–2 gate the whole feature; 3–6 refine wording, timeouts, and the
reconciliation logic. Probe results get recorded in this document (append a
"Phase 0 results" section) before Phase 1 starts.

Probe vehicle: probes 1–4 need ad-hoc access to the running session from the
Scala side. Options: a temporary `PIDE/debugger_probe` request in the fork, or
a scratch build of the component with extra logging. Decide when starting
Phase 0; remove the scaffolding before release.

## Phase 1 — Scala fork

All changes under `src/isabelle_mcp/scala/Isabelle2025-2/` (package
`isabelle.mcp`). Prover interaction goes exclusively through the existing
`session.debugger` API (`isabelle.Debugger`, `src/Pure/Tools/debugger.scala`
in the distribution) — per the §7 design constraint, no debugger logic is
reimplemented.

- `src/lsp.scala` — extractor/emitter objects for the §7 messages, next to
  the existing `PIDE/*` extensions (currently at `lsp.scala:670-745`).
- `src/language_server.scala` —
  - dispatch cases in `handle` for the four requests;
  - `PIDE/debugger_breakpoints`: snapshot selection of `ML_breakpoint`
    markup over the requested range — model the lookup on jEdit's
    `JEdit_Rendering.breakpoint` (`src/Tools/jEdit/src/jedit_rendering.scala:208`
    in the distribution), which also yields the enclosing `Command` needed
    for toggling;
  - `PIDE/debugger_toggle_breakpoint`: re-locate the command for the serial
    in the snapshot, then `session.debugger.toggle_breakpoint(command, serial)`;
  - `PIDE/debugger_eval` / `PIDE/debugger_print_vals`: send the verb via
    `session.debugger.eval` / `.print_vals`, then await the matching
    per-thread output entries (timeout from probe 4) — same rendezvous
    pattern as the existing state-panel dance;
  - `PIDE/debugger_input`: forward resume verbs;
  - a consumer subscribed to `session.debugger_updates` forwarding thread
    stacks as `PIDE/debugger_state` notifications, and unconsumed output as
    `PIDE/debugger_output`;
  - implicit `session.debugger.init(...)` before the first debugger action,
    re-issued by the session-ready hook after a prover restart;
    `Debugger.exit` on shutdown only (§7).
- Jar rebuild: the usual manual release step —
  `scripts/check_component.py` gate, recipe in
  `docs/COMPONENT_INSTALL_PLAN.md`; CI check in
  `.github/workflows/ci.yml`.

## Phase 2 — Python protocol layer

`src/isabelle_mcp/lsp_client.py`:

- The notification dispatch is a hardwired `if/elif` chain
  (`_handle_notification`, `lsp_client.py:825`); unknown methods are silently
  dropped. Add branches for `PIDE/debugger_state` and `PIDE/debugger_output`.
- Request wrappers for the four §7 requests (the request/response path is
  generic — correlation by id — so no dispatch change is needed there).
- `isabelle_launch(debug=true)` plumbing: append `-o ML_debugger=true` to the
  spawn argv (`start()`, `lsp_client.py:354-379`); record the flag on the
  client so tools can fail fast when it is off (§2.1).

## Phase 3 — Registry and tools

- New `src/isabelle_mcp/debugger.py` — the breakpoint registry (§5): entry
  store, `at_text`/first-site resolution (§3), anchor-snippet extraction from
  file content, reconciliation with notices, the debug-notice buffer (§6.3),
  and the stopped-thread state fed by `PIDE/debugger_state`.
- `src/isabelle_mcp/server.py` — the ten tools of §4 with the exact schemas
  of the specification; `debug` parameter on `isabelle_launch`; the fail-fast
  guard.
- `src/isabelle_mcp/models.py` — result models for
  `isabelle_list_breakpoints` (§4.4) and `isabelle_debug_state` (§4.7),
  including the `notices` field.
- Output style split per §4: those two tools are structured; the other eight
  are text results (`@mcp.tool(output_schema=None)` + formatter), matching
  the repository's existing convention (structured for enumerable query
  results, text for narrative reports). Formatters live with the existing
  ones in `utils/formatters.py`.

## Phase 4 — Evaluation and cancellation integration

`src/isabelle_mcp/evaluation.py`:

- Third exit condition in the `evaluate_to` wait loop: stopped threads
  (fed from the `PIDE/debugger_state` handler), producing the §6.1 hit report
  (including the automatic frame-0 locals, which is one
  `PIDE/debugger_print_vals` round trip).
- "Paused at breakpoint" section in `evaluation_status` (wording informed by
  probe 3).
- Reconciliation hook: run after every completed evaluation round, and
  cheaply from the per-tool-call freshness path (§5, §6.2).
- `cancel_evaluation` extension (§6.4): resume all stopped threads first —
  with armed sites temporarily disabled and restored afterwards — then the
  existing cancellation path.

## Phase 5 — Tests and documentation

- Tests under `tests/`, following the existing pytest layout: unit tests for
  registry resolution/reconciliation (pure Python, no prover), plus
  integration tests behind the existing live-session test conventions for:
  set → hit → locals → eval → continue; step modes; disable_all/enable_all;
  recompilation re-arm; cancel-while-stopped.
- `README.md` feature section; MCP server instructions (tool docstrings are
  the primary agent-facing documentation — keep them aligned with §4
  descriptions verbatim where possible); `CHANGELOG.md`.

## Ordering and gates

Phases run in order; each phase is a working increment. Explicit gates:

- Phase 0 gates everything: if probe 1 or 2 fails, stop and revisit the
  specification.
- Phase 1 is complete only with a rebuilt jar passing
  `scripts/check_component.py`.
- Phases 2–4 land together behind the `debug=false` default: with debugging
  off, every new code path is inert, so partial progress never destabilizes
  the existing tools.
