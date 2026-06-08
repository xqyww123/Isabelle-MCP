# Isa-LSP Functional Specification

**Version:** 0.1.0
**Date:** 2026-06-04
**Status:** Draft with current implementation notes

> Documentation reliability note:
> This document contains both current behavior and design targets. The current
> server exposes these MCP tools: session launch/terminate, hover, definition,
> local occurrences, goal, command output, session info, and the evaluation tools
> (`isabelle_evaluate_to`, `isabelle_evaluation_status`, `isabelle_cancel_evaluation`).

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
- Standard LSP-based MCP tools (hover, definition, local occurrences, diagnostics)
- PIDE-specific MCP tools (proof state, command output)
- 3 session management tools (launch, terminate, session info)
- Document open/close state management (`didOpen`, `didClose`)
- Async notification handling (diagnostics and selected PIDE notifications)
- Session lifecycle management

**Out of Scope:**
- Document editing for the current release
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
4. **Symbol- and Snippet-Based Targeting**: Agents are unreliable at counting columns,
   so position-sensitive tools take a *text snippet* instead. `isabelle_hover`,
   `isabelle_definition`, and `isabelle_local_occurrences` take a `symbol` (the token
   text to find on the line); `isabelle_goal` and `isabelle_command_output` take an
   optional `after_text` (the command right after that snippet is used)
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
class LocalOccurrencesResult(BaseModel):
    symbol: str = Field(description="Symbol being looked up")
    occurrences: List[Occurrence] = Field(default_factory=list)

def tool() -> LocalOccurrencesResult: ...
```

### 2.3 Parameter Conventions

- `file_path: str` - Absolute path to theory file
- `line: int` - 1-indexed line number; evaluation tools also accept
  negative indices counting from the end (`-1` = last line)
- `symbol: str` - Token text to locate on the line (hover, definition,
  local occurrences); matched on token boundaries, ASCII and Unicode forms equivalent
- `after_text: str | None` - Optional snippet on the line; the command right after
  it is used (goal, command output). Without it, the command at the end of the line
  is used. `isabelle_evaluate_to` uses it as a stop point and the snippet may span
  onto following lines

---

## 3. Feature Catalog

The current server registers **11 MCP tools**: two prover-lifecycle tools
(`isabelle_launch` / `isabelle_terminate`), three evaluation tools, and six
query tools.

### 3.0 Evaluation Tools (3 tools)

Evaluation drives Isabelle's processing; the query tools below read its results.

#### Tool: `isabelle_evaluate_to`
**Purpose**: Start checking a theory file up to a target line (requires a launched session)
**Returns**: Plain-text per-file snapshot (errors / warnings / running lines) — may
report `in_progress`; poll with `isabelle_evaluation_status`
**Priority**: High (entry point for all checking)

#### Tool: `isabelle_evaluation_status`
**Purpose**: Check progress of an ongoing evaluation (per-file errors/warnings/running
line spans, completion)
**Returns**: Plain-text per-file snapshot

#### Tool: `isabelle_cancel_evaluation`
**Purpose**: Cancel an ongoing evaluation; already-checked results stay valid
**Returns**: Plain-text status message

### 3.1 Standard LSP Tools (4 tools)

Based on `isabelle vscode_server` analysis - **only LSP-native features**:

#### Tool 1: `isabelle_hover`
**Purpose**: Get type signature and documentation for symbol
**LSP Mapping**: `textDocument/hover` ✅
**Priority**: High (Core feature)
**Pattern**: Like `lean_hover_info`

#### Tool 2: `isabelle_definition`
**Purpose**: Find where a symbol is defined
**LSP Mapping**: `textDocument/definition` ✅
**Priority**: High (Navigation)
**Pattern**: Like `lean_declaration_file`

#### Tool 3: `isabelle_local_occurrences`
**Purpose**: Find in-file occurrences (definition + uses) of a locally-defined entity
**LSP Mapping**: `textDocument/documentHighlight` ✅
**Priority**: Medium (Navigation)
**Pattern**: New (no Lean equivalent)

### 3.2 PIDE Extension Tools (3 tools)

Isabelle-specific features - **only PIDE-native methods**:

#### Tool 4: `isabelle_goal`
**Purpose**: Get the Isar command at a position and the proof state (subgoals) after it runs
**PIDE Mapping**: `PIDE/state_*` sequence ✅
**Priority**: **CRITICAL** (Most important tool for theorem proving)
**Pattern**: Like `lean_goal`; targeting is by optional `after_text`, not a column

#### Tool 5: `isabelle_command_output`
**Purpose**: Get the Isar command at a position and the prover messages it produced
(including the full error/warning message text — this is where error detail is read)
**PIDE Mapping**: `PIDE/dynamic_output` notifications ✅
**Priority**: Medium (Debugging)
**Pattern**: New (Isabelle-specific); targeting is by optional `after_text`

### 3.3 Session Management Tools (3 tools)

#### Tool 6: `isabelle_launch`
**Purpose**: Start (or restart) the prover for a chosen session/logic; must be
called before any evaluation/query tool (the prover does not auto-start)
**LSP Mapping**: Spawns `isabelle vscode_server -l <session> -d <dirs…>` ✅
**Priority**: High (entry point)
**Pattern**: New (lifecycle)

#### Tool 7: `isabelle_terminate`
**Purpose**: Terminate the running prover (the MCP server stays up; relaunch allowed)
**LSP Mapping**: LSP `shutdown`/`exit` then process teardown ✅
**Priority**: Low (lifecycle)
**Pattern**: New (lifecycle)

#### Tool 8: `isabelle_session_info`
**Purpose**: Get the current session name and server version
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

#### 4.1.2 `isabelle_definition`

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
    note: Optional[str] = Field(default=None, description="Warning note (e.g. line still running)")
```

---

#### 4.1.3 `isabelle_local_occurrences`

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
    note: Optional[str] = Field(default=None, description="Warning note (e.g. line still running)")
```

---

### 4.2 PIDE Extension Tools

#### 4.2.1 `isabelle_goal` ⭐ MOST IMPORTANT TOOL

**Description**: Get the Isar command enclosing a position and the proof state
(remaining subgoals) **after** that command runs. **MOST IMPORTANT tool for theorem
proving — use often!** An empty `subgoals` list means the proof is finished at that
command. Auto-evaluates the line first if needed (requires a launched session).

Without `after_text`, the command at the end of the line is used. Pass `after_text`
to target the command right after that snippet on the line. To see a tactic's
before/after effect, query it twice: once at the line *before* the tactic and once
at the tactic's own line.

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
after_text: Annotated[Optional[str], Field(
    description="Optional text on the line; the command right after it is used. "
                "Without it, the command at the end of the line is used."
)] = None
```

**Output Model**:
```python
class CommandSpan(BaseModel):
    text: str = Field(description="Full source text of the Isar command (may span multiple lines)")
    start_line: int = Field(ge=1, description="Command start line (1-indexed)")
    start_column: int = Field(ge=1, description="Command start column (1-indexed)")
    end_line: int = Field(ge=1, description="Command end line (1-indexed)")
    end_column: int = Field(ge=1, description="Command end column (1-indexed, just past the last character)")

class GoalState(BaseModel):
    command: Optional[CommandSpan] = Field(
        default=None,
        description="The Isar command enclosing the queried position — its full source "
                    "text and range. None if there is no command at the position."
    )
    subgoals: List[str] = Field(
        default_factory=list,
        description="Open subgoals of the proof state after the command runs — one "
                    "string per subgoal; empty list means no subgoals remain."
    )
    note: Optional[str] = Field(default=None, description="Warning note (e.g. line still running)")
```

**Example Response**:
```json
{
  "command": {
    "text": "by (auto simp: lemma1)",
    "start_line": 12,
    "start_column": 3,
    "end_line": 12,
    "end_column": 25
  },
  "subgoals": []
}
```

---

#### 4.2.2 `isabelle_command_output`

**Description**: Get the Isar command enclosing a position and the prover output it
produced. Auto-evaluates the line first if needed (requires a launched session).
Targeting follows the same `after_text` rule as `isabelle_goal`.

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
after_text: Annotated[Optional[str], Field(
    description="Optional text on the line; the command right after it is used. "
                "Without it, the command at the end of the line is used."
)] = None
```

**Output**: This tool is registered with `output_schema=None` and returns a
**formatted plain-text** `ToolResult`, not a JSON model. The underlying
`CommandOutputResult` (below) is rendered by `format_command_output` into a
location header (`[line N]` or `[line N-M]`), the command source text, and one
`[kind] message` line per output message (or `(no output)`).

```python
class OutputMessage(BaseModel):
    kind: str = Field(description="normal | tracing | warning | error | information | state")
    message: str = Field(description="Message content")

class CommandOutputResult(BaseModel):
    command: Optional[CommandSpan] = Field(
        default=None,
        description="The Isar command enclosing the queried position — its full source "
                    "text and range. None if there is no command at the position."
    )
    messages: List[OutputMessage] = Field(default_factory=list)
    note: Optional[str] = Field(default=None, description="Warning note (e.g. line still running)")
```

---

### 4.3 Session Management Tools

#### 4.3.1 `isabelle_launch`

**Description**: Start (or restart) the Isabelle prover with the given
session/logic. **Must be called before any evaluation or query tool** — the prover
does not auto-start. Calling it with the same session is a no-op; a different session
restarts the prover (any in-progress evaluation is discarded).

**Tool Annotations**:
```python
ToolAnnotations(
    title="Launch Session",
    idempotentHint=True,
)
```

**Input Parameters**:
```python
session: Annotated[str, Field(description="Session/logic name, e.g. HOL, HOL-Analysis, Minilang")]
session_dirs: Annotated[Optional[list[str]], Field(
    description="Extra -d session search dirs for non-builtin sessions; "
                "defaults to the server's working directory."
)] = None
```

**Output Model**: `SessionInfo` (below) — the running session name and server version.

#### 4.3.2 `isabelle_terminate`

**Description**: Terminate the running prover. The MCP server itself stays up; a fresh
prover (e.g. a different session) can be started afterwards with `isabelle_launch`.
Returns a plain-text `ToolResult` (`output_schema=None`).

**Tool Annotations**:
```python
ToolAnnotations(
    title="Terminate Session",
    destructiveHint=True,
    idempotentHint=True,
)
```

**Input Parameters**: None

#### 4.3.3 `isabelle_session_info`

**Description**: Get the name and server version of the current Isabelle session.

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
    version: str | None = Field(default=None, description="Isabelle server version (None if unknown)")
```

---

### 4.4 Evaluation Tools

These three tools drive Isabelle's processing. All return a **plain-text per-file
snapshot** (a `ToolResult` with `output_schema=None`, i.e. no structured output
schema), not a Pydantic model. Because checking runs asynchronously, an
`isabelle_evaluate_to` call may return before processing reaches the target — it
then reports that evaluation is still in progress, and the agent polls
`isabelle_evaluation_status` until it completes (or cancels a stuck run). Errors do
**not** halt checking: every command up to the target is checked even if an earlier
one fails.

The snapshot lists each relevant file with up to three columns — **errors** /
**warnings** / **running** — as 1-indexed line spans. `errors` is the line-deduped
union of the `text_overview_error` and `background_bad` decorations, so a `sorry`, a
failed proof, and a killed command all show up as errors (there is no separate
"sorry" category). `warnings` is `text_overview_warning`; `running` is
`background_running1` (still-executing forked proofs). Classification is
decoration-only — no diagnostics channel is read for the snapshot. For a file with
no decoration (e.g. an unopened dependency) the snapshot falls back to
`theory_status` **counts** (failed→errors, warned→warnings, no line numbers) and
uses `unprocessed`/`consolidated` to show "in progress" vs "clean". Full
error/warning message *text* is fetched separately via `isabelle_command_output`.

#### 4.4.1 `isabelle_evaluate_to`

**Description**: Start evaluating a theory file up to a location on a line.
Requires a launched session. The result may indicate evaluation is still in progress.

**Input Parameters**:
```python
file_path: Annotated[str, Field(description="Absolute path to .thy file")]
line: Annotated[int, Field(description="Target line number (1-indexed). Use -1 for last line.")]
after_text: Annotated[Optional[str], Field(
    description="Optional snippet to stop at. Evaluation proceeds through the command "
                "ending at this snippet, matched on token boundaries (ASCII/Unicode "
                "equivalent); it must BEGIN on `line` and may span onto following lines."
)] = None
```

#### 4.4.2 `isabelle_evaluation_status`

**Description**: Check the progress of an ongoing evaluation. Returns the current
per-file snapshot (errors / warnings / running line spans) and whether it finished.
**Input Parameters**: None. Reports "No evaluation in progress." when idle.

#### 4.4.3 `isabelle_cancel_evaluation`

**Description**: Cancel an ongoing evaluation. Stops Isabelle from processing
further; already-processed results remain valid for querying. **Input Parameters**:
None.

**Output** (shared by all three): a **plain-text string** (no `output_schema`).
Internally these tools return an `EvaluationView` dataclass (see
`src/isabelle_mcp/models.py`) which is rendered to the agent-facing text by
`format_evaluation_result` in `src/isabelle_mcp/evaluation.py`. The internal shapes
are:

```python
@dataclass
class FileSnapshot:
    file_path: str
    lined: bool   # True = decoration spans; False = theory_status-count fallback
    state: str    # "clean" | "in_progress" | "problems"
    errors: list[tuple[int, int]] = field(default_factory=list)    # (start, end) line spans
    warnings: list[tuple[int, int]] = field(default_factory=list)
    running: list[tuple[int, int]] = field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    running_count: int = 0

@dataclass
class EvaluationView:
    status: str   # complete | in_progress | no_evaluation | cancelled
    destination_line: int | None = None
    message: str = ""
    files: list[FileSnapshot] = field(default_factory=list)
    running_commands: list[RunningCommand] = field(default_factory=list)
```

The rendered text leads with the status message and then one block per file, e.g.:

```
Evaluation complete, arrived at line 42.

Scratch.thy:
  errors: 37, 40-41
  warnings: 12
  running: 55
```

A file with no problems renders as `Scratch.thy: clean`; an unopened dependency with
only theory_status counts renders as `Imported.thy: 1 error (no line info)` or
`Imported.thy: in progress`. A `RunningCommand` whose `elapsed_seconds` keeps
climbing while it stays in the `running` column is the signature of a stuck
evaluation; cancel it, fix the command, and re-evaluate.

---

### 4.5 File Synchronization

The agent edits `.thy`/`.ML` files on disk with ordinary tools, and the server pushes
those edits to Isabelle automatically:

- **Editor-opened `.thy` (the MCP's job).** A `FileWatcher` watches the parent
  directories of open files and, on any change, **immediately** pushes that file to
  Isabelle as a `textDocument/didChange` (event-driven — no dirty-set, no polling, no
  HTTP hook). Its four handlers include `moved`, so atomic-rename saves (Claude
  Edit/Write, jEdit, sed) are caught. A tool-call backstop re-stats every open file at
  the start of each MCP call and pushes any the watcher missed (content comparison is
  the final gate).
- **Dependency files (the server's job).** `.ML` blobs and imported `.thy` are synced
  by Isabelle's own vscode_server File_Watcher. Because that watcher debounces by
  `vscode_load_delay` (default `0.5` s), the backstop also stats the `theory_status`
  dependency set and waits out the debounce when a dependency was just edited.
- Pushing an edit *during* an in-progress evaluation is intentional and supported:
  PIDE re-checks incrementally and the processing tracker adopts the new version —
  exactly as editing in jEdit/VS Code while checking runs. The locked sync paths do not
  skip while an evaluation is active.

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

The server-level instructions string delivered to the agent on connect is the
`INSTRUCTIONS` literal in `src/isabelle_mcp/instructions.py` (returned by
`get_instructions()` and passed as `FastMCP(..., instructions=...)`). That file is
the single source of truth; it is **not** reproduced here, to avoid drift.

Its key points: positions are 1-indexed and file paths absolute; you edit `.thy`
files on disk and the server syncs them to Isabelle automatically (§4.5);
evaluation is asynchronous, so poll `isabelle_evaluation_status` and cancel a stuck
run rather than waiting for `complete`; and errors do not halt checking.

---

## 7. Current Verification Status

### 7.1 Functional Status

1. Current server registers 11 MCP tools.
2. Standard LSP features (hover, definition, local occurrences) are
   implemented and covered by tests.
3. PIDE tools use Isabelle2024 native notifications:
   - `isabelle_goal` uses `PIDE/caret_update`, `PIDE/state_init`,
     `PIDE/state_output`, and `PIDE/state_exit`.
   - `isabelle_command_output` uses `PIDE/dynamic_output`.
4. PIDE tools are best-effort: they may timeout if Isabelle emits no matching
   notification for the requested position/file.

### 7.2 Interface Consistency

1. Current tool names are `isabelle_launch`, `isabelle_terminate`,
   `isabelle_evaluate_to`, `isabelle_evaluation_status`,
   `isabelle_cancel_evaluation`, `isabelle_hover`, `isabelle_definition`,
   `isabelle_local_occurrences`, `isabelle_goal`, `isabelle_command_output`, and
   `isabelle_session_info`.
2. All public tool positions are 1-indexed.
3. Query tools return Pydantic models rather than bare lists; the three evaluation
   tools return a plain-text snapshot (no structured output schema).
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
| Position targeting | Optional column | `symbol` / `after_text` snippet | Snippet-based (agents miscount columns) |
| Diagnostics | `lean_diagnostic_messages` | evaluation snapshot + `isabelle_command_output` | Snapshot lists error/warning lines; message text via `command_output` |
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
