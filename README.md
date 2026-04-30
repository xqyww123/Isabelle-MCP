# Isa-LSP: Model Context Protocol Server for Isabelle

**Version:** 0.1.0 (MVP)
**Status:** Development
**Python:** ≥ 3.10

Isa-LSP is a Model Context Protocol (MCP) server that bridges AI agents (like Claude) with Isabelle's theorem prover through its Language Server Protocol (LSP) implementation. It enables AI-assisted theorem proving by providing programmatic access to Isabelle's interactive proof environment (PIDE).

## Features

### Current Implementation Status

This README is the authoritative user-facing status for the current code. Some
files in `docs/` contain design notes and future targets; when they disagree
with this table, trust this table and the tests.

| Area | Tools | Status |
|------|-------|--------|
| Core LSP | `isabelle_hover`, `isabelle_completions`, `isabelle_definition`, `isabelle_highlights`, `isabelle_diagnostics` | Implemented and covered by unit/integration tests |
| PIDE extensions | `isabelle_goal`, `isabelle_command_output`, `isabelle_preview` | Implemented as best-effort wrappers around native PIDE notifications; see reliability notes below |
| Session management | `isabelle_build`, `isabelle_session_info` | Implemented |
| Document editing | none | Not implemented in the current server |

### Design Principles
- 🎯 **LSP-Native Only** - Only wraps features natively supported by `isabelle vscode_server`
- 📋 **Structured Outputs** - All tools return Pydantic models (never bare lists/primitives)
- 🔢 **1-Indexed Positions** - Consistent with lean-lsp-mcp (line 1, column 1 = first character)
- 🚀 **Session Reuse** - Long-lived LSP server for performance
- 🛡️ **Type-Safe** - Full type hints and validation

### Reliability Notes

- Standard LSP tools are the most reliable part of the project.
- PIDE state, command output, and preview depend on asynchronous Isabelle
  notifications. Timeouts are surfaced as tool errors instead of silently
  returning fabricated results.
- `isabelle_goal` opens a temporary PIDE state panel and uses the server-assigned
  panel id from `PIDE/state_output`.
- `isabelle_command_output` avoids reusing output from a different position; if
  Isabelle emits no fresh `PIDE/dynamic_output`, it returns no messages.
- Goal/context parsing from Isabelle's HTML is heuristic. `context` is currently
  returned as `null`.

---

## Installation

### Prerequisites

1. **Isabelle2024** (or later)
   ```bash
   # Download from https://isabelle.in.tum.de/
   # Or install via package manager
   ```

2. **Python 3.10+**
   ```bash
   python3 --version  # Should be >= 3.10
   ```

3. **Built Session Heap** (e.g., HOL)
   ```bash
   isabelle build -b HOL
   ```

### Install Isa-LSP

```bash
cd contrib/Isa-LSP
pip install -e .
```

This installs:
- `isa_lsp` Python package
- `fastmcp` and dependencies
- Entry point for MCP server

---

## Quick Start

### 1. Configure Claude Desktop

Add to your Claude Desktop configuration (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "isabelle-lsp": {
      "command": "python",
      "args": ["-m", "isa_lsp.server"],
      "env": {
        "ISABELLE_HOME": "/path/to/Isabelle2024",
        "ISABELLE_SESSION_PATH": "/path/to/your/isabelle/project"
      }
    }
  }
}
```

### 2. Restart Claude Desktop

The MCP server will start automatically when Claude launches.

### 3. Use in Conversation

```
You: I'm working on this Isabelle proof. Can you check the proof state at line 42?

[File: MyTheory.thy]
lemma example: "P ∧ Q ⟶ Q ∧ P"
  apply (rule impI)  # Line 42
  apply (rule conjI)
  by auto

Claude: [Calls isabelle_goal(file_path="/path/to/MyTheory.thy", line=42, column=None)]

Based on the proof state:
- Before the tactic: goal is "P ∧ Q ⟹ Q ∧ P"
- After the tactic: goal is "P ∧ Q ⟹ Q" and "P ∧ Q ⟹ P"

The `apply (rule impI)` successfully introduced the implication...
```

---

## Configuration

### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `ISABELLE_HOME` | Isabelle installation directory | (auto-detected) | No |
| `ISABELLE_SESSION_PATH` | Project root for theory files | Current directory | No |
| `ISA_LSP_LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) | `INFO` | No |

### Session Options

When calling `isabelle_build`, you can specify:
- `logic`: Session name (default: `"HOL"`)
- `session_dirs`: Additional session directories (default: `[]`)
- `clean`: Clean build (default: `false`)
- `verbose`: Verbose output (default: `false`)

---

## Usage Examples

### Example 1: Check Proof State

```python
# AI agent calls:
result = isabelle_goal(
    file_path="/path/to/theory.thy",
    line=42,
    column=None  # Omit column to see before/after
)

# Returns:
{
  "line_context": "  by auto",
  "goals_before": ["⋀x. P x ⟹ Q x", "R y"],
  "goals_after": [],  # Proof complete!
  "context": null
}
```

### Example 2: Get Type Information

```python
result = isabelle_hover(
    file_path="/path/to/theory.thy",
    line=15,
    column=8  # Position at start of "Suc"
)

# Returns:
{
  "symbol": "Suc",
  "info": "Suc :: nat ⇒ nat\n\nThe successor function for natural numbers.",
  "line_context": "lemma \"Suc n = n + 1\"",
  "diagnostics": []
}
```

### Example 3: Check for Errors

```python
result = isabelle_diagnostics(
    file_path="/path/to/theory.thy",
    start_line=10,
    end_line=20
)

# Returns:
{
  "success": false,
  "items": [
    {
      "severity": "error",
      "message": "Undefined constant \"foo\"",
      "line": 15,
      "column": 10,
      "end_line": 15,
      "end_column": 13
    }
  ],
  "processing_complete": true,
  "failed_dependencies": []
}
```

### Example 4: Code Completion

```python
result = isabelle_completions(
    file_path="/path/to/theory.thy",
    line=20,
    column=5,
    max_completions=10
)

# Returns:
{
  "items": [
    {
      "label": "Suc",
      "kind": "function",
      "detail": "nat ⇒ nat",
      "documentation": "Successor function",
      "insert_text": "Suc"
    },
    ...
  ],
  "line_context": "lemma \"S"
}
```

---

## Tool Reference

### Position Conventions

**IMPORTANT**: All line and column numbers are **1-indexed** (first line = 1, first column = 1).

```
Line 1: theory Example imports Main begin
        ^
        Column 1
```

### Optional Column Pattern

For `isabelle_goal`, omitting the `column` parameter gives you a before/after view:
- `goals_before`: State at line start (before tactic)
- `goals_after`: State at line end (after tactic)

This is useful for understanding how a tactic transforms the proof state.

### Tool Annotations

- 🟢 **Read-Only**: Tool only reads state, doesn't modify anything
- 🔵 **Idempotent**: Calling multiple times has same effect
- 🔴 **Destructive**: `isabelle_build` restarts the session

---

## Development

### Project Structure

```
contrib/Isa-LSP/
├── README.md
├── pyproject.toml
├── docs/
│   ├── SPECIFICATION.md      # Feature specifications
│   ├── ARCHITECTURE.md        # System architecture
│   └── API_DESIGN.md          # Implementation details
├── src/
│   └── isa_lsp/
│       ├── __init__.py
│       ├── server.py          # Main MCP server
│       ├── lsp_client.py      # LSP client wrapper
│       ├── models.py          # Pydantic models
│       ├── instructions.py    # User instructions
│       ├── tools/             # Tool implementations
│       │   ├── __init__.py
│       │   ├── hover.py
│       │   ├── completions.py
│       │   ├── definition.py
│       │   ├── highlights.py
│       │   ├── diagnostics.py
│       │   ├── goal.py
│       │   ├── command_output.py
│       │   ├── preview.py
│       │   └── session.py
│       └── utils/
│           ├── __init__.py
│           ├── errors.py      # Error handling
│           ├── uri_utils.py   # URI conversion
│           └── formatters.py  # Response formatting
├── tests/
│   ├── unit/
│   └── integration/
└── examples/
    └── test_theories/         # Example .thy files
```

### Running Tests

```bash
# Unit tests
pytest tests/unit/

# Integration tests (requires Isabelle)
pytest tests/integration/

# All tests
pytest
```

### Running Locally (Development)

```bash
# Start MCP server directly
python -m isa_lsp.server

# With debug logging
ISA_LSP_LOG_LEVEL=DEBUG python -m isa_lsp.server
```

---

## Troubleshooting

### Session Won't Start

**Problem**: `isabelle_build` fails with "Session not found"

**Solution**:
1. Check that Isabelle is installed: `isabelle version`
2. Verify session exists: `isabelle build -n -b HOL`
3. Build the session: `isabelle build -b HOL`

### LSP Timeouts

**Problem**: Tools return "PIDE timeout" errors

**Solution**:
1. Ensure document is not too large (< 1000 lines recommended)
2. Wait for PIDE processing to complete (check `processing_complete` in diagnostics)
3. Increase timeout (future enhancement)

### Diagnostics Not Updating

**Problem**: Old errors still showing after fixing code

**Solution**:
1. PIDE processes incrementally - wait 2-5 seconds
2. Check `processing_complete` flag in `isabelle_diagnostics`
3. Restart session if stuck: call `isabelle_build` again

### Proof State Shows HTML

**Problem**: `isabelle_goal` returns HTML tags in text

**Solution**:
- This is a parsing issue - report as bug
- Workaround: Look for text between goal markers (e.g., "1. ", "2. ")

---

## Limitations (MVP)

### Not Implemented (Phase 2)

The following features are **intentionally excluded** from MVP because they require complex implementation beyond LSP-native support:

- ❌ **File Outline** - Requires custom parsing (`textDocument/documentSymbol` not implemented)
- ❌ **Sledgehammer Integration** - Requires command execution framework
- ❌ **Find Theorems** - Requires command execution framework
- ❌ **Try Methods** - Requires transient file modifications
- ❌ **Term Goals** - No dedicated PIDE method
- ❌ **Code Actions** - LSP doesn't implement `textDocument/codeAction`

See `docs/SPECIFICATION.md` Appendix B for details on future enhancements.

### Known Issues

- **Large Files**: Processing time increases significantly for files > 500 lines
- **Build Time**: Session builds can take 1-10 minutes for large logics
- **Memory Usage**: Each LSP server instance uses ~500MB-1GB RAM
- **Single Session**: Only one logic session active at a time
- **PIDE Notification Timing**: Goal, command output, and preview tools may
  timeout if Isabelle does not emit the expected native notification.
- **Dynamic Output Identity**: Isabelle's `PIDE/dynamic_output` notification
  contains only HTML content, not the originating file/line. Isa-LSP serializes
  these queries and refuses to reuse output from a different requested position.

---

## Architecture Overview

```
AI Agent (Claude)
       │
       │ MCP Protocol
       ▼
   Isa-LSP MCP Server (Python)
   ├─ FastMCP
   ├─ Tool Handlers
   └─ LSP Client Wrapper
       │
       │ JSON-RPC 2.0
       ▼
   isabelle vscode_server (Scala)
       │
       │ PIDE Protocol
       ▼
   Isabelle Prover Process
```

For detailed architecture, see `docs/ARCHITECTURE.md`.

---

## Contributing

### Reporting Issues

Please report issues at: [GitHub Issues](link-to-issues)

Include:
- Isa-LSP version
- Isabelle version
- Error messages
- Minimal reproducing example (.thy file)

### Development Workflow

1. Fork the repository
2. Create a feature branch
3. Make changes with tests
4. Run full test suite
5. Submit pull request

### Code Style

- **Formatter**: black
- **Type Checker**: mypy
- **Docstrings**: Google style
- **Line Length**: 100 characters

---

## Comparison with lean-lsp-mcp

Isa-LSP follows the design patterns from `lean-lsp-mcp`:

| Feature | lean-lsp-mcp | Isa-LSP | Notes |
|---------|--------------|---------|-------|
| Tool Naming | `lean_*` | `isabelle_*` | System prefix |
| Position Indexing | 1-indexed | 1-indexed | ✅ Consistent |
| Output Models | Pydantic | Pydantic | ✅ Consistent |
| List Wrappers | `items` field | `items` field | ✅ Consistent |
| Goal Query | `lean_goal` | `isabelle_goal` | ✅ Same pattern |
| Optional Column | Yes (before/after) | Yes (before/after) | ✅ Same pattern |
| Diagnostics | `lean_diagnostic_messages` | `isabelle_diagnostics` | ✅ Same pattern |
| File Outline | ✅ | ❌ MVP | LSP support differs |
| Automation | `lean_hammer_premise` | ❌ MVP | Requires command execution |

---

## License

[To be determined]

---

## Acknowledgments

- **Isabelle Team** - For the excellent `vscode_server` LSP implementation
- **lean-lsp-mcp** - For the proven MCP server design patterns
- **Anthropic** - For the Model Context Protocol specification

---

## Resources

- **Documentation**: See `docs/` directory
  - `SPECIFICATION.md` - Feature specifications
  - `ARCHITECTURE.md` - System architecture
  - `API_DESIGN.md` - Implementation details
- **Isabelle**: https://isabelle.in.tum.de/
- **MCP Protocol**: https://modelcontextprotocol.io/
- **LSP Specification**: https://microsoft.github.io/language-server-protocol/

---

**Status**: MVP - standard LSP tools plus best-effort native PIDE notification wrappers
**Next**: Phase 2 will add command execution framework for sledgehammer, find_theorems, and try_methods
