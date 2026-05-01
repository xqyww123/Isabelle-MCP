INSTRUCTIONS = """\
# Isabelle LSP MCP Server

## Tools

### Standard LSP (5 tools)

1. **isabelle_hover** — type info and documentation for symbol at position
2. **isabelle_completions** — completion suggestions, sorted by relevance
3. **isabelle_definition** — jump to symbol definition
4. **isabelle_highlights** — all occurrences of symbol in document
5. **isabelle_diagnostics** — errors, warnings, processing status

### PIDE Extensions (3 tools)

6. **isabelle_goal** ⭐ — proof goals at position; omit column for before/after diff
7. **isabelle_command_output** — prover messages for a command
8. **isabelle_preview** — HTML preview of theory

### Session Management (2 tools)

9.  **isabelle_session_info** — current session
10. **isabelle_build** — build session heap images

## Key conventions

- All positions are **1-indexed** (line 1, column 1 = first character).
- Always use **absolute paths**.
- Check `processing_complete` in diagnostics before relying on results.
- PIDE tools (goal, command_output, preview) are best-effort; fall back to diagnostics on timeout.

## Recommended workflow

1. **isabelle_diagnostics** — always check code validity first.
2. **isabelle_goal** — use extensively during proof development (omit column to see tactic effect).
3. **isabelle_hover** + **isabelle_definition** — understand symbols.
4. No `isabelle_edit` tool exists; modify files with your editor, then re-check with diagnostics/goals.

## Session configuration

Default session is **HOL**. Override via `ISABELLE_SESSION` env var before starting the MCP server.
"""


def get_instructions() -> str:
    return INSTRUCTIONS
