# Changelog

## Unreleased

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
