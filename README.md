# Isa-LSP: MCP Server for Isabelle

MCP server bridging AI agents with Isabelle's theorem prover via its LSP implementation.

**Python ≥ 3.10 | v0.1.0 (MVP)**

> ⚠️ **One agent per server instance.** This server holds a single Isabelle
> session with global mutable state — one set of open documents, one
> caret/perspective, and one evaluation in flight at a time. It is
> **single-threaded and not concurrency-safe**: pointing multiple agents at one
> instance, or interleaving concurrent requests, corrupts the evaluation/caret/
> document state with catastrophic, hard-to-debug results. The server runs over
> **stdio**, so each agent already gets its own dedicated server process (and its
> own `isabelle vscode_server`) — just don't share one or drive it concurrently.

## Quick Start

```bash
pip install -e ".[dev]"

# Claude Desktop config (~/.config/claude/claude_desktop_config.json):
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
| `--version` | — | Print the version and exit |
| `-- ...` | — | Everything after `--` is forwarded to `isabelle vscode_server` |

### Environment variables

These are read once at process startup; a connected agent cannot change them.

| Variable | Default | Effect |
|----------|---------|--------|
| `ISA_LSP_EVAL_POLL_INTERVAL` | `10` | Seconds an evaluate/poll call waits before returning `in_progress` |
| `ISA_LSP_DUMP` | unset | If set to a path, append a JSON wire-log of all LSP traffic (debugging) |

## Tools

| Tool | Description |
|------|-------------|
| `isabelle_launch` | Start (or restart) the prover for a session/logic (e.g. `HOL`, `Minilang`); **call this first** |
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
