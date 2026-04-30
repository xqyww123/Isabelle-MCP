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
      "command": "isa-lsp",
      "env": { "ISABELLE_SESSION": "HOL" }
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `isabelle_hover` | Type info and documentation at position |
| `isabelle_completions` | Completion suggestions |
| `isabelle_definition` | Jump to symbol definition |
| `isabelle_highlights` | All occurrences in document |
| `isabelle_diagnostics` | Errors, warnings, processing status |
| `isabelle_goal` | **Proof goals** — omit column for before/after diff |
| `isabelle_command_output` | Prover output messages |
| `isabelle_preview` | HTML preview of theory |
| `isabelle_session_info` | Current session info |
| `isabelle_build` | Build session heap images |

All positions are **1-indexed**. File paths must be **absolute**.

PIDE tools (goal, command_output, preview) are best-effort wrappers around async PIDE notifications and may time out.

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
