# Isa-LSP API Design Document

**Version:** 0.1.0
**Date:** 2026-03-07
**Status:** Draft with current implementation notes

## 1. Overview

This document provides API design and implementation guidance for Isa-LSP. The
current server exposes 10 MCP tools. Document editing (`isabelle_edit`),
completion (`isabelle_completions`), and preview (`isabelle_preview`) are design
targets discussed below, not tools currently registered by the server — though
the LSP-client layer already implements completion and preview support.
For high-level specifications, see SPECIFICATION.md. For architecture, see
ARCHITECTURE.md.

---

## 2. LSP Method Mappings

### 2.1 Standard LSP Methods

| MCP Tool | LSP Method | Request Params | Response Fields |
|----------|------------|----------------|-----------------|
| `isabelle_hover` | `textDocument/hover` | `TextDocumentPositionParams` | `Hover` with `contents` and `range` |
| `isabelle_completions` _(design target)_ | `textDocument/completion` | `CompletionParams` | `CompletionList` with `items[]` — not exposed as a tool |
| `isabelle_definition` | `textDocument/definition` | `DefinitionParams` | `Location[]` or `LocationLink[]` |
| `isabelle_local_occurrences` | `textDocument/documentHighlight` | `DocumentHighlightParams` | `DocumentHighlight[]` |
| `isabelle_diagnostics` | (notifications) | - | Cached from `publishDiagnostics` |

### 2.2 Document Editing Methods

| MCP Tool | LSP Method | Flow |
|----------|------------|------|
| `isabelle_edit` | `textDocument/didChange` (Full sync) | Design target only; not implemented in current server |

### 2.3 PIDE Extension Methods

| MCP Tool | PIDE Methods | Flow |
|----------|--------------|------|
| `isabelle_goal` | `PIDE/caret_update`, `PIDE/state_init`, `PIDE/state_output`, `PIDE/state_exit` | Multi-step async; state id assigned by server |
| `isabelle_command_output` | `PIDE/dynamic_output` | Notification-based |
| `isabelle_preview` _(design target)_ | `PIDE/preview_request`, `PIDE/preview_response` | Request-response; not exposed as a tool |

### 2.4 Session Management

| MCP Tool | Implementation | External Commands |
|----------|----------------|-------------------|
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

### 3.2 `isabelle_completions` (Design Target)

> Not exposed as an MCP tool. The LSP-client layer implements `get_completions`;
> the design below is the intended tool surface.

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

### 3.3 `isabelle_definition`

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

### 3.4 `isabelle_local_occurrences`

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

### 3.5 `isabelle_diagnostics`

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

### 3.6 `isabelle_edit` (Design Target)

This section describes a future mutating tool. It is not registered in the
current server.

**LSP Notification (Client → Server):**
```json
{
  "jsonrpc": "2.0",
  "method": "textDocument/didChange",
  "params": {
    "textDocument": {
      "uri": "file:///path/to/file.thy",
      "version": 3
    },
    "contentChanges": [
      {
        "text": "<full new content of the file>"
      }
    ]
  }
}
```

**Server Behavior After didChange:**

| Phase | Delay | Description |
|-------|-------|-------------|
| Input debounce | 100ms (`vscode_input_delay`) | Batches rapid consecutive changes |
| Flush to PIDE | immediate after debounce | Converts pending edits → `Document.Edit_Text` |
| PIDE processing | variable (depends on file size) | Incremental type-checking and proof processing |
| Output debounce | 500ms (`vscode_output_delay`) | Batches diagnostic updates |
| Diagnostics push | after output debounce | `textDocument/publishDiagnostics` notification |

**Implementation Notes:**

1. **Sync Kind**: Isabelle reports `textDocumentSync = 2` (Incremental per LSP spec). However, a client can always send full content replacement (omitting the `range` field in `contentChanges`) even when the server announces Incremental — the LSP spec guarantees this fallback. We use full replacement for simplicity and correctness. The server internally computes diffs via `doc.change()`.

2. **Version Management**: Each `didChange` must increment the version. The tool tracks versions per-document in `DocumentState.version`.

3. **Line-range to full content**: When the user provides `start_line`/`end_line`/`new_text`, the tool computes the full new content from the cached `DocumentState.content`:
   ```python
   def apply_line_edit(content: str, start_line: int, end_line: int, new_text: str) -> str:
       lines = content.splitlines(keepends=True)
       # Ensure last line has newline for consistent splicing
       if lines and not lines[-1].endswith('\n'):
           lines[-1] += '\n'

       # Convert to 0-indexed; end_line is 1-indexed inclusive → exclusive
       start_idx = min(start_line - 1, len(lines))
       end_idx = min(end_line, len(lines))

       # When end_idx < start_idx, this is a pure insert (no lines removed)
       new_lines = new_text.splitlines(keepends=True) if new_text else []

       result_lines = lines[:start_idx] + new_lines + lines[end_idx:]
       return ''.join(result_lines)
   ```

4. **Processing detection**: After sending `didChange`, the tool waits for PIDE to finish by monitoring the diagnostics cache:
   - Poll every 300ms
   - Consider complete when no new `publishDiagnostics` received for 500ms+
   - Timeout after 10 seconds (configurable)

   **Known limitation**: This heuristic can produce false positives (PIDE takes >500ms between diagnostic batches → falsely reports completion) and false negatives (file has zero diagnostics → waits until timeout). A more robust approach would track `PIDE/decoration` notifications with `background_running` status, but this is not implemented.

5. **Disk sync**: When `sync_to_disk=True`, write the new content to the file on disk after the LSP change is sent. This keeps the file system consistent for `git`, other editors, and external tools.

   **Warning**: When `sync_to_disk=False`, the LSP buffer diverges from the file on disk. A subsequent `open_document` call (which reads from disk) would overwrite the in-buffer changes. Only use `sync_to_disk=False` when performing a sequence of edits followed by a single explicit disk write, or when the edit is transient (e.g., command injection for sledgehammer).

6. **Auto-open**: If the file is not yet open in the LSP session, automatically open it via `didOpen` before applying the change.

7. **Cache invalidation**: After an edit, previously cached results from other tools (`isabelle_goal`, `isabelle_hover`, etc.) are stale for the edited document. The tool does not automatically invalidate them — callers must re-query after editing.

8. **Concurrency**: Line-range edits splice against `DocumentState.content`. If two `isabelle_edit` calls to the same document overlap, both will compute their splice against the same base content, and the second `change_document` will silently overwrite the first. Callers must serialize edits to the same document.

**Code Snippet:**
```python
async def edit_document(
    client: IsabelleLSPClient,
    file_path: str,
    new_content: Optional[str] = None,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    new_text: Optional[str] = None,
    sync_to_disk: bool = True,
    wait_for_processing: bool = True,
) -> EditResult:
    """Edit theory file and trigger PIDE reprocessing."""

    # Validate: exactly one edit mode
    has_full = new_content is not None
    has_range = start_line is not None
    if has_full == has_range:
        raise IsabelleToolError(
            "Provide either new_content (full replacement) "
            "or start_line/end_line/new_text (line-range edit), not both"
        )

    # Ensure document is open
    if file_path not in client.open_documents:
        await client.open_document(file_path)

    doc = client.open_documents[file_path]

    # Compute new content
    if has_range:
        final_content = apply_line_edit(
            doc.content, start_line, end_line, new_text or ""
        )
    else:
        final_content = new_content

    # Send didChange
    new_version = await client.change_document(file_path, final_content)

    # Sync to disk
    if sync_to_disk:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(final_content)

    # Wait for PIDE reprocessing
    processing_complete = False
    if wait_for_processing:
        processing_complete = await client.wait_for_processing(
            file_path, timeout=10.0
        )

    # Collect ALL diagnostics for the document (not just the edited region)
    diagnostics = client.get_cached_diagnostics(file_path)
    diag_items = [parse_diagnostic(d) for d in diagnostics]

    # success = no errors anywhere in the document
    success = all(d.severity != "error" for d in diag_items)

    return EditResult(
        success=success,
        version=new_version,
        content_length=len(final_content),
        diagnostics=diag_items,
        processing_complete=processing_complete,
    )
```

**Edge Cases:**
- Empty `new_text` with valid range → deletes the specified lines
- `start_line > end_line` → pure insertion before `start_line` (no lines removed)
- `start_line` beyond file length → append at end of file
- File modified externally since last `didOpen` → the cached content diverges from disk; the tool uses cached content as the base for line-range edits. Use `new_content` for full replacement when in doubt.
- PIDE timeout → return `processing_complete=false` with whatever diagnostics are available
- Unicode recoding → server may send `workspace/applyEdit` to normalize Isabelle symbols; currently not handled (logged only)

---

### 3.7 `isabelle_goal`

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
For before/after mode, it performs the sequence twice: once at line start and
once at line end.

```
1. Send: PIDE/caret_update
   {"uri": "file:///...", "line": 41, "character": <end_of_line>}

2. Send: PIDE/state_init

3. Receive: PIDE/state_output
   {"id": <panel_id>, "content": "<html>...goals...</html>"}

4. Send: PIDE/state_exit
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
1. **Panel ID Management**: `PIDE/state_init` has no client-supplied id.
   Learn the server-assigned id from the first `PIDE/state_output` and use it
   for `PIDE/state_exit`.
2. **Async Coordination**: Use `asyncio.Future` for waiting on `state_output`
3. **Timeout**: 5-10 seconds max wait for state output
4. **Before/After Pattern**: If column is None, query twice (line start and end)
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

### 3.8 `isabelle_command_output`

**PIDE Notification (Server → Client):**
```json
{
  "jsonrpc": "2.0",
  "method": "PIDE/dynamic_output",
  "params": {
    "content": "<pre class=\"source\"><span class=\"writeln_message\">val it = 64: int</span></pre>"
  }
}
```

**Implementation Notes:**
1. **Notification Based**: `dynamic_output` is sent after caret movement, but
   the notification payload contains only `content`.
2. The current client serializes dynamic-output queries so a notification can
   be associated with the currently requested position.
3. The client does not reuse output from a different file/line/column. If
   Isabelle emits no fresh notification and no same-position cache exists, the
   command output result contains an empty message list.
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

### 3.9 `isabelle_preview` (Design Target)

> Not exposed as an MCP tool. The LSP-client layer implements `request_preview`;
> the design below is the intended tool surface.

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

### 3.10 `isabelle_session_info`

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

    return SessionInfo(current_session=client.logic)
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

### 4.4 Document Change Management

**Protocol context**: Isabelle's `vscode_server` reports `textDocumentSync = 2` (Incremental per LSP spec). However, the LSP spec guarantees that a client can always send full content replacement (omitting `range` in `contentChanges`) regardless of the announced sync kind. We use this full-replacement approach for simplicity.

```python
async def change_document(self, file_path: str, new_content: str) -> int:
    """Send textDocument/didChange with full content replacement.

    The server debounces input (100ms vscode_input_delay), flushes to PIDE,
    and pushes updated diagnostics (debounced 500ms vscode_output_delay).

    Args:
        file_path: Must be already open (call ensure_document_open first)
        new_content: Complete new file content

    Returns:
        New document version number
    """
    doc = self.open_documents[file_path]
    doc.version += 1
    doc.content = new_content

    await self.notify("textDocument/didChange", {
        "textDocument": {
            "uri": doc.uri,
            "version": doc.version,
        },
        "contentChanges": [{"text": new_content}],
    })

    return doc.version


async def wait_for_processing(self, file_path: str, timeout: float = 10.0) -> bool:
    """Wait for PIDE to finish reprocessing after a change.

    Heuristic: consider complete when no publishDiagnostics received
    for 500ms+ (matching vscode_output_delay).

    Returns:
        True if processing completed within timeout

    Known limitations:
        - False positive: if PIDE takes >500ms between diagnostic batches
          (e.g., processing a large proof), we falsely report completion
          before the second batch arrives.
        - False negative: if the file has zero diagnostics after the edit,
          no publishDiagnostics is sent at all, so we wait until timeout.
        - A more robust approach would track PIDE/decoration notifications
          with background_running status, but this is not yet implemented.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(0.3)
        if self.is_processing_complete(file_path):
            return True
    return False
```

**Timing Considerations:**

After `didChange`, there are two debounce delays before the client sees results:
- `vscode_input_delay` (100ms): server batches rapid edits before flushing to PIDE
- `vscode_output_delay` (500ms): server batches diagnostic updates before pushing

So the minimum round-trip for a single edit is ~600ms + PIDE processing time.
For small edits to already-loaded theories, total round-trip is typically 1-2 seconds.
For large files or complex proofs, it can take 5-10+ seconds.

### 4.5 Error Handling

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
