# Isa-LSP Architecture Design

**Version:** 0.1.0
**Date:** 2026-03-07
**Status:** Draft with current implementation notes

> Documentation reliability note:
> The current server exposes 10 MCP tools. Older architecture notes in this
> document mention `isabelle_edit` and 11 tools as a design direction; that tool
> is not registered in the current codebase.

## 1. Overview

Isa-LSP is a Python-based MCP (Model Context Protocol) server that acts as a bridge between AI agents and Isabelle's Language Server Protocol (LSP) implementation (`isabelle vscode_server`). The architecture follows the proven patterns from `lean-lsp-mcp` while adapting to Isabelle's PIDE (Prover IDE) specific features.

### 1.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    AI Agent (Claude)                         │
│                                                              │
│  - Processes natural language requests                       │
│  - Calls MCP tools for theorem proving                       │
│  - Interprets responses and generates proofs                 │
└────────────────┬────────────────────────────────────────────┘
                 │
                 │ MCP Protocol (stdio)
                 │ JSON-RPC requests/responses
                 │
┌────────────────▼────────────────────────────────────────────┐
│               Isa-LSP MCP Server (Python)                    │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  FastMCP Server                                       │   │
│  │  - Tool registration and routing                      │   │
│  │  - Request validation                                 │   │
│  │  - Response formatting                                │   │
│  └────────────┬─────────────────────────────────────────┘   │
│               │                                              │
│  ┌────────────▼─────────────────────────────────────────┐   │
│  │  MCP Tool Handlers (10 tools)                        │   │
│  │  - isabelle_hover                                    │   │
│  │  - isabelle_completions                              │   │
│  │  - isabelle_definition                               │   │
│  │  - isabelle_highlights                               │   │
│  │  - isabelle_diagnostics                              │   │
│  │  - isabelle_goal                                     │   │
│  │  - isabelle_command_output                           │   │
│  │  - isabelle_preview                                  │   │
│  │  - isabelle_build                                    │   │
│  │  - isabelle_session_info                            │   │
│  └────────────┬─────────────────────────────────────────┘   │
│               │                                              │
│  ┌────────────▼─────────────────────────────────────────┐   │
│  │  LSP Client Wrapper                                  │   │
│  │  - Process lifecycle management                      │   │
│  │  - JSON-RPC 2.0 communication                        │   │
│  │  - Request correlation (ID mapping)                  │   │
│  │  - Async notification handling                       │   │
│  │  - Document state tracking                           │   │
│  │  - PIDE state panel management                       │   │
│  └────────────┬─────────────────────────────────────────┘   │
│               │                                              │
│  ┌────────────▼─────────────────────────────────────────┐   │
│  │  Utilities                                           │   │
│  │  - Error handling (IsabelleToolError)                │   │
│  │  - URI ↔ file path conversion                        │   │
│  │  - Response formatters (HTML → text)                 │   │
│  │  - Position conversion (1-indexed ↔ 0-indexed)       │   │
│  └──────────────────────────────────────────────────────┘   │
└────────────────┬────────────────────────────────────────────┘
                 │
                 │ JSON-RPC 2.0 over stdin/stdout
                 │ LSP + PIDE protocols
                 │
┌────────────────▼────────────────────────────────────────────┐
│         isabelle vscode_server (Scala)                       │
│                                                              │
│  - Standard LSP methods (hover, completion, definition, etc.)│
│  - PIDE extensions (state panels, dynamic output, preview)   │
│  - Document processing and incremental type checking         │
│  - Document sync: Incremental (textDocumentSync = 2)         │
│  - Session management (logic images, build system)           │
└────────────────┬────────────────────────────────────────────┘
                 │
                 │ PIDE protocol
                 │
┌────────────────▼────────────────────────────────────────────┐
│            Isabelle Prover Process                           │
│                                                              │
│  - ML interpreter                                            │
│  - Proof state management                                    │
│  - Theory processing                                         │
│  - Session heap (HOL, Main, etc.)                            │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. Core Components

### 2.1 FastMCP Server

**Responsibility:** MCP protocol handling and tool registration

**Key Features:**
- Tool registration with `@mcp.tool()` decorator
- Request routing to appropriate tool handlers
- Input validation using Pydantic models
- Response serialization
- Lifespan context management

**Implementation:**
```python
from mcp import FastMCP

mcp = FastMCP("isabelle-lsp")

@dataclass
class AppContext:
    """Application lifespan context"""
    isabelle_session_path: Path | None
    lsp_client: IsabelleLSPClient | None
    session_start_time: float

@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Manage application lifecycle"""
    # Initialization
    context = AppContext(
        isabelle_session_path=Path(os.environ.get("ISABELLE_SESSION_PATH", "")),
        lsp_client=None,
        session_start_time=time.time(),
    )

    yield context

    # Cleanup
    if context.lsp_client:
        await context.lsp_client.shutdown()
```

---

### 2.2 LSP Client Wrapper

**Responsibility:** Manage communication with `isabelle vscode_server`

**File:** `src/isa_lsp/lsp_client.py`

**Key Features:**
- Subprocess lifecycle management
- JSON-RPC 2.0 message framing (Content-Length headers)
- Async request/response correlation
- Background notification listener
- Document state tracking
- PIDE state panel management

**Architecture:**
```python
class IsabelleLSPClient:
    def __init__(self, logic: str = "HOL", session_dirs: List[str] = None):
        self.logic = logic
        self.session_dirs = session_dirs or []
        self.process: Optional[subprocess.Popen] = None
        self.request_id = 0
        self.pending_requests: Dict[int, asyncio.Future] = {}
        self.open_documents: Dict[str, DocumentState] = {}
        self.diagnostics_cache: Dict[str, List[Diagnostic]] = {}
        self.state_panels: Dict[int, StatePanel] = {}
        self.reader_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start isabelle vscode_server process"""

    async def initialize(self):
        """Send LSP initialize request"""

    async def request(self, method: str, params: Dict) -> Any:
        """Send LSP request and wait for response"""

    async def notify(self, method: str, params: Dict):
        """Send LSP notification (no response)"""

    async def open_document(self, file_path: str, content: str = None):
        """Open document in LSP session"""

    async def change_document(self, file_path: str, new_content: str):
        """Send textDocument/didChange with full content (triggers PIDE reprocessing)"""

    async def close_document(self, file_path: str):
        """Close document in LSP session"""

    async def get_hover(self, file_path: str, line: int, column: int):
        """Get hover information"""

    async def get_completions(self, file_path: str, line: int, column: int):
        """Get completions"""

    async def get_definition(self, file_path: str, line: int, column: int):
        """Get definition location"""

    async def get_highlights(self, file_path: str, line: int, column: int):
        """Get document highlights"""

    async def get_diagnostics(self, file_path: str):
        """Get cached diagnostics for file"""

    async def create_state_panel(self, file_path: str, line: int, column: int):
        """Create PIDE state panel and return goals"""

    async def get_dynamic_output(self, file_path: str, line: int):
        """Get command output from dynamic output cache"""

    async def request_preview(self, file_path: str):
        """Request document preview"""

    async def shutdown(self):
        """Gracefully shutdown LSP server"""
```

**State Management:**
```python
@dataclass
class DocumentState:
    file_path: str
    uri: str
    version: int
    content: str
    language_id: str = "isabelle"

@dataclass
class StatePanel:
    panel_id: int
    file_path: str
    line: int
    column: int
    state_html: str = ""
    auto_update: bool = True
```

---

### 2.3 Message Flow

#### 2.3.1 JSON-RPC 2.0 Communication

**Request Format:**
```json
{
  "jsonrpc": "2.0",
  "id": 123,
  "method": "textDocument/hover",
  "params": {
    "textDocument": {"uri": "file:///path/to/file.thy"},
    "position": {"line": 41, "character": 14}
  }
}
```

**Response Format:**
```json
{
  "jsonrpc": "2.0",
  "id": 123,
  "result": {
    "contents": {
      "kind": "markdown",
      "value": "Suc :: nat ⇒ nat"
    },
    "range": {...}
  }
}
```

**Notification Format (Server → Client):**
```json
{
  "jsonrpc": "2.0",
  "method": "textDocument/publishDiagnostics",
  "params": {
    "uri": "file:///path/to/file.thy",
    "diagnostics": [...]
  }
}
```

#### 2.3.2 Message Framing

LSP uses Content-Length header framing:

```
Content-Length: 123\r\n
\r\n
{"jsonrpc":"2.0",...}
```

**Implementation:**
```python
async def _send(self, message: Dict):
    """Send JSON-RPC message with LSP framing"""
    content = json.dumps(message).encode('utf-8')
    header = f"Content-Length: {len(content)}\r\n\r\n".encode('ascii')
    self.process.stdin.write(header + content)
    await self.process.stdin.drain()

async def _read_loop(self):
    """Read LSP messages in background"""
    while True:
        # Read header
        header_line = await self.process.stdout.readline()
        if not header_line:
            break

        # Parse Content-Length
        match = re.match(b"Content-Length: (\\d+)\r\n", header_line)
        content_length = int(match.group(1))

        # Skip blank line
        await self.process.stdout.readline()

        # Read content
        content = await self.process.stdout.readexactly(content_length)
        message = json.loads(content.decode('utf-8'))

        # Handle message
        await self._handle_message(message)
```

---

### 2.4 PIDE State Panel Management

**Challenge:** PIDE state panels are asynchronous. Isabelle2024 defines
`PIDE/state_init` as a notification with no parameters; the server assigns the
state panel id and reports it in the next `PIDE/state_output` notification.
The client must update the caret, initialize a panel, learn the server id from
`state_output`, and use that id for `PIDE/state_exit`.

**Solution:** State machine for panel lifecycle

```python
class StatePanelManager:
    def __init__(self):
        self.state_lock = asyncio.Lock()
        self.init_waiters: List[asyncio.Future[tuple[int, str]]] = []

    async def query_position(self, client, file_path, line, character):
        """Query proof goals at one LSP position."""
        async with self.state_lock:
            future = asyncio.Future()
            self.init_waiters.append(future)
            panel_id = None

            try:
                await client.notify("PIDE/caret_update", {
                    "uri": file_path_to_uri(file_path),
                    "line": line,
                    "character": character,
                })
                await client.notify("PIDE/state_init", {})
                panel_id, html = await asyncio.wait_for(future, timeout=5.0)
                return parse_goals_from_html(html)
            finally:
                if panel_id is not None:
                    await client.notify("PIDE/state_exit", {"id": panel_id})

    def handle_state_output(self, panel_id: int, output: str):
        """Handle PIDE/state_output notification"""
        if self.init_waiters:
            self.init_waiters.pop(0).set_result((panel_id, output))

    def _parse_goals(self, html_output: str) -> List[str]:
        """Parse goals from HTML output"""
        # Strip HTML tags, extract goal text
        # Handle "no goals" case
        pass
```

---

### 2.5 Document Synchronization

**Challenge:** Keep LSP server's document state in sync with queries

**Solution:** Automatic document opening with caching

```python
class DocumentManager:
    def __init__(self, client: IsabelleLSPClient):
        self.client = client
        self.open_documents: Set[str] = set()

    async def ensure_open(self, file_path: str):
        """Ensure document is open in LSP session"""
        if file_path in self.open_documents:
            return

        # Read file content
        with open(file_path, 'r') as f:
            content = f.read()

        # Send didOpen
        uri = file_path_to_uri(file_path)
        await self.client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "isabelle",
                "version": 1,
                "text": content
            }
        })

        self.open_documents.add(file_path)

        # Wait for initial processing (heuristic: 2 seconds)
        await asyncio.sleep(2.0)
```

---

### 2.6 Document Editing and Dynamic Reprocessing (Design Target)

**Challenge:** Enable AI agents to edit theory files and have Isabelle incrementally reprocess changes — the same workflow as editing in Isabelle/VSCode.

**Protocol Background:**
Isabelle's `vscode_server` reports `textDocumentSync = 2` (Incremental per LSP spec). However, a client can always send full content replacement even when the server announces Incremental support — the LSP spec guarantees this fallback. We use full content replacement for simplicity. After receiving a `textDocument/didChange`:

1. **Input debounce** (100ms `vscode_input_delay`): rapid edits are batched via `Delay.last()`
2. **Flush to PIDE**: pending edits converted to `Document.Edit_Text` and sent to the prover via `session.update()`
3. **Incremental reprocessing**: PIDE reprocesses only affected commands in the document
4. **Output debounce** (500ms `vscode_output_delay`): updated diagnostics pushed via `textDocument/publishDiagnostics`

**Current status:** Not implemented in the current server. The following design
describes a future `change_document` / `wait_for_processing` layer and an
`isabelle_edit` MCP tool. For design details, see API_DESIGN.md Section 3.6.

**Component Interactions:**

```
future isabelle_edit MCP tool
    │
    ├─→ Edit Tool Handler (tools/edit.py)
    │   ├─→ Computes full new content (from line-range or full replacement)
    │   ├─→ Calls IsabelleLSPClient.change_document()
    │   ├─→ Optionally writes to disk
    │   └─→ Calls IsabelleLSPClient.wait_for_processing()
    │
    ├─→ IsabelleLSPClient (lsp_client.py)
    │   ├─→ Manages DocumentState (version, content cache)
    │   ├─→ Sends textDocument/didChange notification
    │   └─→ Monitors DiagnosticCache for processing completion
    │
    └─→ DiagnosticCache (lsp_client.py)
        └─→ Updated by publishDiagnostics notifications from vscode_server
```

**Key Design Decisions:**

- **Full sync** (not incremental): Isabelle's `vscode_server` accepts both. Full sync is simpler and avoids offset computation bugs. The server internally computes diffs from the new content.
- **Disk sync**: By default, also write to disk so that `git`, other editors, and external tools see consistent state. When `sync_to_disk=False`, the LSP buffer diverges from disk — a subsequent `open_document` (which reads from disk) would overwrite in-buffer changes.
- **Wait for processing**: By default, wait for PIDE to finish so the returned diagnostics reflect the new state. Can be disabled for batch edits.
- **Cache invalidation**: After an edit, all previously cached tool results (goals, hover, etc.) for that document are stale. The tool does not invalidate them automatically — callers must re-query.
- **No concurrent edit safety**: Line-range edits splice against the cached content. Overlapping edit calls can produce incorrect results. Callers must serialize edits to the same document.

---

### 2.7 Diagnostic Caching

**Challenge:** Diagnostics are sent via async notifications, but tools need synchronous access

**Solution:** Cache diagnostics from `publishDiagnostics` notifications

```python
class DiagnosticCache:
    def __init__(self):
        self.diagnostics: Dict[str, List[Dict]] = {}
        self.processing_status: Dict[str, bool] = {}

    def handle_publish_diagnostics(self, uri: str, diagnostics: List[Dict]):
        """Handle textDocument/publishDiagnostics notification"""
        file_path = uri_to_file_path(uri)
        self.diagnostics[file_path] = diagnostics

        # Heuristic: if no "running" decorations, processing is complete
        # (In reality, would need to track PIDE decorations)
        self.processing_status[file_path] = True

    def get_diagnostics(self, file_path: str) -> DiagnosticsResult:
        """Get cached diagnostics for file"""
        items = []
        for diag in self.diagnostics.get(file_path, []):
            items.append(DiagnosticMessage(
                severity=severity_to_string(diag["severity"]),
                message=diag["message"],
                line=diag["range"]["start"]["line"] + 1,  # Convert to 1-indexed
                column=diag["range"]["start"]["character"] + 1,
                end_line=diag["range"]["end"]["line"] + 1,
                end_column=diag["range"]["end"]["character"] + 1,
            ))

        success = all(item.severity != "error" for item in items)
        processing_complete = self.processing_status.get(file_path, False)

        return DiagnosticsResult(
            success=success,
            items=items,
            processing_complete=processing_complete,
            failed_dependencies=[]
        )
```

---

## 3. Tool Implementation Pattern

Each MCP tool follows this pattern:

```python
@mcp.tool(
    "isabelle_hover",
    annotations=ToolAnnotations(
        title="Hover Info",
        readOnlyHint=True,
        idempotentHint=True,
    ),
)
async def hover_info(
    ctx: Context,
    file_path: Annotated[str, Field(description="Absolute path to .thy file")],
    line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)],
    column: Annotated[int, Field(description="Column number (1-indexed)", ge=1)],
) -> HoverInfo:
    """Get type signature and documentation for symbol."""

    # 1. Get LSP client from context
    client = ctx.request_context.lifespan_context.lsp_client
    if not client:
        raise IsabelleToolError("Session not initialized. Please call isabelle_build first.")

    # 2. Ensure document is open
    await ensure_document_open(client, file_path)

    # 3. Convert to 0-indexed for LSP
    lsp_line = line - 1
    lsp_column = column - 1

    # 4. Call LSP method
    try:
        response = await client.get_hover(file_path, lsp_line, lsp_column)
        check_pide_response(response, "get_hover", allow_none=True)
    except Exception as e:
        raise IsabelleToolError(f"Failed to get hover info: {e}")

    # 5. Parse response
    symbol = extract_symbol_from_range(file_path, response.get("range"))
    info_text = response.get("contents", {}).get("value", "")

    # 6. Get line context
    with open(file_path, 'r') as f:
        lines = f.readlines()
        line_context = lines[line - 1].rstrip() if line <= len(lines) else ""

    # 7. Get diagnostics at position (optional)
    all_diagnostics = await client.get_diagnostics(file_path)
    position_diagnostics = filter_diagnostics_at_position(all_diagnostics, line, column)

    # 8. Return structured result
    return HoverInfo(
        symbol=symbol,
        info=info_text,
        line_context=line_context,
        diagnostics=position_diagnostics
    )
```

---

## 4. Session Lifecycle

### 4.1 Initialization Flow

```
User calls isabelle_build(logic="HOL")
         │
         ├─→ Check if session heap exists
         │   │
         │   └─→ If not or clean=True:
         │       ├─→ Run: isabelle build -b HOL
         │       └─→ Wait for build completion (may take minutes)
         │
         ├─→ Spawn: isabelle vscode_server -l HOL [options]
         │   │
         │   └─→ Start stdin/stdout readers
         │
         ├─→ Send LSP initialize request
         │   │
         │   └─→ Wait for initialize response
         │
         ├─→ Send LSP initialized notification
         │
         ├─→ Start background notification listener
         │
         └─→ Return BuildResult with server capabilities
```

### 4.2 Tool Call Flow

```
AI Agent calls isabelle_goal(file, line)
         │
         ├─→ MCP Server validates input
         │
         ├─→ Get LSP client from context
         │
         ├─→ Ensure document is open
         │   │
         │   └─→ If not: send textDocument/didOpen
         │
         ├─→ Create state panel
         │   │
         │   ├─→ Send PIDE/state_init
         │   ├─→ Send PIDE/caret_update (line start)
         │   ├─→ Wait for PIDE/state_output → goals_before
         │   ├─→ Send PIDE/caret_update (line end)
         │   ├─→ Wait for PIDE/state_output → goals_after
         │   └─→ Send PIDE/state_exit
         │
         ├─→ Parse goals from HTML
         │
         └─→ Return GoalState model
```

### 4.3 Edit Tool Call Flow (Future Design)

```
AI Agent calls isabelle_edit(file, start_line=42, end_line=42, new_text="  by simp")
         │
         ├─→ MCP Server validates input
         │   (exactly one of new_content or start_line/end_line/new_text)
         │
         ├─→ Get LSP client from context
         │
         ├─→ Ensure document is open
         │   │
         │   └─→ If not: send textDocument/didOpen, wait for initial processing
         │
         ├─→ Compute new full content
         │   │
         │   ├─→ If new_content provided: use directly
         │   └─→ If line range provided: splice new_text into current content
         │
         ├─→ Send textDocument/didChange
         │   │
         │   ├─→ Increment document version
         │   ├─→ Update DocumentState.content cache
         │   └─→ Send notification with full new content
         │
         ├─→ (Optional) Write to disk
         │
         ├─→ Wait for PIDE reprocessing
         │   │
         │   ├─→ Server debounces input (100ms)
         │   ├─→ PIDE processes changes incrementally
         │   ├─→ Server pushes publishDiagnostics (debounced 500ms)
         │   └─→ Client detects processing complete (no updates for 500ms+)
         │
         ├─→ Collect diagnostics from cache
         │
         └─→ Return EditResult(success, version, diagnostics, processing_complete)
```

### 4.4 Shutdown Flow

```
User calls isabelle_shutdown_session() OR
Process termination detected
         │
         ├─→ Send LSP shutdown request
         │
         ├─→ Wait for response
         │
         ├─→ Send LSP exit notification
         │
         ├─→ Cancel background reader task
         │
         ├─→ Terminate isabelle vscode_server process
         │
         ├─→ Clear document cache
         │
         └─→ Reset client state
```

---

## 5. Error Handling Strategy

### 5.1 Error Categories

1. **Session Errors**
   - Session not initialized
   - Build failures
   - Process crashes

2. **Document Errors**
   - File not found
   - Document not open
   - Invalid position

3. **LSP/PIDE Errors**
   - Timeout (no response in N seconds)
   - LSP error response
   - Parse errors

4. **Validation Errors**
   - Invalid parameters
   - Invalid file paths

### 5.2 Error Handling Flow

```python
try:
    # Tool implementation
    result = await some_lsp_call()

except asyncio.TimeoutError:
    raise IsabelleToolError("PIDE timeout during operation")

except FileNotFoundError as e:
    raise IsabelleToolError(f"File not found: {file_path}")

except Exception as e:
    # Log unexpected errors
    logger.error(f"Unexpected error in tool: {e}", exc_info=True)
    raise IsabelleToolError(f"Internal error: {e}")
```

All `IsabelleToolError` exceptions are caught by FastMCP and returned as error responses to the MCP client.

---

## 6. Technology Stack

### 6.1 Core Dependencies

- **Python**: ≥ 3.10 (for modern async/await and type hints)
- **FastMCP**: MCP protocol implementation
- **Pydantic**: Data validation and serialization
- **asyncio**: Async I/O for LSP communication

### 6.2 Isabelle Dependencies

- **Isabelle2024**: Includes `isabelle vscode_server`
- **Logic Images**: Pre-built session heaps (HOL, Main, etc.)

### 6.3 Development Dependencies

- **pytest**: Unit and integration testing
- **pytest-asyncio**: Async test support
- **mypy**: Type checking
- **black**: Code formatting

---

## 7. Performance Considerations

### 7.1 Session Reuse

**Problem:** Starting `isabelle vscode_server` is expensive (10-30s)

**Solution:** Keep long-lived process across multiple MCP tool calls

**Implementation:**
- Store LSP client in lifespan context
- Reuse same process for all tools in a session
- Only restart on explicit `isabelle_build` call

### 7.2 Document Opening

**Problem:** PIDE needs time to process documents (1-5s depending on size)

**Solution:** Wait heuristics and caching

**Implementation:**
- Cache list of open documents
- Add 2-second delay after opening before first query
- Return `processing_complete` flag in diagnostics

### 7.3 State Panel Management

**Problem:** Creating state panels for each goal query is slow

**Solution:** Future optimization with persistent panels

**Current MVP:**
- Create panel, query, destroy for each call
- Acceptable for MVP (< 1s per query)

**Future Optimization:**
- Keep one persistent panel per file
- Reuse panel for multiple queries

---

## 8. Testing Strategy

### 8.1 Unit Tests

**Target:** Individual components in isolation

**Examples:**
- URI ↔ file path conversion
- Position conversion (1-indexed ↔ 0-indexed)
- Goal parsing from HTML
- Diagnostic filtering
- Error handling

**Tools:** pytest with mocked LSP client

### 8.2 Integration Tests

**Target:** End-to-end workflows with real `isabelle vscode_server`

**Examples:**
- Session initialization with HOL
- Open document → get diagnostics
- Get hover info for known symbol
- Get proof state for simple lemma
- Complete shutdown lifecycle

**Requirements:**
- Isabelle2024 installed
- HOL session built
- Test theory files

### 8.3 Test Theory Files

Create minimal `.thy` files for testing:

```isabelle
theory Test_Basic
  imports Main
begin

lemma simple_lemma: "P ⟶ P"
  by auto

definition "test_def = (42 :: nat)"

theorem test_thm: "test_def = 42"
  unfolding test_def_def by simp

end
```

---

## 9. Deployment Model

### 9.1 As MCP Server

**Installation:**
```bash
cd contrib/Isa-LSP
pip install -e .
```

**Configuration (Claude Desktop):**
```json
{
  "mcpServers": {
    "isabelle-lsp": {
      "command": "python",
      "args": ["-m", "isa_lsp.server"],
      "env": {
        "ISABELLE_SESSION_PATH": "/path/to/isabelle/session"
      }
    }
  }
}
```

**Runtime:**
- MCP server runs as subprocess of Claude Desktop
- stdin/stdout used for MCP protocol
- LSP client spawns `isabelle vscode_server` as subprocess

---

## 10. Future Architectural Enhancements

### 10.1 Command Execution Framework

For Phase 2 features (sledgehammer, find_theorems, try methods).

Once `isabelle_edit` provides a `change_document` + `wait_for_processing`
foundation, the command execution framework can build on top of it:

**Architecture:**
```python
class CommandExecutor:
    """Execute Isabelle commands by injecting into theory files.

    Built on top of change_document() from the document editing layer.
    """

    async def execute_command(
        self,
        client: IsabelleLSPClient,
        file_path: str,
        line: int,
        command: str,
        timeout: float = 30.0
    ) -> CommandResult:
        """
        1. Save original content from DocumentState
        2. Inject command at position via change_document()
        3. Wait for PIDE reprocessing via wait_for_processing()
        4. Parse command_output / diagnostics for results
        5. Restore original content via change_document()
        6. Return structured result
        """
```

### 10.2 File Outline Parser

Custom Isabelle syntax parser for `isabelle_file_outline`:

**Approach:**
- Parse theory file for structure
- Extract imports, type definitions, constants, lemmas, theorems
- No need for full semantic analysis
- Regex-based or simple parser

### 10.3 Persistent State Panels

Optimize goal queries by keeping panels alive:

**Approach:**
- One state panel per open document
- Update caret position instead of creating new panel
- Destroy panel when document closes
- 10x faster goal queries

---

## Appendix A: Data Flow Diagrams

### A.1 Hover Info Query

```
AI Agent
   │ MCP: isabelle_hover(file, line=42, col=15)
   │
   ▼
MCP Server
   │ Validate inputs
   │ Get LSP client from context
   │
   ▼
Document Manager
   │ ensure_document_open(file)
   │ Check if file in open_documents
   │
   ├─→ Not open: send textDocument/didOpen
   │             wait for processing
   │
   ▼
LSP Client
   │ Convert to 0-indexed (line=41, col=14)
   │ Generate request ID
   │ Create Future for response
   │
   │ JSON-RPC: {"id": 1, "method": "textDocument/hover",
   │            "params": {"textDocument": {"uri": "file://..."},
   │                      "position": {"line": 41, "character": 14}}}
   ▼
isabelle vscode_server
   │ Process request
   │ Query PIDE for hover info
   │
   │ JSON-RPC: {"id": 1, "result": {"contents": {...}, "range": {...}}}
   ▼
LSP Client
   │ Match response ID to Future
   │ Resolve Future with result
   │
   ▼
Tool Handler
   │ Parse hover contents
   │ Extract symbol text
   │ Get line context from file
   │ Get diagnostics at position
   │
   │ Return: HoverInfo(symbol="Suc", info="nat => nat", ...)
   ▼
AI Agent
```

### A.2 Goal State Query

```
AI Agent
   │ MCP: isabelle_goal(file, line=42, column=None)
   │
   ▼
MCP Server
   │ Route to goal tool handler
   │
   ▼
State Panel Manager
   │ PIDE: {"method": "PIDE/caret_update",
   │        "params": {"line": 41, "character": 0}}
   │ PIDE: {"method": "PIDE/state_init"}
   │
   ▼
isabelle vscode_server
   │ Update caret to line start
   │ Create state panel with server-assigned id
   │ Query PIDE for proof state
   │
   │ PIDE: {"method": "PIDE/state_output",
   │        "params": {"id": 1, "content": "<html>goal (2 subgoals): ...</html>"}}
   ▼
State Panel Manager
   │ Receive state_output and learn panel id = 1
   │ Store as goals_before
   │ PIDE: {"method": "PIDE/state_exit", "params": {"id": "<panel_id>"}}
   │
   │ Repeat the same temporary-panel sequence at line end:
   │ PIDE: {"method": "PIDE/caret_update",
   │        "params": {"line": 41, "character": <end>}}
   │ PIDE: {"method": "PIDE/state_init"}
   │
   ▼
isabelle vscode_server
   │ Update caret to line end
   │ Create state panel with server-assigned id
   │ Query PIDE for proof state
   │
   │ PIDE: {"method": "PIDE/state_output",
   │        "params": {"id": 1, "content": "<html>no goals</html>"}}
   ▼
State Panel Manager
   │ Receive state_output and learn panel id
   │ Store as goals_after
   │
   │ PIDE: {"method": "PIDE/state_exit", "params": {"id": "<panel_id>"}}
   │
   │ Parse goals from HTML
   │ Extract goal text, strip formatting
   │
   │ Return: GoalState(goals_before=["P x"], goals_after=[], ...)
   ▼
AI Agent
```

### A.3 Document Edit + Reprocessing (Future Design)

```
AI Agent
   │ MCP: isabelle_edit(file, start_line=42, end_line=42, new_text="  by simp")
   │
   ▼
MCP Server
   │ Validate input (line-range mode)
   │ Ensure document open
   │
   ▼
Edit Tool Handler
   │ Read current content from DocumentState cache
   │ Splice new_text into lines 42..42
   │ Compute full new content
   │
   │ JSON-RPC: {"method": "textDocument/didChange",
   │            "params": {"textDocument": {"uri": "file://...", "version": 2},
   │                      "contentChanges": [{"text": "<full new content>"}]}}
   ▼
isabelle vscode_server
   │ Receive didChange
   │ Store as pending_edits
   │ Debounce (100ms vscode_input_delay)
   │ Flush pending_edits to PIDE
   │
   ▼
Isabelle PIDE
   │ Incremental reprocessing
   │ (only reprocesses affected commands)
   │
   │ JSON-RPC: {"method": "textDocument/publishDiagnostics",
   │            "params": {"uri": "file://...", "diagnostics": [...]}}
   │ (debounced 500ms vscode_output_delay)
   ▼
LSP Client (read loop)
   │ Cache updated diagnostics
   │ Update last_update timestamp
   │
   ▼
Edit Tool Handler
   │ Write new content to disk (sync_to_disk=true)
   │ Poll: wait until is_processing_complete() → true
   │   (no new diagnostics for 500ms+)
   │
   │ Collect diagnostics from cache
   │ Compute success = (no errors)
   │
   │ Return: EditResult(success=true, version=2, diagnostics=[], ...)
   ▼
AI Agent
```

---

**Document Status**: Ready for API Design
**Next Step**: Create API_DESIGN.md with detailed endpoint specifications
