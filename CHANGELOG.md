# Changelog

## Unreleased

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
