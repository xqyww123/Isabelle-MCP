# Isa-LSP: MCP Server for Isabelle

MCP server bridging AI agents with Isabelle's theorem prover via its LSP implementation.

**Python ≥ 3.10 | v0.1.0 (MVP)**

## Quick Start

```bash
pip install -e ".[dev]"

# Claude Desktop config (~/.config/claude/claude_desktop_config.json):
{
  "mcpServers": {
    "isabelle": {
      "command": "isabelle-mcp",
      "args": ["-s", "HOL"]
    }
  }
}
```

## Running the server

```bash
isabelle-mcp -s HOL                              # stdio transport (default)
isabelle-mcp -s HOL-Analysis --http --port 8371  # shared HTTP server
isabelle-mcp -s HOL -- -o editor_output_state=true  # args after `--` go to Isabelle
```

| Flag | Default | Meaning |
|------|---------|---------|
| `-s`, `--session` | *(required)* | Isabelle session/logic, e.g. `HOL`, `HOL-Analysis` |
| `--http` | off (stdio) | Run as a shared HTTP server instead of stdio |
| `--host` | `127.0.0.1` | HTTP bind host |
| `--port` | `8371` | HTTP bind port |
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
| `isabelle_evaluate_to` | Evaluate the theory up to a line (auto-starts the prover) |
| `isabelle_evaluation_status` | Poll progress of a running evaluation |
| `isabelle_cancel_evaluation` | Cancel a running evaluation |
| `isabelle_hover` | Type info and documentation at position |
| `isabelle_definition` | Jump to symbol definition |
| `isabelle_local_occurrences` | In-file occurrences (definition + uses) of a local entity |
| `isabelle_diagnostics` | Errors, warnings, processing status |
| `isabelle_goal` | **Proof goals** — omit after_text for before/after diff |
| `isabelle_command_output` | Prover output messages |
| `isabelle_session_info` | Current session info |

All positions are **1-indexed**. File paths must be **absolute**.

PIDE tools (goal, command_output) are best-effort wrappers around async PIDE notifications and may time out.

_Design targets (not yet exposed as MCP tools): `isabelle_completions` and `isabelle_preview` — both already supported at the LSP-client layer — plus `isabelle_edit`._

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
