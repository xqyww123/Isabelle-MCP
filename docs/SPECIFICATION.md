# Isa-LSP Functional Specification

**Version:** 0.1.0
**Date:** 2026-03-07
**Status:** Draft - MVP (LSP/PIDE Native Features Only)

## 1. Executive Summary

### 1.1 Project Overview

Isa-LSP is a Model Context Protocol (MCP) server that provides AI agents (like Claude) with programmatic access to Isabelle's Language Server Protocol (LSP) capabilities. It bridges the gap between AI-assisted theorem proving and Isabelle's interactive proof environment (PIDE) by exposing both standard LSP features and Isabelle-specific extensions through a clean, tool-based interface.

**Design Philosophy:** Follow the proven patterns from `lean-lsp-mcp`, maintaining consistent naming conventions, structured outputs, and user experience patterns.

### 1.2 Objectives

1. **Expose Isabelle LSP/PIDE Features**: Make Isabelle's `vscode_server` accessible to AI agents through MCP tools
2. **Support Interactive Theorem Proving**: Enable AI agents to query proof states, get completions, navigate definitions, and access documentation
3. **Provide Only Native Features**: Support ONLY features natively implemented by `isabelle vscode_server`
4. **Maintain Session Efficiency**: Reuse long-lived LSP server sessions to avoid expensive initialization overhead
5. **Follow lean-lsp-mcp Patterns**: Consistent interface design, 1-indexed positions, Pydantic models, structured errors

### 1.3 Scope

**In Scope (MVP - LSP/PIDE Native Support Only):**
- 5 standard LSP-based MCP tools (hover, completion, definition, highlights, diagnostics)
- 3 PIDE-specific MCP tools (proof state, command output, preview)
- 2 session management tools (build, session info)
- Document synchronization and state management
- Async notification handling (diagnostics, decorations)
- Session lifecycle management with optional build support

**Out of Scope:**
- Direct VSCode extension integration
- Non-LSP Isabelle interfaces (jEdit, raw PIDE)
- **Advanced LSP features not implemented by `isabelle vscode_server`:**
  - File outline (`textDocument/documentSymbol` not implemented)
  - Code actions (`textDocument/codeAction` not implemented)
  - References (`textDocument/references` not implemented)
- **Command execution features (requires complex implementation):**
  - Sledgehammer integration (needs command injection + output parsing)
  - Find theorems (needs command injection + output parsing)
  - Try methods (needs command injection + output parsing)
  - Term goals (no dedicated PIDE method)
- Real-time streaming of PIDE decorations (initial version caches only)
- Multi-user or concurrent session management

**Future Enhancements (Phase 2):**
- File outline tool (requires custom file parsing or `textDocument/documentSymbol` implementation)
- Sledgehammer, find_theorems, try methods (requires command execution framework)
- Term goal extraction (may be derivable from hover info)
- Code actions if Isabelle LSP adds support

---

## 2. Design Principles (Following lean-lsp-mcp)

### 2.1 Core Principles

1. **Structured Outputs**: All tools return Pydantic models, never bare primitives or lists
2. **Consistent Naming**: `isabelle_{category}_{action}` pattern
3. **1-Indexed Positions**: All line/column numbers are 1-indexed (explicit in docs)
4. **Optional Column Pattern**: Omitting column gives before/after view (for proof states)
5. **Custom Exceptions**: `IsabelleToolError` for tool failures
6. **Tool Annotations**: Mark readonly, idempotent, and destructive operations
7. **Concise Documentation**: Instruction card + docstrings
8. **Native Features Only**: Only wrap features actually implemented by `isabelle vscode_server`

### 2.2 Output Model Pattern

**Always wrap lists in models:**
```python
# BAD: Bare list
def tool() -> List[str]: ...

# GOOD: Wrapped in result model
class DiagnosticsResult(BaseModel):
    success: bool = Field(True, description="True if no errors")
    items: List[DiagnosticMessage] = Field(default_factory=list)

def tool() -> DiagnosticsResult: ...
```

### 2.3 Parameter Conventions

- `file_path: str` - Absolute path to theory file
- `line: int` - 1-indexed line number (Field ge=1)
- `column: Optional[int]` - 1-indexed column (omit for ranges/before-after)
- `max_*: int` - Limits (max_completions, max_results)
- `interactive: bool` - Verbose output flag
- `theorem_name: str` - Fully qualified identifier

---

## 3. Feature Catalog

### 3.1 Standard LSP Tools (5 tools)

Based on `isabelle vscode_server` analysis - **only LSP-native features**:

#### Tool 1: `isabelle_hover_info`
**Purpose**: Get type signature and documentation for symbol
**LSP Mapping**: `textDocument/hover` ✅
**Priority**: High (Core feature)
**Pattern**: Like `lean_hover_info`

#### Tool 2: `isabelle_completions`
**Purpose**: Get code completion suggestions (syntax, semantic, paths, spelling)
**LSP Mapping**: `textDocument/completion` ✅
**Priority**: High (Core feature)
**Pattern**: Like `lean_completions`

#### Tool 3: `isabelle_declaration_location`
**Purpose**: Find where a symbol is defined
**LSP Mapping**: `textDocument/definition` ✅
**Priority**: High (Navigation)
**Pattern**: Like `lean_declaration_file`

#### Tool 4: `isabelle_document_highlights`
**Purpose**: Find all occurrences of symbol in document
**LSP Mapping**: `textDocument/documentHighlight` ✅
**Priority**: Medium (Navigation)
**Pattern**: New (no Lean equivalent)

#### Tool 5: `isabelle_diagnostic_messages`
**Purpose**: Get compiler diagnostics (errors, warnings, info)
**LSP Mapping**: Cached `textDocument/publishDiagnostics` notifications ✅
**Priority**: High (Essential)
**Pattern**: Like `lean_diagnostic_messages`

### 3.2 PIDE Extension Tools (3 tools)

Isabelle-specific features - **only PIDE-native methods**:

#### Tool 6: `isabelle_goal`
**Purpose**: Get proof state (goals, assumptions) at position
**PIDE Mapping**: `PIDE/state_*` sequence ✅
**Priority**: **CRITICAL** (Most important tool for theorem proving)
**Pattern**: Like `lean_goal` with optional column for before/after

#### Tool 7: `isabelle_command_output`
**Purpose**: Get prover messages for command (writeln, warnings, errors)
**PIDE Mapping**: `PIDE/dynamic_output` notifications ✅
**Priority**: Medium (Debugging)
**Pattern**: New (Isabelle-specific)

#### Tool 8: `isabelle_preview`
**Purpose**: Generate HTML preview/documentation
**PIDE Mapping**: `PIDE/preview_request` → `PIDE/preview_response` ✅
**Priority**: Low (Documentation generation)
**Pattern**: New (Isabelle-specific)

### 3.3 Session Management Tools (2 tools)

#### Tool 9: `isabelle_build`
**Purpose**: Rebuild session and restart LSP server
**LSP Mapping**: Spawns new `isabelle vscode_server` process ✅
**Priority**: Critical (Session initialization)
**Pattern**: Like `lean_build`
**Destructive**: Yes (restarts session)

#### Tool 10: `isabelle_session_info`
**Purpose**: Get current session info and capabilities
**LSP Mapping**: Cached `initialize` response ✅
**Priority**: Low (Introspection)
**Pattern**: New (info query)

---

## 4. Tool Specifications

### 4.1 Standard LSP Tools

#### 4.1.1 `isabelle_hover_info`

**Description**: Get type signature, documentation, and tooltips for the symbol at a position. Use column at the START of the identifier.

**Tool Annotations**:
```python
ToolAnnotations(
    title="Hover Info",
    readOnlyHint=True,
    idempotentHint=True,
)
```

**Input Parameters**:
```python
file_path: Annotated[str, Field(description="Absolute path to .thy file")]
line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)]
column: Annotated[int, Field(description="Column number (1-indexed)", ge=1)]
```

**Output Model**:
```python
class HoverInfo(BaseModel):
    symbol: str = Field(description="Symbol text at position")
    info: str = Field(description="Type signature and documentation")
    line_context: str = Field(description="Full source line for reference")
    diagnostics: List[DiagnosticMessage] = Field(
        default_factory=list,
        description="Diagnostics at this position"
    )
```

**Example Response**:
```json
{
  "symbol": "Suc",
  "info": "Suc :: nat ⇒ nat\n\nThe successor function for natural numbers.",
  "line_context": "lemma \"Suc n = n + 1\"",
  "diagnostics": []
}
```

---

#### 4.1.2 `isabelle_completions`

**Description**: Get code completion suggestions at a position.

**Tool Annotations**:
```python
ToolAnnotations(
    title="Completions",
    readOnlyHint=True,
    idempotentHint=True,
)
```

**Input Parameters**:
```python
file_path: Annotated[str, Field(description="Absolute path to .thy file")]
line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)]
column: Annotated[int, Field(description="Column number (1-indexed)", ge=1)]
max_completions: Annotated[int, Field(
    description="Maximum number of completions to return", ge=1
)] = 32
```

**Output Model**:
```python
class CompletionItem(BaseModel):
    label: str = Field(description="Completion text")
    kind: str = Field(description="function | variable | keyword | constant | class | module")
    detail: Optional[str] = Field(None, description="Additional info (e.g., type)")
    documentation: Optional[str] = Field(None, description="Description")
    insert_text: str = Field(description="Text to insert")

class CompletionsResult(BaseModel):
    items: List[CompletionItem] = Field(default_factory=list)
    line_context: str = Field(description="Source line for reference")
```

---

#### 4.1.3 `isabelle_declaration_location`

**Description**: Find where a symbol is defined.

**Tool Annotations**:
```python
ToolAnnotations(
    title="Go to Definition",
    readOnlyHint=True,
    idempotentHint=True,
)
```

**Input Parameters**:
```python
file_path: Annotated[str, Field(description="Absolute path to .thy file")]
line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)]
column: Annotated[int, Field(description="Column number (1-indexed)", ge=1)]
```

**Output Model**:
```python
class Location(BaseModel):
    file_path: str = Field(description="Absolute path to file")
    line: int = Field(description="Line number (1-indexed)", ge=1)
    column: int = Field(description="Column number (1-indexed)", ge=1)

class DeclarationLocation(BaseModel):
    symbol: str = Field(description="Symbol being queried")
    locations: List[Location] = Field(
        default_factory=list,
        description="Definition locations (may be multiple for overloaded symbols)"
    )
```

---

#### 4.1.4 `isabelle_document_highlights`

**Description**: Find all occurrences of the symbol at the given position within the current document.

**Tool Annotations**:
```python
ToolAnnotations(
    title="Document Highlights",
    readOnlyHint=True,
    idempotentHint=True,
)
```

**Input Parameters**:
```python
file_path: Annotated[str, Field(description="Absolute path to .thy file")]
line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)]
column: Annotated[int, Field(description="Column number (1-indexed)", ge=1)]
```

**Output Model**:
```python
class Highlight(BaseModel):
    line: int = Field(description="Line number (1-indexed)", ge=1)
    start_column: int = Field(description="Start column (1-indexed)", ge=1)
    end_column: int = Field(description="End column (1-indexed)", ge=1)
    kind: str = Field(description="text | read | write")

class HighlightsResult(BaseModel):
    symbol: str = Field(description="Symbol being highlighted")
    highlights: List[Highlight] = Field(default_factory=list)
```

---

#### 4.1.5 `isabelle_diagnostic_messages`

**Description**: Get compiler diagnostics (errors, warnings, information) for a theory file. **This is essential for checking if your changes are correct.**

**Tool Annotations**:
```python
ToolAnnotations(
    title="Diagnostics",
    readOnlyHint=True,
    idempotentHint=True,
)
```

**Input Parameters**:
```python
file_path: Annotated[str, Field(description="Absolute path to .thy file")]
start_line: Annotated[Optional[int], Field(
    description="Filter diagnostics from this line (1-indexed)", ge=1
)] = None
end_line: Annotated[Optional[int], Field(
    description="Filter diagnostics to this line (1-indexed)", ge=1
)] = None
interactive: Annotated[bool, Field(
    description="Returns verbose nested markup with embedded PIDE information. "
                "Only use when plain text is insufficient."
)] = False
```

**Output Model**:
```python
class DiagnosticMessage(BaseModel):
    severity: str = Field(description="error | warning | information | hint")
    message: str = Field(description="Diagnostic message text")
    line: int = Field(description="Line number (1-indexed)", ge=1)
    column: int = Field(description="Column number (1-indexed)", ge=1)
    end_line: int = Field(description="End line (1-indexed)", ge=1)
    end_column: int = Field(description="End column (1-indexed)", ge=1)

class DiagnosticsResult(BaseModel):
    success: bool = Field(True, description="True if the queried file/range has no errors")
    items: List[DiagnosticMessage] = Field(default_factory=list)
    processing_complete: bool = Field(description="Whether PIDE finished processing")
    failed_dependencies: List[str] = Field(
        default_factory=list,
        description="File paths of theories that failed to load"
    )
```

**Example Response**:
```json
{
  "success": false,
  "items": [
    {
      "severity": "error",
      "message": "Undefined constant \"foo\"",
      "line": 42,
      "column": 10,
      "end_line": 42,
      "end_column": 13
    }
  ],
  "processing_complete": true,
  "failed_dependencies": []
}
```

---

### 4.2 PIDE Extension Tools

#### 4.2.1 `isabelle_goal` ⭐ MOST IMPORTANT TOOL

**Description**: Get proof goals at a position. **MOST IMPORTANT tool for theorem proving - use often!**

Omit `column` to see `goals_before` (at line start) and `goals_after` (at line end), showing how the tactic transforms the proof state. "no goals" means the proof is complete.

**Tool Annotations**:
```python
ToolAnnotations(
    title="Proof Goals",
    readOnlyHint=True,
    idempotentHint=True,
)
```

**Input Parameters**:
```python
file_path: Annotated[str, Field(description="Absolute path to .thy file")]
line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)]
column: Annotated[Optional[int], Field(
    description="Column number (1-indexed). Omit to see before/after tactic transformation.", ge=1
)] = None
```

**Output Model**:
```python
class GoalState(BaseModel):
    line_context: str = Field(description="Source line where goals were queried")

    # If column is provided:
    goals: Optional[List[str]] = Field(None, description="Goals at specific column")

    # If column is omitted:
    goals_before: Optional[List[str]] = Field(None, description="Goals at line start (before tactic)")
    goals_after: Optional[List[str]] = Field(None, description="Goals at line end (after tactic)")

    # Additional context:
    context: Optional[str] = Field(None, description="Local proof context (assumptions, fixes)")
```

**Example Response (column omitted)**:
```json
{
  "line_context": "  by (auto simp: lemma1)",
  "goals_before": [
    "⋀x. P x ⟹ Q x",
    "R y"
  ],
  "goals_after": [],
  "context": "fix x y\nassume \"A x\" \"B y\""
}
```

**Example Response (column provided)**:
```json
{
  "line_context": "  apply (induction n)",
  "goals": [
    "case 0\nthen show ?thesis",
    "case (Suc n)\nthen show ?thesis"
  ],
  "goals_before": null,
  "goals_after": null,
  "context": "fix n :: nat"
}
```

---

#### 4.2.2 `isabelle_command_output`

**Description**: Get prover output messages (writeln, warnings, errors) for the command at a position.

**Tool Annotations**:
```python
ToolAnnotations(
    title="Command Output",
    readOnlyHint=True,
    idempotentHint=True,
)
```

**Input Parameters**:
```python
file_path: Annotated[str, Field(description="Absolute path to .thy file")]
line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)]
```

**Output Model**:
```python
class OutputMessage(BaseModel):
    kind: str = Field(description="writeln | warning | error | information")
    text: str = Field(description="Message content")

class CommandOutputResult(BaseModel):
    line_context: str = Field(description="Source line")
    messages: List[OutputMessage] = Field(default_factory=list)
```

---

#### 4.2.3 `isabelle_preview`

**Description**: Generate HTML preview/documentation rendering of a theory file.

**Tool Annotations**:
```python
ToolAnnotations(
    title="Preview",
    readOnlyHint=True,
    idempotentHint=True,
)
```

**Input Parameters**:
```python
file_path: Annotated[str, Field(description="Absolute path to .thy file")]
```

**Output Model**:
```python
class PreviewResult(BaseModel):
    html_content: str = Field(description="HTML preview of theory")
    title: str = Field(description="Document title")
```

---

### 4.3 Session Management Tools

#### 4.3.1 `isabelle_build` 🔨 Destructive

**Description**: Rebuild the session and restart the LSP server. **WARNING: This is destructive and will restart the entire session.**

**Tool Annotations**:
```python
ToolAnnotations(
    title="Build Session",
    readOnlyHint=False,
    idempotentHint=False,
)
```

**Input Parameters**:
```python
logic: Annotated[str, Field(description="Session name (e.g., 'HOL', 'HOL-Analysis')")] = "HOL"
session_dirs: Annotated[List[str], Field(
    description="Additional session directories"
)] = []
clean: Annotated[bool, Field(description="Clean build (rebuild everything)")] = False
verbose: Annotated[bool, Field(description="Verbose build output")] = False
```

**Output Model**:
```python
class BuildResult(BaseModel):
    success: bool = Field(description="True if build succeeded")
    build_log: str = Field(description="Build output")
    session_name: str = Field(description="Session that was built")
    server_info: Optional[Dict[str, Any]] = Field(None, description="LSP server info after restart")
```

---

#### 4.3.2 `isabelle_session_info`

**Description**: Get information about the current Isabelle session.

**Tool Annotations**:
```python
ToolAnnotations(
    title="Session Info",
    readOnlyHint=True,
    idempotentHint=True,
)
```

**Input Parameters**: None

**Output Model**:
```python
class SessionInfo(BaseModel):
    logic_name: str = Field(description="Current logic/session name")
    isabelle_version: str = Field(description="Isabelle version")
    capabilities: Dict[str, Any] = Field(description="LSP server capabilities")
    uptime_seconds: int = Field(description="Session uptime in seconds")
```

---

## 5. Error Handling

### 5.1 Custom Exception

```python
class IsabelleToolError(Exception):
    """Exception raised when an Isabelle MCP tool operation fails."""
    pass
```

### 5.2 LSP/PIDE Response Validation

```python
def check_pide_response(response: Any, operation: str, *, allow_none: bool = False):
    """Check a PIDE/LSP response for error patterns and raise if found."""
    if response is None and not allow_none:
        raise IsabelleToolError(f"PIDE timeout during {operation}")
    if isinstance(response, dict) and "error" in response:
        msg = response["error"].get("message", "unknown error")
        raise IsabelleToolError(f"PIDE error during {operation}: {msg}")
    return response
```

### 5.3 Common Error Messages

- `"Invalid theory file path: '{path}' not found in any Isabelle session"`
- `"PIDE timeout during get_hover"`
- `"Document not open. Please call isabelle_open_document first."`
- `"Session not initialized. Please call isabelle_build first."`

---

## 6. Instructions Card

```python
INSTRUCTIONS = """## General Rules
- All line and column numbers are 1-indexed.
- This MCP does NOT edit files. Use other tools for editing.

## Key Tools
- **isabelle_goal**: Proof state at position. Omit `column` for before/after. MOST IMPORTANT!
- **isabelle_diagnostic_messages**: Compiler errors/warnings. Use after every change.
- **isabelle_hover_info**: Type signature + docs. Column at START of identifier.
- **isabelle_completions**: Code completion suggestions.

## Position Conventions
- Line/column are 1-indexed (first line = 1, first character = 1)
- For goals, omit column to see how a tactic transforms the state
- For hover, use column at the START of the identifier

## Workflow
1. Open a theory file in your editor
2. Use isabelle_diagnostic_messages to check for errors
3. Use isabelle_goal to see proof state
4. Use isabelle_hover_info to understand symbols
5. Use isabelle_completions for code assistance

## Return Formats
All tools return JSON objects (Pydantic models). Lists are wrapped in `items` field.
Empty list = `{"items": []}`.
"""
```

---

## 7. Success Criteria

### 7.1 Functional Acceptance

1. ✅ All 10 MCP tools are implemented and functional
2. ✅ Session initialization works with common logic images (HOL, Pure, Main)
3. ✅ Standard LSP features (hover, completion, definition) return correct results
4. ✅ PIDE goal extraction works on simple proof scripts
5. ✅ Diagnostics correctly reflect errors from Isabelle
6. ✅ All tools only use LSP/PIDE native methods

### 7.2 Interface Consistency

1. ✅ All tools follow `isabelle_{category}_{action}` naming
2. ✅ All positions are 1-indexed (explicit in Field descriptions)
3. ✅ All tools return Pydantic models (no bare lists)
4. ✅ All list-returning tools use `items` field wrapper
5. ✅ Error handling uses `IsabelleToolError` consistently

### 7.3 Quality Acceptance

1. ✅ Unit test coverage ≥ 70%
2. ✅ Integration tests cover end-to-end workflows
3. ✅ All error codes are tested
4. ✅ Documentation follows lean-lsp-mcp style
5. ✅ Instructions card is concise and helpful

---

## Appendix A: Comparison with lean-lsp-mcp

| Feature | lean-lsp-mcp | Isa-LSP | Notes |
|---------|--------------|---------|-------|
| Tool naming | `lean_*` | `isabelle_*` | System prefix |
| Position indexing | 1-indexed | 1-indexed | ✅ Consistent |
| Output models | Pydantic | Pydantic | ✅ Consistent |
| List wrapper | `items` field | `items` field | ✅ Consistent |
| Goal query | `lean_goal` | `isabelle_goal` | ✅ Same pattern |
| Optional column | Yes (before/after) | Yes (before/after) | ✅ Same pattern |
| Diagnostics | `lean_diagnostic_messages` | `isabelle_diagnostic_messages` | ✅ Same pattern |
| File outline | `lean_file_outline` | ❌ Not in MVP | LSP doesn't support |
| Code actions | `lean_code_actions` | ❌ Not in MVP | LSP doesn't support |
| Hammer | `lean_hammer_premise` | ❌ Not in MVP | Requires command execution |
| Search | `lean_loogle`, `lean_leansearch` | ❌ Not in MVP | Requires command execution |
| Multi-attempt | `lean_multi_attempt` | ❌ Not in MVP | Requires command execution |

---

## Appendix B: Future Enhancements (Phase 2)

These features are intentionally excluded from MVP because they are not natively supported by `isabelle vscode_server` and would require significant additional implementation:

1. **File Outline (`isabelle_file_outline`)**
   - Requires: Custom file parsing or waiting for `textDocument/documentSymbol` support
   - Complexity: Medium (parsing Isabelle syntax)
   - Value: High (navigation)

2. **Sledgehammer Integration (`isabelle_sledgehammer`)**
   - Requires: Command injection, output parsing, timeout handling
   - Complexity: High (asynchronous command execution)
   - Value: Very High (automation)

3. **Find Theorems (`isabelle_find_theorems`)**
   - Requires: Command injection, result parsing
   - Complexity: Medium
   - Value: Medium (discovery)

4. **Try Methods (`isabelle_try_methods`)**
   - Requires: Command injection, transient document modifications, result parsing
   - Complexity: High (similar to lean_multi_attempt)
   - Value: High (exploration)

5. **Term Goals (`isabelle_term_goal`)**
   - Requires: Analysis of hover info or custom PIDE queries
   - Complexity: Low-Medium
   - Value: Medium (type-driven development)

6. **Code Actions (`isabelle_code_actions`)**
   - Requires: Waiting for `textDocument/codeAction` support in Isabelle LSP
   - Complexity: Low (if LSP adds support)
   - Value: Medium (quality of life)

---

**Document Status**: Ready for Architecture Design
**Next Step**: Create ARCHITECTURE.md with system components and data flow
