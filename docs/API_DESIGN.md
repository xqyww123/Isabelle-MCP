# Isa-LSP API Design Document

**Version:** 0.1.0
**Date:** 2026-03-07
**Status:** Draft

## 1. Overview

This document provides detailed API design and implementation guidance for the 10 MCP tools in Isa-LSP. For high-level specifications, see SPECIFICATION.md. For architecture, see ARCHITECTURE.md.

---

## 2. LSP Method Mappings

### 2.1 Standard LSP Methods

| MCP Tool | LSP Method | Request Params | Response Fields |
|----------|------------|----------------|-----------------|
| `isabelle_hover_info` | `textDocument/hover` | `TextDocumentPositionParams` | `Hover` with `contents` and `range` |
| `isabelle_completions` | `textDocument/completion` | `CompletionParams` | `CompletionList` with `items[]` |
| `isabelle_declaration_location` | `textDocument/definition` | `DefinitionParams` | `Location[]` or `LocationLink[]` |
| `isabelle_document_highlights` | `textDocument/documentHighlight` | `DocumentHighlightParams` | `DocumentHighlight[]` |
| `isabelle_diagnostic_messages` | (notifications) | - | Cached from `publishDiagnostics` |

### 2.2 PIDE Extension Methods

| MCP Tool | PIDE Methods | Flow |
|----------|--------------|------|
| `isabelle_goal` | `PIDE/state_init`, `PIDE/caret_update`, `PIDE/state_output`, `PIDE/state_exit` | Multi-step async |
| `isabelle_command_output` | `PIDE/dynamic_output` | Notification-based |
| `isabelle_preview` | `PIDE/preview_request`, `PIDE/preview_response` | Request-response |

### 2.3 Session Management

| MCP Tool | Implementation | External Commands |
|----------|----------------|-------------------|
| `isabelle_build` | Spawn `isabelle build` + `vscode_server` | `isabelle build -b <session>` |
| `isabelle_session_info` | Query LSP client state | - |

---

## 3. Tool Implementation Details

### 3.1 `isabelle_hover_info`

**LSP Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "textDocument/hover",
  "params": {
    "textDocument": {
      "uri": "file:///absolute/path/to/file.thy"
    },
    "position": {
      "line": 41,
      "character": 14
    }
  }
}
```

**LSP Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "contents": {
      "kind": "markdown",
      "value": "**Suc** :: nat ⇒ nat\n\nThe successor function for natural numbers."
    },
    "range": {
      "start": {"line": 41, "character": 14},
      "end": {"line": 41, "character": 17}
    }
  }
}
```

**Implementation Notes:**
1. Convert file_path → URI: `file:///` + absolute path
2. Convert positions: MCP (1-indexed) → LSP (0-indexed)
3. Extract symbol text from range if available
4. Parse markdown content (remove ** formatting if needed)
5. Get line context by reading file
6. Optional: filter diagnostics at same position

**Edge Cases:**
- No hover info available → return empty `info` field
- Position out of bounds → LSP returns null, tool raises error
- Symbol spans multiple tokens → use range to extract exact text

**Code Snippet:**
```python
async def hover_info(ctx, file_path, line, column):
    client = get_lsp_client(ctx)
    await ensure_document_open(client, file_path)

    # Convert to 0-indexed
    lsp_line, lsp_col = line - 1, column - 1

    # Call LSP
    response = await client.request("textDocument/hover", {
        "textDocument": {"uri": file_path_to_uri(file_path)},
        "position": {"line": lsp_line, "character": lsp_col}
    })

    check_pide_response(response, "hover", allow_none=True)

    # Parse response
    if not response or "contents" not in response:
        return HoverInfo(symbol="", info="", line_context=get_line(file_path, line))

    contents = response["contents"]
    info_text = contents.get("value", "") if isinstance(contents, dict) else str(contents)

    # Extract symbol from range
    symbol = ""
    if "range" in response:
        symbol = extract_text_from_range(file_path, response["range"])

    return HoverInfo(
        symbol=symbol,
        info=info_text,
        line_context=get_line(file_path, line),
        diagnostics=[]
    )
```

---

### 3.2 `isabelle_completions`

**LSP Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "textDocument/completion",
  "params": {
    "textDocument": {"uri": "file:///path/to/file.thy"},
    "position": {"line": 10, "character": 5}
  }
}
```

**LSP Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "isIncomplete": false,
    "items": [
      {
        "label": "Suc",
        "kind": 3,
        "detail": "nat ⇒ nat",
        "documentation": "Successor function",
        "insertText": "Suc",
        "textEdit": {
          "range": {
            "start": {"line": 10, "character": 4},
            "end": {"line": 10, "character": 5}
          },
          "newText": "Suc"
        }
      }
    ]
  }
}
```

**Completion Kinds (LSP Enum):**
- 1: Text
- 2: Method
- 3: Function
- 4: Constructor
- 5: Field
- 6: Variable
- 7: Class
- 9: Module
- 14: Keyword
- 15: File
- 21: Constant

**Implementation Notes:**
1. Isabelle returns MANY completions (50-200), filter to `max_completions`
2. Sort by relevance: prefix match > contains > alphabetical
3. Map LSP `kind` enum to string: `{3: "function", 6: "variable", ...}`
4. Extract `insertText` from `textEdit.newText` if present
5. Handle symbol abbreviations (Isabelle-specific trigger characters)

**Sorting Algorithm:**
```python
def sort_completions(items: List[CompletionItem], cursor_prefix: str) -> List[CompletionItem]:
    """Sort completions by relevance to cursor prefix"""
    prefix_lower = cursor_prefix.lower()

    def sort_key(item):
        label_lower = item.label.lower()
        if label_lower.startswith(prefix_lower):
            return (0, label_lower)  # Prefix match (highest priority)
        elif prefix_lower in label_lower:
            return (1, label_lower)  # Contains (medium priority)
        else:
            return (2, label_lower)  # Alphabetical (low priority)

    items.sort(key=sort_key)
    return items[:max_completions]
```

**Edge Cases:**
- Empty completions → return `items=[]`
- Trigger character (e.g., `\`) → Isabelle handles symbol completions
- Mid-word position → LSP adjusts range automatically

---

### 3.3 `isabelle_declaration_location`

**LSP Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "textDocument/definition",
  "params": {
    "textDocument": {"uri": "file:///path/to/file.thy"},
    "position": {"line": 20, "character": 10}
  }
}
```

**LSP Response (Single Location):**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "uri": "file:///path/to/definition.thy",
    "range": {
      "start": {"line": 50, "character": 0},
      "end": {"line": 50, "character": 15}
    }
  }
}
```

**LSP Response (Multiple Locations):**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": [
    {"uri": "file:///path/to/def1.thy", "range": {...}},
    {"uri": "file:///path/to/def2.thy", "range": {...}}
  ]
}
```

**Implementation Notes:**
1. LSP can return single `Location` or `Location[]`
2. Normalize to always return list
3. Convert URIs back to file paths
4. Convert positions to 1-indexed
5. Extract symbol name from original position

**Edge Cases:**
- No definition found → return `locations=[]`
- Definition in same file → file_path same as input
- Definition in library → file_path points to Isabelle distribution

**Code Snippet:**
```python
def normalize_definition_response(response):
    """Normalize LSP definition response to list"""
    if response is None:
        return []
    elif isinstance(response, list):
        return response
    else:
        return [response]

async def declaration_location(ctx, file_path, line, column):
    client = get_lsp_client(ctx)
    await ensure_document_open(client, file_path)

    response = await client.request("textDocument/definition", {
        "textDocument": {"uri": file_path_to_uri(file_path)},
        "position": {"line": line - 1, "character": column - 1}
    })

    locations = normalize_definition_response(response)

    # Extract symbol at query position
    symbol = extract_symbol_at_position(file_path, line, column)

    return DeclarationLocation(
        symbol=symbol,
        locations=[
            Location(
                file_path=uri_to_file_path(loc["uri"]),
                line=loc["range"]["start"]["line"] + 1,
                column=loc["range"]["start"]["character"] + 1
            )
            for loc in locations
        ]
    )
```

---

### 3.4 `isabelle_document_highlights`

**LSP Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "textDocument/documentHighlight",
  "params": {
    "textDocument": {"uri": "file:///path/to/file.thy"},
    "position": {"line": 15, "character": 8}
  }
}
```

**LSP Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": [
    {
      "range": {
        "start": {"line": 15, "character": 8},
        "end": {"line": 15, "character": 12}
      },
      "kind": 1
    },
    {
      "range": {
        "start": {"line": 20, "character": 5},
        "end": {"line": 20, "character": 9}
      },
      "kind": 2
    }
  ]
}
```

**DocumentHighlightKind (LSP Enum):**
- 1: Text (default)
- 2: Read (variable read)
- 3: Write (variable write)

**Implementation Notes:**
1. Isabelle primarily returns kind=1 (text highlighting)
2. Convert ranges to 1-indexed positions
3. Map kind enum to string
4. Extract symbol text from first highlight range

**Edge Cases:**
- No highlights found → return `highlights=[]`
- Symbol only occurs once → return single-item list
- Partial highlights (e.g., in comments) → LSP filters appropriately

---

### 3.5 `isabelle_diagnostic_messages`

**LSP Notification (Server → Client):**
```json
{
  "jsonrpc": "2.0",
  "method": "textDocument/publishDiagnostics",
  "params": {
    "uri": "file:///path/to/file.thy",
    "diagnostics": [
      {
        "range": {
          "start": {"line": 10, "character": 5},
          "end": {"line": 10, "character": 10}
        },
        "severity": 1,
        "message": "Undefined constant \"foo\"",
        "source": "Isabelle"
      }
    ]
  }
}
```

**Diagnostic Severity (LSP Enum):**
- 1: Error
- 2: Warning
- 3: Information
- 4: Hint

**Implementation Notes:**
1. **Caching Required**: Diagnostics arrive via async notifications
2. Store diagnostics by file URI in cache
3. Tool reads from cache synchronously
4. Filter by line range if `start_line`/`end_line` provided
5. Compute `success` flag: true if no errors in range

**Processing Status Heuristic:**
- PIDE sends diagnostics incrementally as processing progresses
- Heuristic: if last diagnostic was received > 500ms ago, consider complete
- Better: track `PIDE/decoration` with `background_running` type

**Code Snippet:**
```python
class DiagnosticCache:
    def __init__(self):
        self.diagnostics: Dict[str, List[Dict]] = {}
        self.last_update: Dict[str, float] = {}

    def handle_publish_diagnostics(self, uri: str, diagnostics: List[Dict]):
        """Handle LSP publishDiagnostics notification"""
        file_path = uri_to_file_path(uri)
        self.diagnostics[file_path] = diagnostics
        self.last_update[file_path] = time.time()

    def get_diagnostics(
        self,
        file_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None
    ) -> DiagnosticsResult:
        """Get cached diagnostics, optionally filtered by line range"""
        all_diags = self.diagnostics.get(file_path, [])

        # Filter by line range
        filtered = []
        for diag in all_diags:
            diag_line = diag["range"]["start"]["line"] + 1  # Convert to 1-indexed
            if start_line and diag_line < start_line:
                continue
            if end_line and diag_line > end_line:
                continue
            filtered.append(diag)

        # Convert to DiagnosticMessage models
        items = [
            DiagnosticMessage(
                severity=severity_to_string(diag["severity"]),
                message=diag["message"],
                line=diag["range"]["start"]["line"] + 1,
                column=diag["range"]["start"]["character"] + 1,
                end_line=diag["range"]["end"]["line"] + 1,
                end_column=diag["range"]["end"]["character"] + 1
            )
            for diag in filtered
        ]

        # Compute success flag
        success = all(item.severity != "error" for item in items)

        # Check processing status (heuristic)
        last_update = self.last_update.get(file_path, 0)
        processing_complete = (time.time() - last_update) > 0.5

        return DiagnosticsResult(
            success=success,
            items=items,
            processing_complete=processing_complete,
            failed_dependencies=[]
        )
```

**Edge Cases:**
- File not yet opened → return empty diagnostics with warning
- PIDE still processing → return `processing_complete=false`
- Build failures → extract failed dependency paths from special diagnostics

---

### 3.6 `isabelle_goal`

**PIDE Flow:**

```
1. Send: PIDE/state_init
   ← No immediate response, panel created internally

2. Send: PIDE/caret_update
   {"uri": "file:///...", "line": 41, "character": 0}
   ← Triggers state recomputation

3. Receive: PIDE/state_output
   {"id": <panel_id>, "content": "<html>...goals...</html>", "auto_update": true}

4. Send: PIDE/caret_update
   {"uri": "file:///...", "line": 41, "character": <end_of_line>}

5. Receive: PIDE/state_output
   {"id": <panel_id>, "content": "<html>...goals...</html>"}

6. Send: PIDE/state_exit
   {"id": <panel_id>}
```

**HTML Output Format (Example):**
```html
<html>
  <body>
    <div class="state">
      <h3>proof (prove)</h3>
      <pre class="goals">
goal (2 subgoals):
 1. ⋀x. P x ⟹ Q x
 2. R y
      </pre>
      <div class="context">
        fix x y
        assume "A x" "B y"
      </div>
    </div>
  </body>
</html>
```

**Parsing Strategy:**
```python
def parse_goals_from_html(html: str) -> List[str]:
    """Extract goal text from PIDE HTML output"""
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '', html)

    # Handle special cases
    if "no goals" in text.lower():
        return []

    # Extract goals (simple heuristic: lines starting with digits)
    goals = []
    for line in text.split('\n'):
        line = line.strip()
        # Match patterns like "1. goal_text" or "⋀x. goal_text"
        if re.match(r'^\d+\.', line) or re.match(r'^⋀', line):
            goals.append(line)

    return goals
```

**Implementation Notes:**
1. **Panel ID Management**: Track panel IDs for matching responses
2. **Async Coordination**: Use `asyncio.Future` for waiting on `state_output`
3. **Timeout**: 5-10 seconds max wait for state output
4. **Before/After Pattern**: If column is None, query twice (line start and end)
5. **Context Extraction**: Parse `<div class="context">` if available

**Edge Cases:**
- No proof state available → return empty goals
- Timeout waiting for state_output → raise error
- Panel creation fails → retry once
- HTML parsing errors → return raw text as single goal

**Code Snippet:**
```python
class StatePanelManager:
    def __init__(self):
        self.next_panel_id = 1
        self.output_futures: Dict[int, asyncio.Future] = {}

    async def get_goals(
        self,
        client: IsabelleLSPClient,
        file_path: str,
        line: int,
        column: Optional[int] = None
    ) -> GoalState:
        """Get proof goals using state panel mechanism"""
        panel_id = self.next_panel_id
        self.next_panel_id += 1

        try:
            # Initialize panel
            await client.notify("PIDE/state_init", {})

            if column is None:
                # Get before and after
                goals_before = await self._query_position(client, file_path, line, 0, panel_id)
                line_content = get_line(file_path, line)
                goals_after = await self._query_position(client, file_path, line, len(line_content), panel_id)

                return GoalState(
                    line_context=line_content,
                    goals_before=goals_before,
                    goals_after=goals_after,
                    goals=None,
                    context=None
                )
            else:
                # Get at specific column
                goals = await self._query_position(client, file_path, line, column - 1, panel_id)

                return GoalState(
                    line_context=get_line(file_path, line),
                    goals=goals,
                    goals_before=None,
                    goals_after=None,
                    context=None
                )

        finally:
            # Always close panel
            await client.notify("PIDE/state_exit", {"id": panel_id})

    async def _query_position(
        self,
        client: IsabelleLSPClient,
        file_path: str,
        line: int,
        column: int,
        panel_id: int
    ) -> List[str]:
        """Query goals at specific position"""
        # Create future for response
        future = asyncio.Future()
        self.output_futures[panel_id] = future

        # Send caret update
        await client.notify("PIDE/caret_update", {
            "uri": file_path_to_uri(file_path),
            "line": line - 1,  # Convert to 0-indexed
            "character": column
        })

        # Wait for state_output (with timeout)
        try:
            html_output = await asyncio.wait_for(future, timeout=5.0)
            return parse_goals_from_html(html_output)
        except asyncio.TimeoutError:
            raise IsabelleToolError("Timeout waiting for proof state")

    def handle_state_output(self, panel_id: int, html_content: str):
        """Called by LSP client when PIDE/state_output received"""
        if panel_id in self.output_futures:
            self.output_futures[panel_id].set_result(html_content)
```

---

### 3.7 `isabelle_command_output`

**PIDE Notification (Server → Client):**
```json
{
  "jsonrpc": "2.0",
  "method": "PIDE/dynamic_output",
  "params": {
    "content": "<html><div class='writeln'>Proof complete</div><div class='warning'>Unused variable</div></html>"
  }
}
```

**Implementation Notes:**
1. **Cache Based**: `dynamic_output` is sent when caret moves
2. Cache output by file and line number
3. Parse HTML to extract message type and text
4. Return messages for requested line

**Output Types:**
- `writeln`: Normal prover output
- `warning`: Warnings
- `error`: Errors
- `information`: Info messages

**Parsing Strategy:**
```python
def parse_dynamic_output(html: str) -> List[OutputMessage]:
    """Parse PIDE dynamic output HTML"""
    messages = []

    # Extract message divs
    for match in re.finditer(r'<div class=[\'"]([^\'"]+)[\'"]>(.*?)</div>', html, re.DOTALL):
        kind = match.group(1)
        text = re.sub(r'<[^>]+>', '', match.group(2)).strip()

        # Map CSS class to message kind
        kind_map = {
            'writeln': 'writeln',
            'warning': 'warning',
            'error': 'error',
            'information': 'information'
        }

        messages.append(OutputMessage(
            kind=kind_map.get(kind, 'writeln'),
            text=text
        ))

    return messages
```

**Edge Cases:**
- No output at line → return empty messages
- Multiple commands on line → return combined output
- HTML parsing errors → return raw HTML as single message

---

### 3.8 `isabelle_preview`

**PIDE Request:**
```json
{
  "jsonrpc": "2.0",
  "method": "PIDE/preview_request",
  "params": {
    "uri": "file:///path/to/file.thy",
    "column": 0
  }
}
```

**PIDE Response:**
```json
{
  "jsonrpc": "2.0",
  "method": "PIDE/preview_response",
  "params": {
    "uri": "file:///path/to/file.thy",
    "column": 0,
    "label": "Theory Name",
    "content": "<html>...full document HTML...</html>"
  }
}
```

**Implementation Notes:**
1. Send `preview_request` notification
2. Wait for `preview_response` notification
3. Match by URI and column
4. Timeout after 30 seconds (preview generation can be slow)

**Code Snippet:**
```python
class PreviewManager:
    def __init__(self):
        self.preview_futures: Dict[str, asyncio.Future] = {}

    async def request_preview(
        self,
        client: IsabelleLSPClient,
        file_path: str
    ) -> PreviewResult:
        """Request document preview"""
        uri = file_path_to_uri(file_path)
        key = f"{uri}:0"

        # Create future for response
        future = asyncio.Future()
        self.preview_futures[key] = future

        # Send request
        await client.notify("PIDE/preview_request", {
            "uri": uri,
            "column": 0
        })

        # Wait for response
        try:
            response = await asyncio.wait_for(future, timeout=30.0)
            return PreviewResult(
                html_content=response["content"],
                title=response.get("label", "Preview")
            )
        except asyncio.TimeoutError:
            raise IsabelleToolError("Timeout waiting for preview")

    def handle_preview_response(self, uri: str, column: int, label: str, content: str):
        """Called when PIDE/preview_response received"""
        key = f"{uri}:{column}"
        if key in self.preview_futures:
            self.preview_futures[key].set_result({
                "label": label,
                "content": content
            })
```

---

### 3.9 `isabelle_build`

**Implementation Steps:**

1. **Check if build needed:**
   ```bash
   isabelle build -n -b <logic>  # -n = no build, just check
   # Exit code 0 = up to date, non-zero = build needed
   ```

2. **Run build if needed or clean=True:**
   ```bash
   isabelle build -b <logic> [-c] [-d <dir>] [-v]
   # -b = build heap
   # -c = clean build
   # -d = session directory
   # -v = verbose
   ```

3. **Spawn LSP server:**
   ```bash
   isabelle vscode_server -l <logic> [-d <dir>] [-o <option>=<value>]
   ```

4. **Send LSP initialize:**
   ```json
   {
     "jsonrpc": "2.0",
     "id": 1,
     "method": "initialize",
     "params": {
       "processId": null,
       "rootUri": null,
       "capabilities": {}
     }
   }
   ```

5. **Wait for initialize response and send initialized notification**

**Implementation Notes:**
- Build can take 1-10 minutes for large sessions
- Stream build output to user (via build_log field)
- Handle build failures gracefully
- Store server info and capabilities in context

**Code Snippet:**
```python
async def build_session(
    logic: str = "HOL",
    session_dirs: List[str] = [],
    clean: bool = False,
    verbose: bool = False
) -> BuildResult:
    """Build session and start LSP server"""
    build_log = []

    # Check if build needed
    check_cmd = ["isabelle", "build", "-n", "-b", logic]
    for d in session_dirs:
        check_cmd.extend(["-d", d])

    check_result = await asyncio.create_subprocess_exec(
        *check_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await check_result.wait()

    # Build if needed or clean requested
    if check_result.returncode != 0 or clean:
        build_cmd = ["isabelle", "build", "-b", logic]
        if clean:
            build_cmd.append("-c")
        for d in session_dirs:
            build_cmd.extend(["-d", d])
        if verbose:
            build_cmd.append("-v")

        build_process = await asyncio.create_subprocess_exec(
            *build_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        # Stream output
        while True:
            line = await build_process.stdout.readline()
            if not line:
                break
            line_str = line.decode('utf-8').rstrip()
            build_log.append(line_str)

        await build_process.wait()

        if build_process.returncode != 0:
            return BuildResult(
                success=False,
                build_log="\n".join(build_log),
                session_name=logic,
                server_info=None
            )

    # Start LSP server
    client = IsabelleLSPClient(logic=logic, session_dirs=session_dirs)
    await client.start()

    # Get server info
    server_info = {
        "name": "isabelle vscode_server",
        "version": await get_isabelle_version()
    }

    return BuildResult(
        success=True,
        build_log="\n".join(build_log),
        session_name=logic,
        server_info=server_info
    )
```

---

### 3.10 `isabelle_session_info`

**Implementation Notes:**
- Query LSP client state
- No external calls needed
- Return cached information

**Code Snippet:**
```python
def session_info(ctx: Context) -> SessionInfo:
    """Get current session information"""
    client = ctx.request_context.lifespan_context.lsp_client

    if not client:
        raise IsabelleToolError("No active session")

    return SessionInfo(
        logic_name=client.logic,
        isabelle_version=client.isabelle_version,
        capabilities=client.server_capabilities,
        uptime_seconds=int(time.time() - client.start_time)
    )
```

---

## 4. Common Implementation Patterns

### 4.1 Position Conversion

```python
def mcp_to_lsp_position(line: int, column: int) -> Tuple[int, int]:
    """Convert MCP (1-indexed) to LSP (0-indexed)"""
    return (line - 1, column - 1)

def lsp_to_mcp_position(line: int, column: int) -> Tuple[int, int]:
    """Convert LSP (0-indexed) to MCP (1-indexed)"""
    return (line + 1, column + 1)
```

### 4.2 URI Conversion

```python
def file_path_to_uri(file_path: str) -> str:
    """Convert absolute file path to file:// URI"""
    from pathlib import Path
    from urllib.parse import quote

    path = Path(file_path).resolve()
    return f"file://{quote(str(path))}"

def uri_to_file_path(uri: str) -> str:
    """Convert file:// URI to absolute file path"""
    from urllib.parse import unquote

    if not uri.startswith("file://"):
        raise ValueError(f"Invalid file URI: {uri}")

    return unquote(uri[7:])  # Remove "file://"
```

### 4.3 Document State Management

```python
async def ensure_document_open(client: IsabelleLSPClient, file_path: str):
    """Ensure document is open in LSP session"""
    if file_path in client.open_documents:
        return

    # Read file content
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Send didOpen
    await client.notify("textDocument/didOpen", {
        "textDocument": {
            "uri": file_path_to_uri(file_path),
            "languageId": "isabelle",
            "version": 1,
            "text": content
        }
    })

    # Update state
    client.open_documents[file_path] = DocumentState(
        file_path=file_path,
        uri=file_path_to_uri(file_path),
        version=1,
        content=content
    )

    # Wait for initial processing (heuristic)
    await asyncio.sleep(2.0)
```

### 4.4 Error Handling

```python
def check_pide_response(response: Any, operation: str, *, allow_none: bool = False):
    """Validate LSP/PIDE response"""
    if response is None and not allow_none:
        raise IsabelleToolError(f"PIDE timeout during {operation}")

    if isinstance(response, dict) and "error" in response:
        error_msg = response["error"].get("message", "Unknown error")
        error_code = response["error"].get("code", -1)
        raise IsabelleToolError(
            f"PIDE error during {operation}: {error_msg} (code {error_code})"
        )

    return response
```

---

## 5. Testing Strategies

### 5.1 Unit Tests

**Mock LSP Client:**
```python
class MockLSPClient:
    def __init__(self):
        self.requests = []
        self.responses = {}

    async def request(self, method: str, params: Dict) -> Any:
        self.requests.append((method, params))
        return self.responses.get(method, {})

    async def notify(self, method: str, params: Dict):
        self.requests.append((method, params))

# Test hover
@pytest.mark.asyncio
async def test_hover_info():
    client = MockLSPClient()
    client.responses["textDocument/hover"] = {
        "contents": {"value": "nat => nat"},
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}
    }

    result = await hover_info_impl(client, "/path/to/file.thy", 1, 1)

    assert result.symbol == "Suc"
    assert "nat => nat" in result.info
    assert len(client.requests) == 1
    assert client.requests[0][0] == "textDocument/hover"
```

### 5.2 Integration Tests

**Real LSP Server:**
```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_hover_with_real_server():
    # Start real isabelle vscode_server
    client = IsabelleLSPClient(logic="HOL")
    await client.start()

    try:
        # Open test file
        test_file = Path(__file__).parent / "test_data" / "Simple.thy"
        await client.open_document(str(test_file))

        # Query hover
        result = await client.get_hover(str(test_file), 5, 10)

        assert result is not None
        assert "contents" in result

    finally:
        await client.shutdown()
```

---

**Document Status**: Ready for Implementation
**Next Step**: Create README.md with installation and usage instructions
