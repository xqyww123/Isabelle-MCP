# Isabelle-MCP

[![PyPI](https://img.shields.io/pypi/v/isabelle-mcp)](https://pypi.org/project/isabelle-mcp/)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.12-blue)](https://pypi.org/project/isabelle-mcp/)
[![CI](https://github.com/xqyww123/Isabelle-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/xqyww123/Isabelle-MCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

MCP server that lets AI agents (Claude Code, Codex, …) drive the Isabelle
theorem prover through its LSP/PIDE commands — fully autonomously, with no
human in the loop.

## Purpose

This MCP server exists so that Claude / Codex can issue Isabelle LSP commands
**without any human mediation**. The entire Isabelle process is encapsulated
behind the MCP tools — it exposes **no UI to the user**. The agent works by
editing `.thy`/`.ML` files on disk and calling the tools to evaluate them and
query proof states; nobody watches or steers the prover interactively.

This server is **not designed for human–AI collaboration** (there is no
jEdit/VSCode front-end in the picture). It implements a single
AI ↔ Isabelle, no-human-in-the-loop model.

> ⚠️ **One agent per server instance.** This server holds a single Isabelle
> session with global mutable state — one set of open documents, one
> caret/perspective, and one evaluation in flight at a time. It is
> **single-threaded and not concurrency-safe**: pointing multiple agents at one
> instance, or interleaving concurrent requests, corrupts the evaluation/caret/
> document state with catastrophic, hard-to-debug results. The server runs over
> **stdio**, so each agent already gets its own dedicated server process (and its
> own `isabelle mcp_server`) — just don't share one or drive it concurrently.

> [!IMPORTANT]
> **No Isabelle patch is needed.** Earlier versions required one: the server drove the
> stock `isabelle vscode_server`, which lacks the PIDE requests it needs, so the
> distribution had to be patched. It no longer does. Isabelle-MCP now ships its own
> Isabelle Scala component — `isabelle mcp_server` — and registers it with your Isabelle
> the first time you launch a session. Nothing is compiled on your machine (the component
> carries a prebuilt jar and declares `no_build = true`), so `site-packages` may even be
> read-only, and **no session heap is invalidated**.
>
> Requirements: **Isabelle2025-2**, with `isabelle` on `PATH` (or pinned with
> `isabelle-mcp install --isabelle-bin /path/to/Isabelle/bin/isabelle`). Isabelle2024 is
> no longer supported — see the `last-isabelle2024-support` tag.
>
> To undo the registration: `isabelle-mcp uninstall`. If you remove the package without
> it, Isabelle will print `### Missing Isabelle component: …` on every command until you
> run `isabelle components -x <the path it names>` — harmless, but noisy.

## Quick Start

```bash
pip install isabelle-mcp      # or: uv tool install isabelle-mcp

# register into Claude Code / Codex (auto-detects whichever is installed):
isabelle-mcp install
```

For Claude Desktop, register manually instead
(`~/.config/claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "isabelle": {
      "command": "isabelle-mcp"
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `isabelle_launch` | Start (or restart) the prover with the session/logic that fits the work (bare `Main` is only a minimal fallback); **call this first** |
| `isabelle_terminate` | Terminate the running prover (the MCP server stays up; you can relaunch) |
| `isabelle_evaluate_to` | Evaluate the theory up to a line; returns a per-file snapshot of errors / warnings / running command lines |
| `isabelle_evaluation_status` | Poll progress of a running evaluation (same snapshot) |
| `isabelle_cancel_evaluation` | Cancel a running evaluation |
| `isabelle_hover` | Type info and documentation at position |
| `isabelle_definition` | Jump to symbol definition |
| `isabelle_local_occurrences` | In-file occurrences (definition + uses) of a local entity |
| `isabelle_goal` | **Proof goals** — omit after_text for before/after diff |
| `isabelle_find_theorems` | Search the theorem database in the context at a position (Isabelle's `find_theorems`): by name, pattern, intro/elim/dest, solves, simp |
| `isabelle_command_output` | Prover output messages |
| `isabelle_session_info` | Current session info |

All positions are **1-indexed**. File paths must be **absolute**.

PIDE tools (goal, command_output) are best-effort wrappers around async PIDE notifications and may time out.

## Development

```bash
pip install -e ".[dev]"             # editable install from a checkout
pytest                              # unit tests
pytest -m integration               # requires running Isabelle
python -m mypy src/                 # type checking
```

## Architecture

```
server.py         FastMCP entry point — tool registration, lifespan
lsp_client.py     JSON-RPC 2.0 client for isabelle mcp_server
tools/            Tool implementations (one file per tool)
utils/            Position conversion, URI handling, HTML parsing
models.py         Pydantic output models
```

## License

MIT — see [LICENSE](LICENSE).
