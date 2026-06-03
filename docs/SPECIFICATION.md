# Isa-LSP Functional Specification

**Version:** 0.1.0
**Date:** 2026-03-07
**Status:** Draft with current implementation notes

> Documentation reliability note:
> This document contains both current behavior and design targets. The current
> server exposes these MCP tools: hover, definition, local occurrences,
> diagnostics, goal, command output, session info, and the evaluation tools
> (`isabelle_evaluate_to`, `isabelle_evaluation_status`, `isabelle_cancel_evaluation`).
> It does **not** currently expose `isabelle_completions`, `isabelle_preview`, or
> `isabelle_edit` as MCP tools — though the LSP-client layer already implements
> completion and preview support, it is simply not surfaced as a tool yet.
> Sections that present these three as live tools are retained as design notes
> until they are exposed.

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
- Standard LSP-based MCP tools (hover, definition, local occurrences, diagnostics; completion is a design target)
- PIDE-specific MCP tools (proof state, command output; preview is a design target)
- 1 session management tool (session info)
- Document open/close state management (`didOpen`, `didClose`)
- Async notification handling (diagnostics and selected PIDE notifications)
- Session lifecycle management

**Out of Scope:**
- Document editing (`isabelle_edit`) for the current release
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

#### Tool 1: `isabelle_hover`
**Purpose**: Get type signature and documentation for symbol
**LSP Mapping**: `textDocument/hover` ✅
**Priority**: High (Core feature)
**Pattern**: Like `lean_hover_info`

#### Tool 2: `isabelle_completions` (Design Target)
**Purpose**: Get code completion suggestions (syntax, semantic, paths, spelling)
**Current Status**: Not exposed as an MCP tool (the LSP-client layer supports it via `get_completions`)
**LSP Mapping**: `textDocument/completion` ✅
**Priority**: High (Core feature)
**Pattern**: Like `lean_completions`

#### Tool 3: `isabelle_definition`
**Purpose**: Find where a symbol is defined
**LSP Mapping**: `textDocument/definition` ✅
**Priority**: High (Navigation)
**Pattern**: Like `lean_declaration_file`

#### Tool 4: `isabelle_local_occurrences`
**Purpose**: Find in-file occurrences (definition + uses) of a locally-defined entity
**LSP Mapping**: `textDocument/documentHighlight` ✅
**Priority**: Medium (Navigation)
**Pattern**: New (no Lean equivalent)

#### Tool 5: `isabelle_diagnostics`
**Purpose**: Get prover diagnostics (errors, warnings, info)
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

#### Tool 8: `isabelle_preview` (Design Target)
**Purpose**: Generate HTML preview/documentation
**Current Status**: Not exposed as an MCP tool (the LSP-client layer supports it via `request_preview`)
**PIDE Mapping**: `PIDE/preview_request` → `PIDE/preview_response` ✅
**Priority**: Low (Documentation generation)
**Pattern**: New (Isabelle-specific)

### 3.3 Document Editing Tool (Design Target)

#### Tool 9: `isabelle_edit`
**Purpose**: Edit theory file content and trigger PIDE reprocessing (like editing in VS Code)
**Current Status**: Not implemented in the current server
**LSP Mapping**: `textDocument/didChange` (Full sync)
**Priority**: **HIGH** if implemented (enables interactive theorem proving workflow)
**Pattern**: New (no Lean equivalent — enables the edit-check-fix loop)

### 3.4 Session Management Tools (1 tool)

#### Tool 10: `isabelle_session_info`
**Purpose**: Get the current session name
**LSP Mapping**: In-memory client state ✅
**Priority**: Low (Introspection)
**Pattern**: New (info query)

---

## 4. Tool Specifications

### 4.1 Standard LSP Tools

#### 4.1.1 `isabelle_hover`

**Description**: Get type signature, documentation, and tooltips for a symbol on a line. Looks up every occurrence of the given symbol text on the line.

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
symbol: Annotated[str, Field(description="Symbol text to look up, ASCII or Unicode")]
```

**Output Model**:
```python
class HoverEntry(BaseModel):
    info: str = Field(description="Type signature and documentation")
    occurrences: List[int] = Field(description="1-indexed occurrence indices on the line")
    columns: List[int] = Field(description="1-indexed column positions of those occurrences")

class HoverInfo(BaseModel):
    symbol: str = Field(description="Queried symbol text")
    results: List[HoverEntry] = Field(
        default_factory=list,
        description="Hover results grouped by content"
    )
    line_context: str = Field(description="Full source line for reference")
    diagnostics: List[DiagnosticMessage] = Field(default_factory=list)
    note: Optional[str] = Field(default=None)
```

**Example Response**:
```json
{
  "symbol": "Suc",
  "results": [
    {
      "info": "Suc :: nat ⇒ nat\n\nThe successor function for natural numbers.",
      "occurrences": [1],
      "columns": [7]
    }
  ],
  "line_context": "lemma \"Suc n = n + 1\"",
  "diagnostics": []
}
```

---

#### 4.1.2 `isabelle_completions` (Design Target)

**Current Status**: Not exposed as an MCP tool. The LSP-client layer supports it (`get_completions`); the spec below is the intended tool surface.

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

#### 4.1.3 `isabelle_definition`

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
symbol: Annotated[str, Field(description="Symbol text to look up, ASCII or Unicode")]
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

#### 4.1.4 `isabelle_local_occurrences`

**Description**: Find every in-file occurrence (definition + uses) of a locally-defined entity, given a symbol on a line. Only entities whose definition is in the current file resolve; global constants and free/bound variables return nothing.

**Tool Annotations**:
```python
ToolAnnotations(
    title="Local Occurrences",
    readOnlyHint=True,
    idempotentHint=True,
)
```

**Input Parameters**:
```python
file_path: Annotated[str, Field(description="Absolute path to .thy file")]
line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)]
symbol: Annotated[str, Field(description="Symbol text to look up, ASCII or Unicode")]
```

**Output Model**:
```python
class Occurrence(BaseModel):
    line: int = Field(description="Line number (1-indexed)", ge=1)
    start_column: int = Field(description="Start column (1-indexed)", ge=1)
    end_column: int = Field(description="End column (1-indexed)", ge=1)

class LocalOccurrencesResult(BaseModel):
    symbol: str = Field(description="Symbol being looked up")
    occurrences: List[Occurrence] = Field(default_factory=list)
```

---

#### 4.1.5 `isabelle_diagnostics`

**Description**: Get prover diagnostics (errors, warnings, information) for a theory file. **This is essential for checking if your changes are correct.**

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

#### 4.2.3 `isabelle_preview` (Design Target)

**Current Status**: Not exposed as an MCP tool. The LSP-client layer supports it (`request_preview`); the spec below is the intended tool surface.

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

### 4.3 Document Editing Tool (Design Target)

#### 4.3.1 `isabelle_edit` ✏️ Mutating

**Description**: Design target for editing theory file content and triggering
PIDE incremental reprocessing. This tool is not registered in the current
server.

**Tool Annotations**:
```python
ToolAnnotations(
    title="Edit Document",
    readOnlyHint=False,
    idempotentHint=False,
)
```

**Input Parameters**:
```python
file_path: Annotated[str, Field(description="Absolute path to .thy file")]

# Option A: Full content replacement
new_content: Annotated[Optional[str], Field(
    description="Complete new file content. Mutually exclusive with line-range parameters."
)] = None

# Option B: Line-range replacement
start_line: Annotated[Optional[int], Field(
    description="First line to replace (1-indexed, inclusive)", ge=1
)] = None
end_line: Annotated[Optional[int], Field(
    description="Last line to replace (1-indexed, inclusive). "
    "If end_line < start_line, text is inserted before start_line without removing any lines.", ge=0
)] = None
new_text: Annotated[Optional[str], Field(
    description="Replacement text for the line range"
)] = None

# Behavior options
sync_to_disk: Annotated[bool, Field(
    description="Also write the updated content to the file on disk (default: true)"
)] = True
wait_for_processing: Annotated[bool, Field(
    description="Wait for PIDE to finish reprocessing before returning (default: true)"
)] = True
```

**Output Model**:
```python
class EditResult(BaseModel):
    success: bool = Field(description="True if no errors in the entire document after reprocessing")
    version: int = Field(description="New document version after edit")
    content_length: int = Field(description="Total length of document after edit (characters)")
    diagnostics: List[DiagnosticMessage] = Field(
        default_factory=list,
        description="All diagnostics for the document after PIDE reprocessing"
    )
    processing_complete: bool = Field(
        description="Whether PIDE finished reprocessing the document"
    )
```

**Example: Replace a proof tactic**:
```json
// Request
{
  "file_path": "/path/to/Theory.thy",
  "start_line": 42,
  "end_line": 42,
  "new_text": "  by (simp add: assms)"
}

// Response
{
  "success": true,
  "version": 3,
  "content_length": 1547,
  "diagnostics": [],
  "processing_complete": true
}
```

**Example: Insert a new lemma (without removing any lines)**:
```json
// Request: insert before line 50 (end_line=49 < start_line=50 → pure insert)
{
  "file_path": "/path/to/Theory.thy",
  "start_line": 50,
  "end_line": 49,
  "new_text": "lemma new_lemma: \"P ⟶ P\"\n  by auto\n"
}

// Response
{
  "success": true,
  "version": 4,
  "content_length": 1612,
  "diagnostics": [],
  "processing_complete": true
}
```

**Example: Full content replacement**:
```json
// Request
{
  "file_path": "/path/to/Theory.thy",
  "new_content": "theory Theory imports Main begin\n\nlemma \"True\" by simp\n\nend"
}

// Response
{
  "success": true,
  "version": 2,
  "content_length": 62,
  "diagnostics": [],
  "processing_complete": true
}
```

**Edge Cases**:
- File not yet opened → auto-open via `didOpen` before applying change
- Both `new_content` and line-range params provided → error
- Neither provided → error
- `start_line` beyond file length → append at end
- Empty `new_text` with valid range → delete lines
- PIDE processing timeout → return `processing_complete=false` with partial diagnostics

**Caveats and Known Limitations**:
- After an edit, previously cached results from `isabelle_goal`, `isabelle_hover`, etc. are stale. Always re-query after editing.
- When `sync_to_disk=False`, the LSP buffer diverges from the file on disk. A subsequent `open_document` (which reads from disk) would overwrite the in-buffer changes. Avoid `sync_to_disk=False` unless performing a sequence of edits followed by a single disk write.
- The `wait_for_processing` heuristic (no new `publishDiagnostics` for 500ms+) can be unreliable: if PIDE takes >500ms between diagnostic batches it may falsely report completion, and if the file has zero diagnostics it will wait until timeout. See API_DESIGN.md Section 3.6 for details.
- Concurrent edits are not safe: if two `isabelle_edit` calls overlap, the line-range splice may produce incorrect results because both use the same cached content as base. Serialize edit calls.

For protocol details and implementation guidance, see API_DESIGN.md Section 3.6.

---

### 4.4 Session Management Tools

#### 4.4.1 `isabelle_session_info`

**Description**: Get the name of the current Isabelle session.

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
    current_session: str = Field(description="Current logic/session name")
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

---

## 6. Instructions Card

```python
INSTRUCTIONS = """## General Rules
- All line and column numbers are 1-indexed.
- Modify theory files with normal filesystem/editor tools, then use diagnostics
  and goal queries to verify them. `isabelle_edit` is a design target, not a
  current tool.

## Key Tools
- **isabelle_goal**: Proof state at position. Omit `column` for before/after. MOST IMPORTANT!
- **isabelle_diagnostics**: Prover errors/warnings. Use after every change.
- **isabelle_hover**: Type signature + docs. Look up by symbol text on a line.
- **isabelle_completions**: Code completion suggestions.

## Position Conventions
- Line/column are 1-indexed (first line = 1, first character = 1)
- For goals, omit column to see how a tactic transforms the state
- For hover and definition, pass the symbol text to look up on the line

## Workflow
1. Open a theory file (automatic on first tool call)
2. Use isabelle_diagnostics to check for errors
3. Use isabelle_goal to see proof state
4. Modify tactics or add lemmas with normal file editing tools
5. Check isabelle_diagnostics again to verify changes
6. Use isabelle_hover to understand symbols
7. Use isabelle_completions for code assistance

## Return Formats
All tools return JSON objects (Pydantic models). Lists are wrapped in `items` field.
Empty list = `{"items": []}`.
"""
```

---

## 7. Current Verification Status

### 7.1 Functional Status

1. Current server registers 10 MCP tools.
2. Standard LSP features (hover, definition, local occurrences, diagnostics) are
   implemented and covered by tests. Completion is supported at the LSP-client
   layer (`get_completions`) but not yet exposed as an MCP tool.
3. PIDE tools use Isabelle2024 native notifications:
   - `isabelle_goal` uses `PIDE/caret_update`, `PIDE/state_init`,
     `PIDE/state_output`, and `PIDE/state_exit`.
   - `isabelle_command_output` uses `PIDE/dynamic_output`.
4. PIDE tools are best-effort: they may timeout if Isabelle emits no matching
   notification for the requested position/file.
5. `isabelle_completions`, `isabelle_preview`, and `isabelle_edit` are not
   exposed as MCP tools in the current server (completion and preview have
   LSP-client support; the design specs are retained above).

### 7.2 Interface Consistency

1. Current tool names are `isabelle_evaluate_to`, `isabelle_evaluation_status`,
   `isabelle_cancel_evaluation`, `isabelle_hover`, `isabelle_definition`,
   `isabelle_local_occurrences`, `isabelle_diagnostics`, `isabelle_goal`,
   `isabelle_command_output`, and `isabelle_session_info`.
2. All public tool positions are 1-indexed.
3. Tools return Pydantic models rather than bare lists.
4. Error handling uses `IsabelleToolError` for expected tool failures.

### 7.3 Quality Status

Current quality gates expected for this repository:

1. `python -m ruff check .`
2. `python -m mypy src`
3. `pyright src`
4. `python -m pytest -q`

Documentation must not mark future/design-target behavior as implemented.

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
| Diagnostics | `lean_diagnostic_messages` | `isabelle_diagnostics` | ✅ Same pattern |
| Edit | ❌ (external tool) | Not implemented | Design target only |
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
