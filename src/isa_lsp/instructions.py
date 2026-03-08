"""
User-facing instructions for the Isabelle LSP MCP server.

This module provides helpful guidance to AI agents using this MCP server.
"""

INSTRUCTIONS = """
# Isabelle LSP MCP Server

You now have access to Isabelle theorem prover tools via the Language Server Protocol.

## Available Tools

### Standard LSP Tools (5 tools)

1. **isabelle_hover**: Get type and documentation for symbols
   - Use when you need to understand what a symbol means
   - Shows type information, definitions, and documentation

2. **isabelle_completions**: Get completion suggestions
   - Use when writing new code to see available symbols
   - Returns sorted suggestions based on relevance

3. **isabelle_definition**: Find where symbols are defined
   - Use to jump to definition of theorems, functions, types
   - Returns file path and position

4. **isabelle_highlights**: Find all occurrences of a symbol
   - Use to see where a symbol is used throughout the document
   - Helps understand scope and usage patterns

5. **isabelle_diagnostics**: Get compiler errors and warnings
   - **Use frequently** to check if code is valid
   - Returns errors, warnings, and processing status

### PIDE Extension Tools (3 tools)

6. **isabelle_goal**: Get proof goals at position ⭐ **MOST IMPORTANT**
   - **Use this tool extensively** when working with proofs
   - Omit column to see before/after tactic transformation
   - Shows what remains to be proven

7. **isabelle_command_output**: Get prover output messages
   - Use to see detailed messages from Isabelle commands
   - Useful for debugging failed proofs

8. **isabelle_preview**: Generate HTML preview of theory
   - Use to export formatted documentation
   - Useful for viewing rendered output

### Session Management Tools (2 tools)

9. **isabelle_session_info**: Get current session information
   - Use to check which logic image is loaded
   - Shows available sessions

10. **isabelle_build**: Build Isabelle session heap images
    - Use when you need to build or rebuild a session
    - Required before using new session logic

## Workflow Recommendations

### 1. Checking Code Validity

Always use **isabelle_diagnostics** to verify code:

```
result = isabelle_diagnostics(file_path="/path/to/Theory.thy")
if result.success:
    print("No errors!")
else:
    for diag in result.items:
        if diag.severity == "error":
            print(f"Error at line {diag.line}: {diag.message}")
```

### 2. Working with Proofs

Use **isabelle_goal** frequently to understand proof state:

```
# See how a tactic transforms the goal
state = isabelle_goal(
    file_path="/path/to/Proof.thy",
    line=42  # Tactic line - omit column
)

print("Before:", state.goals_before)
print("After:", state.goals_after)
```

### 3. Understanding Symbols

Combine **isabelle_hover** and **isabelle_definition**:

```
# Get quick info
info = isabelle_hover(file_path=path, line=10, column=5)
print(info.info)

# Jump to definition
loc = isabelle_definition(file_path=path, line=10, column=5)
for definition in loc.locations:
    print(f"Defined at {definition.file_path}:{definition.line}")
```

### 4. Code Completion

Use **isabelle_completions** when writing new code:

```
completions = isabelle_completions(
    file_path=path,
    line=15,
    column=8,
    max_completions=20
)

for item in completions.items:
    print(f"{item.label}: {item.detail}")
```

## Important Notes

### Position Indexing
- All line and column numbers are **1-indexed**
- Line 1, column 1 = first character of the file

### File Paths
- Always use **absolute paths** for file_path parameters
- Relative paths will fail

### Document Processing
- Isabelle processes documents incrementally
- Check `processing_complete` in diagnostics result
- Wait for processing to complete before querying goals

### MVP Limitations

The following features have limited functionality in this MVP:

1. **isabelle_goal**: Returns empty goals (PIDE state panel not fully implemented)
   - Full implementation requires state panel handler in LSP client
   - See goal.py for implementation notes

2. **isabelle_command_output**: Returns empty messages (dynamic output cache not implemented)
   - Full implementation requires caching PIDE/dynamic_output notifications
   - See command_output.py for implementation notes

3. **isabelle_preview**: Returns empty HTML (preview response handler not implemented)
   - Full implementation requires preview notification handler
   - See preview.py for implementation notes

These limitations will be addressed in future versions beyond MVP.

### Session Configuration

The default session is **HOL**. To use a different session:

```bash
# Set environment variable before starting MCP server
export ISABELLE_SESSION=Main
```

Or use **isabelle_build** to build and switch sessions.

## Error Handling

All tools raise **IsabelleToolError** on failure. Handle errors gracefully:

```python
from isa_lsp.utils import IsabelleToolError

try:
    result = isabelle_hover(file_path=path, line=1, column=1)
except IsabelleToolError as e:
    print(f"Error: {e}")
```

## Best Practices

1. **Always check diagnostics** before querying goals or other tools
2. **Use isabelle_goal frequently** when working with proofs
3. **Provide absolute file paths** for all operations
4. **Wait for processing** to complete before complex queries
5. **Handle errors** gracefully with try/except blocks

## Getting Help

For more information, see:
- README.md: Installation and setup
- SPECIFICATION.md: Complete feature documentation
- API_DESIGN.md: Detailed API specifications
- ARCHITECTURE.md: System design and implementation notes
"""


def get_instructions() -> str:
    """Get user-facing instructions for the MCP server.

    Returns:
        Markdown-formatted instructions
    """
    return INSTRUCTIONS
