# Isabelle LSP MCP Server Examples

This directory contains examples demonstrating how to use the Isabelle LSP MCP server with AI agents.

## Example Files

1. **simple_theory.thy** - A simple Isabelle theory file for testing
2. **proof_example.thy** - Example showing proof development workflow
3. **usage_example.py** - Python script showing direct API usage
4. **mcp_config.json** - Example MCP configuration file

## Running Examples

### 1. Install the package

```bash
cd /path/to/Isa-LSP
pip install -e .
```

### 2. Start the MCP server

```bash
# Using default HOL session
isa-lsp

# Or with custom session
ISABELLE_SESSION=Main isa-lsp
```

### 3. Connect with MCP client

Configure your MCP client (e.g., Claude Desktop) to use the server.

See `mcp_config.json` for configuration example.

## Quick Start Guide

### Basic Workflow

1. **Open a theory file** - The server will automatically open and track the file
2. **Check diagnostics** - Use `isabelle_diagnostics` to check for errors
3. **Query proof state** - Use `isabelle_goal` to see what needs to be proven
4. **Get help** - Use `isabelle_hover` to understand symbols
5. **Find definitions** - Use `isabelle_definition` to jump to definitions

### Example: Checking a Proof

```python
# 1. Check for errors
diagnostics = isabelle_diagnostics(file_path="/path/to/Theory.thy")
if not diagnostics.success:
    for error in diagnostics.items:
        if error.severity == "error":
            print(f"Error at line {error.line}: {error.message}")

# 2. Query proof state at a tactic
state = isabelle_goal(
    file_path="/path/to/Theory.thy",
    line=42  # Line with proof tactic
)
print("Command:", state.command.text if state.command else None)
print("Subgoals after the command:", state.subgoals)
```

## Testing Examples

Run the included test theory files:

```bash
# Check if simple_theory.thy is valid
python -c "
import asyncio
from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.tools import diagnostic_messages

async def test():
    client = IsabelleLSPClient(logic='HOL')
    await client.start()

    result = await diagnostic_messages(
        client,
        'examples/simple_theory.thy'
    )

    print(f'Success: {result.success}')
    print(f'Errors: {len([d for d in result.items if d.severity == \"error\"])}')

    await client.shutdown()

asyncio.run(test())
"
```

## Common Use Cases

### 1. Understanding Errors

When you encounter an error, use multiple tools together:

```python
# Get diagnostics
diags = isabelle_diagnostics(file_path=path, start_line=10, end_line=20)

# For each error, get context
for error in diags.items:
    if error.severity == "error":
        # Get hover info for a symbol on the error line
        info = isabelle_hover(
            file_path=path,
            line=error.line,
            symbol="Suc"
        )
        print(f"Error: {error.message}")
        print(f"Symbol: {info.symbol}")
        for entry in info.results:
            print(f"Info: {entry.info}")
```

### 2. Developing Proofs

Use `isabelle_goal` to inspect the proof state after a command:

```python
# Proof state after the command at this line
state = isabelle_goal(file_path=path, line=tactic_line)

if state.command:
    print(f"Command: {state.command.text!r}")
print("Subgoals after it run:")
for i, sg in enumerate(state.subgoals, 1):
    print(f"  {i}. {sg}")

# To target a specific command, pass after_text (matched as Isabelle tokens):
state = isabelle_goal(file_path=path, line=tactic_line, after_text="apply (induct n)")
```

### 3. Code Navigation

Use definition and local occurrences together:

```python
# Find where a symbol is defined
defn = isabelle_definition(file_path=path, line=10, symbol="my_const")
print(f"Symbol: {defn.symbol}")
for loc in defn.locations:
    print(f"  Defined at {loc.file_path}:{loc.line}")

# Find all in-file occurrences (definition + uses) of the symbol
occ = isabelle_local_occurrences(file_path=path, line=10, symbol="my_const")
print(f"\nAll occurrences of '{occ.symbol}':")
for o in occ.occurrences:
    print(f"  Line {o.line}, columns {o.start_column}-{o.end_column}")
```

### 4. Checking the Session

Inspect the current session before use:

```python
# Check current session
info = isabelle_session_info()
print(f"Current session: {info.current_session}")
```

## Troubleshooting

### Server won't start

- Check that Isabelle is installed: `isabelle version`
- Check that `isabelle vscode_server` works: `isabelle vscode_server -help`
- Check logs for error messages

### Tools return empty results

- Ensure document is open first (happens automatically)
- Wait for processing to complete (check `processing_complete` in diagnostics)
- Some PIDE tools have MVP limitations (see tool documentation)

### Performance issues

- Use the same LSP client instance for multiple queries (client is cached)
- Filter diagnostics by line range to reduce processing

## Additional Resources

- **SPECIFICATION.md** - Complete feature documentation
- **API_DESIGN.md** - Detailed API specifications
- **ARCHITECTURE.md** - System design and implementation
- **README.md** - Installation and setup guide
