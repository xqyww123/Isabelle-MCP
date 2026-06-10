# Isabelle-MCP API Design Document

**Version:** 0.1.0
**Date:** 2026-06-04
**Status:** Draft with current implementation notes

> Note: this is a design/protocol document; several code blocks are illustrative
> pseudocode, not verbatim current source. Where a section describes *current*
> behavior it has been reconciled with the implementation.
> Position-sensitive tools target by `symbol`/`after_text`
> snippet, never a column.

## 1. Overview

This document provides API design and implementation guidance for Isabelle-MCP. The
current server exposes 11 MCP tools.
For high-level specifications, see SPECIFICATION.md. For architecture, see
ARCHITECTURE.md.

---

## 2. LSP Method Mappings

### 2.1 Standard LSP Methods

| MCP Tool | LSP Method | Request Params | Response Fields |
|----------|------------|----------------|-----------------|
| `isabelle_hover` | `textDocument/hover` | `TextDocumentPositionParams` | `Hover` with `contents` and `range` |
| `isabelle_definition` | `textDocument/definition` | `DefinitionParams` | `Location[]` or `LocationLink[]` |
| `isabelle_local_occurrences` | `textDocument/documentHighlight` | `DocumentHighlightParams` | `DocumentHighlight[]` |

> The `publishDiagnostics` notification is still cached internally (consumed by
> `isabelle_hover` to attach line diagnostics), but there is no longer a dedicated
> `isabelle_diagnostics` tool. Error/warning *message text* is obtained via
> `isabelle_command_output`; error/warning *line locations* come from the evaluation
> snapshot (decoration channels). See §3.4.

### 2.2 PIDE Extension Methods

| MCP Tool | PIDE Methods | Flow |
|----------|--------------|------|
| `isabelle_goal` | `PIDE/caret_update`, `PIDE/state_init`, `PIDE/state_output`, `PIDE/state_exit` | Multi-step async; state id assigned by server |
| `isabelle_command_output` | `PIDE/output_at_position` (patched, position-explicit) | Request-response; returns the enclosing command's source+range and rendered output in one shot |

### 2.3 Session Management

| MCP Tool | Implementation | External Commands |
|----------|----------------|-------------------|
| `isabelle_launch` | Spawn the prover; set logic + `-d` session dirs | `isabelle vscode_server -l <session> -d <dirs…>` |
| `isabelle_terminate` | LSP `shutdown`/`exit` + process teardown | - |
| `isabelle_session_info` | Query LSP client state | - |

---

## 3. Tool Implementation Details

### 3.1 `isabelle_hover`

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
- Symbol not found on the line → return empty `results` list
- No hover info for an occurrence → skip that occurrence
- Identical hover content for several occurrences → grouped into one `HoverEntry`

**Code Snippet:**
```python
async def hover_info(ctx, file_path, line, symbol):
    client = get_lsp_client(ctx)
    await ensure_document_open(client, file_path)

    # Locate each occurrence of `symbol` on the line (1-indexed columns)
    line_text = get_line(file_path, line)
    columns = find_symbol_columns(line_text, symbol)  # 1-indexed

    if not columns:
        return HoverInfo(symbol=symbol, results=[], line_context=line_text)

    # Query hover for each occurrence, grouping by identical content
    by_info: dict[str, HoverEntry] = {}
    for occ, col in enumerate(columns, start=1):
        response = await client.request("textDocument/hover", {
            "textDocument": {"uri": file_path_to_uri(file_path)},
            "position": {"line": line - 1, "character": col - 1}  # LSP is 0-indexed
        })
        check_pide_response(response, "hover", allow_none=True)
        if not response or "contents" not in response:
            continue
        contents = response["contents"]
        info_text = contents.get("value", "") if isinstance(contents, dict) else str(contents)
        entry = by_info.get(info_text)
        if entry is None:
            entry = HoverEntry(info=info_text, occurrences=[], columns=[])
            by_info[info_text] = entry
        entry.occurrences.append(occ)
        entry.columns.append(col)

    return HoverInfo(
        symbol=symbol,
        results=list(by_info.values()),
        line_context=line_text,
        diagnostics=[]
    )
```

---

### 3.2 `isabelle_definition`

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
1. The MCP tool takes a `symbol` (text on the line), not a column; locate its
   occurrence on the line and issue the LSP request at that column
2. LSP can return single `Location` or `Location[]`
3. Normalize to always return list
4. Convert URIs back to file paths
5. Convert positions to 1-indexed

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

async def declaration_location(ctx, file_path, line, symbol):
    client = get_lsp_client(ctx)
    await ensure_document_open(client, file_path)

    # Locate the symbol's first occurrence on the line (1-indexed column)
    line_text = get_line(file_path, line)
    col = find_symbol_columns(line_text, symbol)[0]  # 1-indexed

    response = await client.request("textDocument/definition", {
        "textDocument": {"uri": file_path_to_uri(file_path)},
        "position": {"line": line - 1, "character": col - 1}
    })

    locations = normalize_definition_response(response)

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

### 3.3 `isabelle_local_occurrences`

The MCP tool takes a `symbol` (text on the line), not a column; the server locates
the symbol's occurrence(s) on the line and issues the LSP request below at each.

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
      "kind": 1
    }
  ]
}
```

**DocumentHighlightKind (LSP Enum):** The LSP spec defines 1=Text, 2=Read,
3=Write, but Isabelle's `vscode_server` hardcodes every result to **kind=1
(Text)** — the `read`/`write` constructors exist in its source but are never
called. The underlying def/ref distinction is therefore unavailable, so the MCP
tool **omits the `kind` field** entirely.

**Implementation Notes:**
1. Convert ranges to 1-indexed positions; merge/dedup occurrences from each query
2. Semantics are entity-occurrence based: a position highlights only when the
   entity's **definition** is present in the current file's markup. References to
   global constants from imported theories, and plain free/bound variables,
   resolve to nothing.

**Edge Cases:**
- Symbol not found on the line → error
- No occurrences (global/free-var) → return `occurrences=[]`
- Entity occurs once → single-item list

---

### 3.4 Diagnostic cache (internal — no longer a tool)

There is no `isabelle_diagnostics` MCP tool. The `publishDiagnostics` notification
handler and the diagnostic cache (`DiagnosticMessage` model) still exist, but are
**internal only**: `isabelle_hover` reads them to attach the line's diagnostics to a
hover result. The two agent-facing paths to error/warning information are:

- **Where** (line locations) → the evaluation snapshot (§4.4 in `SPECIFICATION.md`),
  built from the `PIDE/decoration` channels (`text_overview_error` +
  `background_bad` for errors, `text_overview_warning` for warnings) — not from the
  diagnostics channel.
- **What** (full message text) → `isabelle_command_output` at the offending line.

**LSP Notification (Server → Client), consumed internally:**
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

**Diagnostic Severity (LSP Enum):** 1 = Error, 2 = Warning, 3 = Information, 4 = Hint.

**Implementation Notes:**
1. Diagnostics arrive via async notifications and are cached by file URI.
2. The handler stores them; `isabelle_hover` reads the cache synchronously to attach
   the queried line's diagnostics (`DiagnosticMessage`) to its result.
3. The cache is no longer exposed through a standalone tool, so there is no
   `start_line`/`end_line` filter, `success` flag, or `DiagnosticsResult` model.

---

### 3.5 `isabelle_goal`

**PIDE Flow:**

```
1. Send: PIDE/caret_update
   {"uri": "file:///...", "line": 41, "character": 0}
   ← Updates Isabelle's current caret position

2. Send: PIDE/state_init
   ← No immediate response. Isabelle creates a state panel internally.

3. Receive: PIDE/state_output
   {"id": <panel_id>, "content": "<html>...goals...</html>", "auto_update": true}

4. Send: PIDE/state_exit
   {"id": <panel_id>}
```

The current implementation opens one temporary state panel per queried position.
It performs the sequence **once**, at a single caret resolved from the optional
`after_text` snippet (or the end of the line when `after_text` is omitted) via
`resolve_caret`. There is no "before/after mode" and no `column` parameter. The
tool calls `get_command_at_position` (for the enclosing command's source+range) and
`get_goals_at_position` (for the subgoals after that command), and returns a
`GoalState(command=CommandSpan|None, subgoals=list[str], note=str|None)`. To compare
a tactic's before/after effect, query the line before it and the tactic's own line.

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
1. **Panel ID Management**: `PIDE/state_init` has no client-supplied id.
   Learn the server-assigned id from the first `PIDE/state_output` and use it
   for `PIDE/state_exit`.
2. **Async Coordination**: Use `asyncio.Future` for waiting on `state_output`
3. **Timeout**: 5-10 seconds max wait for state output
4. **Single query**: The caret is resolved once from `after_text` (or end of line);
   there is no before/after double query and no `column` parameter.
5. **Context Extraction**: Parse `<div class="context">` if available
6. **Concurrency**: Serialize state queries because `PIDE/state_init` responses
   are matched by the next state output notification.

**Edge Cases:**
- No proof state available → return empty goals
- Timeout waiting for state_output → raise error
- Panel creation fails → retry once
- HTML parsing errors → return raw text as single goal

**Code Snippet:**
```python
class StatePanelManager:
    def __init__(self):
        self.state_lock = asyncio.Lock()
        self.init_waiters: list[asyncio.Future[tuple[int, str]]] = []

    async def query_position(
        self,
        client: IsabelleLSPClient,
        file_path: str,
        line: int,
        column: int,
    ) -> list[str]:
        """Query goals at one LSP position."""
        uri = file_path_to_uri(file_path)

        async with self.state_lock:
            future = asyncio.Future()
            self.init_waiters.append(future)
            panel_id = None

            try:
                await client.notify("PIDE/caret_update", {
                    "uri": uri,
                    "line": line - 1,
                    "character": column,
                })
                await client.notify("PIDE/state_init", {})

                panel_id, html_output = await asyncio.wait_for(future, timeout=5.0)
                return parse_goals_from_html(html_output)
            finally:
                if panel_id is not None:
                    await client.notify("PIDE/state_exit", {"id": panel_id})

    def handle_state_output(self, panel_id: int, html_content: str):
        """Called by LSP client when PIDE/state_output received"""
        if self.init_waiters:
            self.init_waiters.pop(0).set_result((panel_id, html_content))
```

---

### 3.6 `isabelle_command_output`

**Current mechanism — `PIDE/output_at_position` (patched, position-explicit).**
The caret is resolved once from the optional `after_text` snippet (or end of line)
via `resolve_caret`, then a single `PIDE/output_at_position` request returns the
enclosing command's **source + range AND its rendered output** in one shot. Unlike
the older push-based `dynamic_output`, it does not move the caret, so it is immune to
the "same caret → no push" hang and renders the whole command's results regardless of
the offset within the command. The tool returns
`CommandOutputResult(command=CommandSpan|None, messages=list[OutputMessage], note=str|None)`,
which the MCP layer renders to a plain-text `ToolResult` (`format_command_output`).

**Request/response shape:**
```json
// Request
{"jsonrpc": "2.0", "id": 7, "method": "PIDE/output_at_position",
 "params": {"uri": "file:///...", "line": 40, "character": 2}}
// Response result: the command source, its 1-indexed range, and rendered output HTML,
// e.g. "<pre class=\"source\"><span class=\"writeln_message\">val it = 64: int</span></pre>"
```

**Implementation Notes:**
1. **Request-response, position-explicit**: one request per resolved position;
   no notification matching, no caret movement.
2. **No command at the position** (blank line, comment, or past the last command)
   → `command=None`, empty `messages`.
3. The legacy `PIDE/dynamic_output` push path still exists in the client
   (`get_dynamic_output`) but is not used by this tool.
4. `isabelle_command_output` probes a small set of caret columns on the line:
   first non-space character, end of the command token, the following
   character, then column 0. This matches Isabelle output that appears only
   when the caret is inside a command body.
5. Parse HTML to extract message type and text. Isabelle2024 commonly emits
   message spans such as `writeln_message`, `error_message`, and
   `state_message`; older/simple examples may use `writeln`, `warning`, or
   `error` classes directly.

**Output Types:**
- `writeln`: Normal prover output
- `warning`: Warnings
- `error`: Errors
- `information`: Info messages

**Parsing Strategy:**
```python
def parse_dynamic_output(html: str) -> List[OutputMessage]:
    """Parse PIDE dynamic output HTML"""
    # Use an HTML parser, not regex: Isabelle output is nested markup such as
    # <pre class="source"><span class="writeln_message">...</span></pre>.
    # Recognized CSS classes:
    # - writeln, writeln_message, tracing, tracing_message -> writeln
    # - warning, warning_message -> warning
    # - error, error_message -> error
    # - information, information_message, state_message -> information
    ...
```

**Edge Cases:**
- No output at line → return empty messages
- Empty PIDE payload such as `<pre class="source"/>` → return empty messages
- Empty lines and pure comment lines return empty messages without querying PIDE
- Unrecognized HTML classes → ignore them
- Multiple commands on one line are position-sensitive; the line-only tool
  returns the first recognized output from its caret probes

---

### 3.7 `isabelle_session_info`

**Implementation Notes:**
- Query the current LSP client session name
- No external calls needed
- Does not enumerate available Isabelle sessions

**Code Snippet:**
```python
def session_info(ctx: Context) -> SessionInfo:
    """Get current session information"""
    client = ctx.request_context.lifespan_context.lsp_client

    if not client:
        raise IsabelleToolError("No active session")

    return SessionInfo(current_session=client.logic, version=client.isabelle_version or None)
```

---

### 3.8 `isabelle_launch`

**Implementation Notes:**
- Must be called before any evaluation/query tool — the prover does not auto-start.
- Serializes under the evaluation-state lock; idempotent for the same session, and
  restarts (shutdown → start) when a different session is requested.
- Sets `client.session_dirs` (default `[cwd]` → `-d $cwd`) and `client.logic` before
  `client.start()`.

**Code Snippet:**
```python
async def isabelle_launch(session: str, session_dirs: list[str] | None = None) -> SessionInfo:
    async with _evaluation_state_lock:
        if client.process is not None:
            if client.logic == session:
                return await session_info(client)
            await client.shutdown()
            client.process = None
        client.session_dirs = session_dirs if session_dirs is not None else [os.path.realpath(os.getcwd())]
        client.logic = session
        await client.start()
        return await session_info(client)
```

---

### 3.9 `isabelle_terminate`

**Implementation Notes:**
- Tears the prover down (`shutdown()` then `client.process = None`) and clears the
  FileWatcher's directory watches; the MCP server process stays alive for a relaunch.
- `shutdown()` also resets the global `evaluation_state`, so the next launch starts clean.

**Code Snippet:**
```python
async def isabelle_terminate() -> ToolResult:
    if client is None or client.process is None:
        return text_result("No Isabelle session is running.")
    async with _evaluation_state_lock:
        await client.shutdown()
        client.process = None
        file_watcher.clear_watches()
    return text_result("Isabelle session terminated.")
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

> The fixed `asyncio.sleep(2.0)` above is illustrative only. The current
> `open_document` does not hardcode a delay; processing is awaited dynamically via
> the `ProcessingTracker` (`wait_for_processing` / `wait_for_processing_bounded`).

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

    result = await hover_info_impl(client, "/path/to/file.thy", 1, "Suc")

    assert result.symbol == "Suc"
    assert "nat => nat" in result.results[0].info
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
