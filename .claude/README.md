# Claude Code Configuration

This directory contains configuration for using the Isabelle LSP MCP server with Claude Code.

## MCP Server Configuration

The `mcp.json` file configures Claude Code to use the Isabelle LSP MCP server, which provides:

- **Hover information**: Type signatures and documentation
- **Code completion**: Context-aware suggestions for Isabelle/Isar
- **Go to definition**: Navigate to theorem/constant definitions
- **Document highlights**: Find all references to a symbol
- **Diagnostics**: Type errors and warnings
- **Proof state**: View current goals and proof context
- **Command output**: Execute Isabelle commands
- **Session management**: Build and manage Isabelle sessions

## Setup

### 1. Install Dependencies

First, install the package in development mode:

```bash
cd /path/to/Isa-LSP
pip install -e .
```

Or with development dependencies:

```bash
pip install -e ".[dev]"
```

### 2. Verify Installation

Check that the server is installed:

```bash
isa-lsp --version
```

### 3. Configure Isabelle Session

The default session is `HOL`. To use a different session, edit `mcp.json`:

```json
{
  "mcpServers": {
    "isabelle-lsp": {
      "env": {
        "ISABELLE_SESSION": "Main"  // Change to your session
      }
    }
  }
}
```

Common sessions:
- `HOL` - Higher-Order Logic (default)
- `Main` - Basic theories
- `Complex_Main` - Complex numbers
- `HOL-Analysis` - Real analysis
- `HOL-Algebra` - Abstract algebra

### 4. Start Using with Claude Code

Once configured, Claude Code will automatically start the Isabelle LSP server when needed. The server provides the following tools:

#### Available Tools

1. **isabelle_hover** - Get type information for symbols
2. **isabelle_completions** - Get code completion suggestions
3. **isabelle_definition** - Navigate to definitions
4. **isabelle_highlights** - Find all symbol references
5. **isabelle_diagnostics** - Get type errors and warnings
6. **isabelle_goal** - View current proof goals
7. **isabelle_command_output** - Execute Isabelle commands
8. **isabelle_preview** - Preview document (MVP limited)
9. **isabelle_session_info** - Get session information
10. **isabelle_build** - Build Isabelle sessions

## Troubleshooting

### Server Not Starting

If the server doesn't start, check:

1. Python path is correct
2. Dependencies are installed
3. Isabelle is installed and in PATH

Run manually to see errors:

```bash
python -m isa_lsp.server
```

### LSP Connection Issues

The server requires Isabelle's `vscode_server` to be available:

```bash
isabelle vscode_server --help
```

If not available, install Isabelle from: https://isabelle.in.tum.de/

### Session Build Errors

If session builds fail, try:

```bash
# Build session manually
isabelle build -b HOL

# Or use the MCP tool
isabelle_build(session="HOL", clean=True)
```

## Advanced Configuration

### Custom LSP Server Path

If using a custom Isabelle installation:

```json
{
  "mcpServers": {
    "isabelle-lsp": {
      "env": {
        "ISABELLE_HOME": "/custom/path/to/isabelle",
        "ISABELLE_SESSION": "HOL"
      }
    }
  }
}
```

### Development Mode

For development with auto-reload:

```json
{
  "mcpServers": {
    "isabelle-lsp": {
      "command": "python",
      "args": ["-m", "isa_lsp.server"],
      "env": {
        "PYTHONPATH": "${workspaceFolder}/src",
        "LOG_LEVEL": "DEBUG"
      }
    }
  }
}
```

## Resources

- [Isabelle Documentation](https://isabelle.in.tum.de/documentation.html)
- [MCP Specification](https://spec.modelcontextprotocol.io/)
- [Claude Code Docs](https://docs.claude.com/claude-code)
- [Project README](../README.md)
