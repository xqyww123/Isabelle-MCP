# Isabelle-MCP

MCP server that lets AI agents (Claude Code, Codex, …) drive the Isabelle
theorem prover through its LSP/PIDE commands — fully autonomously, with no
human in the loop.

**Python ≥ 3.12 | v0.1.0 (MVP)**

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
> own `isabelle vscode_server`) — just don't share one or drive it concurrently.

> [!IMPORTANT]
> **Patch Isabelle first — this server does not work on a stock Isabelle.** It
> drives `isabelle vscode_server` through PIDE LSP requests
> (`PIDE/output_at_position`, `PIDE/cancel_execution`, …) that only exist after
> applying the
> [my-better-isabelle-prover](https://github.com/xqyww123/my_better_isabelle_prover)
> patches:
>
> ```bash
> pip install my-better-isabelle-prover   # via pip or uv tool; needs Python ≥ 3.12
> my-better-isabelle patch                # apply patches + rebuild the Scala components
> my-better-isabelle status               # verify: every patch reports "applied"
> ```
>
> `isabelle-mcp install` checks this (when `isabelle` is reachable) and refuses to
> register the server against an unpatched Isabelle. The server re-checks at
> run time too: every `isabelle_launch` verifies the patches (via its bundled
> copy of the patch manager) and refuses to start an unpatched Isabelle —
> bypass with `isabelle-mcp --skip-patch-check` for hand-patched setups the
> patch manager cannot recognize. Compatibility notes
> (PEP 668, non-global Isabelle, …) are in [AGENTS.md](AGENTS.md).

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

The session/logic is **not** configured here — the agent picks it at run time by
calling the `isabelle_launch` tool (see Tools below).

## Running the server

```bash
isabelle-mcp                                  # stdio transport (the only transport)
isabelle-mcp -- -o editor_output_state=true   # args after `--` go to isabelle vscode_server
```

The server starts no prover at launch; the connected agent calls `isabelle_launch`
to start one for a chosen session.

| Flag | Default | Meaning |
|------|---------|---------|
| `install` | — | Register the server with Claude Code / Codex (see `isabelle-mcp install --help`) |
| `--version` | — | Print the version and exit |
| `--skip-patch-check` | — | Skip the my-better-isabelle-prover patch verification at session launch |
| `-- ...` | — | Everything after `--` is forwarded to `isabelle vscode_server` |

### Environment variables

These are read once at process startup; a connected agent cannot change them.

| Variable | Default | Effect |
|----------|---------|--------|
| `ISABELLE_MCP_EVAL_POLL_INTERVAL` | `10` | Seconds an evaluate/poll call waits before returning `in_progress` |
| `ISABELLE_MCP_DUMP` | unset | If set to a path, append a JSON wire-log of all LSP traffic (debugging) |

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
lsp_client.py     JSON-RPC 2.0 client for isabelle vscode_server
tools/            Tool implementations (one file per tool)
utils/            Position conversion, URI handling, HTML parsing
models.py         Pydantic output models
```

## License

See LICENSE.
