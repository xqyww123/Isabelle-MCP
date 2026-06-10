# Changelog

## Unreleased

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
