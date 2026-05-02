"""LSP client for Isabelle vscode_server — JSON-RPC 2.0 over stdin/stdout."""

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar

from isa_lsp.utils import (
    IsabelleToolError,
    file_path_to_uri,
    parse_goals_from_html,
    uri_to_file_path,
)

logger = logging.getLogger(__name__)

JsonDict = dict[str, Any]


@dataclass
class DocumentState:
    file_path: str
    uri: str
    version: int
    content: str
    language_id: str = "isabelle"


@dataclass
class DiagnosticCache:
    diagnostics: dict[str, list[dict]] = field(default_factory=dict)
    last_update: dict[str, float] = field(default_factory=dict)


class IsabelleLSPClient:
    """Manages the lifecycle of `isabelle vscode_server` and JSON-RPC 2.0 communication."""

    STALL_TIMEOUT: ClassVar[float] = 120.0
    PROGRESS_CHECK_INTERVAL: ClassVar[float] = 5.0
    STATE_OUTPUT_GRACE: ClassVar[float] = 10.0

    def __init__(
        self,
        logic: str = "HOL",
        session_dirs: list[str] | None = None,
        verbose: bool = False,
    ):
        self.logic = logic
        self.session_dirs = session_dirs or []
        self.verbose = verbose

        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

        self.request_id = 0
        self.pending_requests: dict[int, asyncio.Future[Any]] = {}

        self.open_documents: dict[str, DocumentState] = {}
        self.diagnostic_cache = DiagnosticCache()
        self._first_diagnostic_event: dict[str, asyncio.Event] = {}

        # Caret lock: serializes the entire goal/dynamic_output query cycle.
        # The Isabelle caret is global — see docs/ARCHITECTURE.md §7.3.
        self._caret_lock = asyncio.Lock()
        self._state_init_waiters: list[asyncio.Future[tuple[int, str]]] = []

        # PIDE dynamic output
        self._dynamic_output_waiters: list[tuple[tuple[str, int, int], asyncio.Future[str]]] = []
        self._dynamic_output_cache_by_position: dict[tuple[str, int, int], str] = {}

        # PIDE preview
        self._preview_waiters: dict[tuple[str, int], asyncio.Future[JsonDict]] = {}

        # Server activity tracking for progress monitoring
        self._last_server_activity: float = 0.0

        self.server_capabilities: dict[str, Any] = {}
        self.isabelle_version: str = ""
        self.start_time: float = 0.0

    # ── Progress monitoring ────────────────────────────────────────────

    async def _wait_with_progress(
        self,
        future: asyncio.Future[Any],
        stall_timeout: float | None = None,
    ) -> Any:
        """Wait for a future, raising IsabelleToolError if Isabelle stalls or crashes.

        Progress is detected by any incoming server message. If no message
        arrives for stall_timeout seconds, assumes Isabelle is stuck.
        """
        if stall_timeout is None:
            stall_timeout = self.STALL_TIMEOUT
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), timeout=self.PROGRESS_CHECK_INTERVAL,
                )
            except asyncio.TimeoutError:
                if future.done():
                    return future.result()
                self._check_server_health(stall_timeout)

    def _check_server_health(self, stall_timeout: float) -> None:
        """Raise IsabelleToolError if the Isabelle process appears dead or stalled."""
        if self.process is not None and self.process.returncode is not None:
            raise IsabelleToolError(
                f"Isabelle process died (exit code {self.process.returncode})"
            )
        if self._last_server_activity > 0:
            elapsed = time.time() - self._last_server_activity
            if elapsed > stall_timeout:
                raise IsabelleToolError(
                    f"Isabelle appears stalled — no server activity for {elapsed:.0f}s"
                )

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        cmd = [
            "isabelle", "vscode_server", "-l", self.logic,
            "-o", "vscode_pide_extensions",
            "-o", "vscode_unicode_symbols",
            "-o", "vscode_caret_perspective=1",
        ]
        for d in self.session_dirs:
            cmd.extend(["-d", d])
        if self.verbose:
            cmd.append("-v")

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise IsabelleToolError(
                "isabelle command not found. Is Isabelle installed and in PATH?"
            ) from exc

        self.start_time = time.time()
        self._last_server_activity = self.start_time
        self.reader_task = asyncio.create_task(self._read_loop())
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        await self.initialize()

    async def initialize(self) -> dict[str, Any]:
        response = await self.request("initialize", {
            "processId": None,
            "rootUri": None,
            "capabilities": {},
        }, timeout=30.0)
        result = response if isinstance(response, dict) else {}
        if result:
            self.server_capabilities = result.get("capabilities", {})
            self.isabelle_version = result.get("serverInfo", {}).get("version", "unknown")
        await self.notify("initialized", {})
        return result

    async def shutdown(self) -> None:
        if self.process and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.request("shutdown", {}, timeout=5.0), timeout=5.0)
            except (asyncio.TimeoutError, IsabelleToolError):
                pass
            with contextlib.suppress(IsabelleToolError):
                await self.notify("exit", {})
            await self._cancel_background_tasks()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

        self.open_documents.clear()
        self.pending_requests.clear()
        self.diagnostic_cache.diagnostics.clear()
        self.diagnostic_cache.last_update.clear()
        self._first_diagnostic_event.clear()
        self._state_init_waiters.clear()
        self._dynamic_output_waiters.clear()
        self._dynamic_output_cache_by_position.clear()
        self._preview_waiters.clear()

    # ── JSON-RPC transport ──────────────────────────────────────────────

    async def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        """Send an LSP request and wait for the response.

        When timeout is None (default), uses progress monitoring — no fixed
        timeout, but raises IsabelleToolError if the server stalls or crashes.
        When timeout is set, uses a hard deadline (for lifecycle methods like
        initialize/shutdown).
        """
        self.request_id += 1
        req_id = self.request_id
        message = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = future

        try:
            await self._send(message)
        except Exception:
            self.pending_requests.pop(req_id, None)
            raise

        try:
            if timeout is not None:
                return await asyncio.wait_for(future, timeout=timeout)
            return await self._wait_with_progress(future)
        except asyncio.TimeoutError as exc:
            raise IsabelleToolError(f"LSP request '{method}' timed out after {timeout}s") from exc
        finally:
            self.pending_requests.pop(req_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: JsonDict) -> None:
        if not self.process or not self.process.stdin:
            raise IsabelleToolError("LSP process not running")
        content = json.dumps(message).encode('utf-8')
        header = f"Content-Length: {len(content)}\r\n\r\n".encode('ascii')
        async with self._write_lock:
            try:
                self.process.stdin.write(header + content)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                raise IsabelleToolError("Failed to write to LSP process") from exc

    # ── Background readers ──────────────────────────────────────────────

    async def _read_loop(self) -> None:
        try:
            while True:
                if not self.process or not self.process.stdout:
                    break
                message = await self._read_message()
                if message is None:
                    break
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Read loop failed: {e}", exc_info=True)
            self._fail_pending_waiters(IsabelleToolError(f"LSP read loop failed: {e}"))

    async def _read_message(self) -> JsonDict | None:
        if not self.process or not self.process.stdout:
            return None
        headers: dict[str, str] = {}
        while True:
            header_line = await self.process.stdout.readline()
            if not header_line:
                return None
            line = header_line.decode("ascii", errors="replace").strip()
            if not line:
                break
            name, sep, value = line.partition(":")
            if sep:
                headers[name.lower()] = value.strip()

        raw_length = headers.get("content-length")
        if raw_length is None:
            return {}
        try:
            content_length = int(raw_length)
        except ValueError:
            return {}

        content = await self.process.stdout.readexactly(content_length)
        try:
            message = json.loads(content.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return message if isinstance(message, dict) else {}

    async def _drain_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                logger.debug("isabelle stderr: %s", line.decode("utf-8", errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _cancel_background_tasks(self) -> None:
        tasks = [
            t for t in (self.reader_task, self.stderr_task)
            if t is not None
        ]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self.reader_task = None
        self.stderr_task = None

    # ── Message dispatch ────────────────────────────────────────────────

    async def _handle_message(self, message: JsonDict) -> None:
        self._last_server_activity = time.time()
        method = message.get("method", "")
        msg_id = message.get("id", "")
        if method:
            logger.debug("← notification: %s", method)
        elif msg_id:
            logger.debug("← response id=%s", msg_id)

        if "id" in message and message["id"] in self.pending_requests:
            req_id = message["id"]
            future = self.pending_requests.pop(req_id)
            if "result" in message:
                future.set_result(message["result"])
            elif "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    error_message = error.get('message', 'Unknown')
                else:
                    error_message = str(error)
                future.set_exception(
                    IsabelleToolError(f"LSP error: {error_message}")
                )
            else:
                future.set_exception(
                    IsabelleToolError("LSP response missing result/error")
                )
        elif "method" in message:
            await self._handle_notification(message["method"], message.get("params", {}))

    async def _handle_notification(self, method: str, params: Any) -> None:
        if method == "textDocument/publishDiagnostics":
            if not isinstance(params, dict):
                return
            uri = params.get("uri", "")
            if not isinstance(uri, str) or not uri.startswith("file://"):
                return
            diagnostics = params.get("diagnostics", [])
            if not isinstance(diagnostics, list):
                diagnostics = []
            file_path = uri_to_file_path(uri)
            self.diagnostic_cache.diagnostics[file_path] = diagnostics
            self.diagnostic_cache.last_update[file_path] = time.time()
            event = self._first_diagnostic_event.get(file_path)
            if event is not None and not event.is_set():
                event.set()
        elif method == "PIDE/state_output":
            self._handle_state_output(params)
        elif method == "PIDE/dynamic_output":
            self._handle_dynamic_output(params)
        elif method == "PIDE/preview_response":
            self._handle_preview_response(params)

    def _handle_state_output(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        raw_panel_id = params.get("id")
        html = str(params.get("content", params.get("output", "")))
        if not isinstance(raw_panel_id, int):
            return

        if self._state_init_waiters:
            init_future = self._state_init_waiters.pop(0)
            if not init_future.done():
                init_future.set_result((raw_panel_id, html))

    def _handle_dynamic_output(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        html = str(params.get("content", ""))
        waiters = self._dynamic_output_waiters
        self._dynamic_output_waiters = []
        for key, future in waiters:
            self._dynamic_output_cache_by_position[key] = html
            if not future.done():
                future.set_result(html)

    def _handle_preview_response(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        uri = str(params.get("uri", ""))
        column = params.get("column", 0)
        if not isinstance(column, int):
            column = 0
        future = self._preview_waiters.pop((uri, column), None)
        if future and not future.done():
            future.set_result(params)

    def _fail_pending_waiters(self, exc: Exception) -> None:
        for waiters in (
            self.pending_requests.values(),
            self._state_init_waiters,
            (future for _, future in self._dynamic_output_waiters),
            self._preview_waiters.values(),
        ):
            for future in list(waiters):
                if not future.done():
                    future.set_exception(exc)
        self.pending_requests.clear()
        self._state_init_waiters.clear()
        self._dynamic_output_waiters.clear()
        self._dynamic_output_cache_by_position.clear()
        self._preview_waiters.clear()

    # ── High-level document methods ─────────────────────────────────────

    async def open_document(
        self,
        file_path: str,
        content: str | None = None,
        *,
        wait_for_diagnostics: bool = True,
        diagnostic_timeout: float = 2.0,
    ) -> None:
        if content is None:
            with open(file_path, encoding='utf-8') as f:
                content = f.read()

        existing = self.open_documents.get(file_path)
        if existing is not None:
            if existing.content == content:
                logger.info("Document unchanged: %s", file_path)
                return
            existing.version += 1
            existing.content = content
            logger.info("Sending didChange v%d for %s", existing.version, file_path)
            await self.notify("textDocument/didChange", {
                "textDocument": {"uri": existing.uri, "version": existing.version},
                "contentChanges": [{"text": content}],
            })
            return

        uri = file_path_to_uri(file_path)

        event = asyncio.Event()
        self._first_diagnostic_event[file_path] = event

        await self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "isabelle",
                "version": 1,
                "text": content,
            }
        })
        self.open_documents[file_path] = DocumentState(
            file_path=file_path, uri=uri, version=1, content=content,
        )

        if wait_for_diagnostics:
            received = await self.wait_for_first_diagnostics(
                file_path,
                timeout=diagnostic_timeout,
            )
            if not received:
                logger.debug(
                    "No diagnostics received for %s within %.1fs",
                    file_path,
                    diagnostic_timeout,
                )

    async def wait_for_first_diagnostics(self, file_path: str, timeout: float = 2.0) -> bool:
        if file_path in self.diagnostic_cache.last_update:
            return True

        event = self._first_diagnostic_event.get(file_path)
        if event is None:
            event = asyncio.Event()
            self._first_diagnostic_event[file_path] = event

        if event.is_set():
            return True

        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            return False
        return True

    async def set_caret(self, file_path: str, line: int, character: int = 0) -> None:
        """Send PIDE/caret_update to tell Isabelle which region to process.

        Line and character are 0-indexed (LSP convention).
        """
        doc = self.open_documents.get(file_path)
        if doc is None:
            return
        await self.notify("PIDE/caret_update", {
            "uri": doc.uri,
            "line": line,
            "character": character,
            "focus": True,
        })

    async def close_document(self, file_path: str) -> None:
        doc = self.open_documents.pop(file_path, None)
        if doc is None:
            return
        await self.notify("textDocument/didClose", {"textDocument": {"uri": doc.uri}})
        self.diagnostic_cache.diagnostics.pop(file_path, None)
        self.diagnostic_cache.last_update.pop(file_path, None)
        self._first_diagnostic_event.pop(file_path, None)

    async def sync_dirty_files(self, dirty_paths: set[str]) -> None:
        """Re-sync open documents that are affected by dirty files.

        .thy files: re-read and didChange if content differs.
        .ML files: force re-sync all open .thy files (unknown dependency graph).
        """
        has_dirty_ml = any(p.endswith(".ML") for p in dirty_paths)
        thy_to_resync: set[str] = set()

        for path in dirty_paths:
            if path in self.open_documents:
                thy_to_resync.add(path)

        if has_dirty_ml:
            thy_to_resync.update(self.open_documents.keys())

        for path in thy_to_resync:
            doc = self.open_documents.get(path)
            if doc is None:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue
            if content != doc.content:
                doc.version += 1
                doc.content = content
                logger.info("Syncing dirty file: %s v%d", path, doc.version)
                await self.notify("textDocument/didChange", {
                    "textDocument": {"uri": doc.uri, "version": doc.version},
                    "contentChanges": [{"text": content}],
                })
            elif has_dirty_ml:
                doc.version += 1
                logger.info("Forcing re-sync (ML changed): %s v%d", path, doc.version)
                await self.notify("textDocument/didChange", {
                    "textDocument": {"uri": doc.uri, "version": doc.version},
                    "contentChanges": [{"text": doc.content}],
                })

    # ── Standard LSP queries ────────────────────────────────────────────

    async def get_hover(self, file_path: str, line: int, character: int) -> JsonDict | None:
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")
        result = await self.request("textDocument/hover", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })
        return result if isinstance(result, dict) or result is None else None

    async def get_completions(
        self,
        file_path: str,
        line: int,
        character: int,
    ) -> JsonDict | list[JsonDict] | None:
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")
        result = await self.request("textDocument/completion", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return result if isinstance(result, dict) or result is None else None

    async def get_definition(self, file_path: str, line: int, character: int) -> Any | None:
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")
        return await self.request("textDocument/definition", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })

    async def get_highlights(self, file_path: str, line: int, character: int) -> list[JsonDict] | None:
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")
        result = await self.request("textDocument/documentHighlight", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return None

    def get_cached_diagnostics(self, file_path: str) -> list[dict]:
        return self.diagnostic_cache.diagnostics.get(file_path, [])

    def diagnostics_settled(self, file_path: str, settle_time: float = 1.0) -> bool:
        """True when no new publishDiagnostics arrived in the last *settle_time* seconds."""
        last = self.diagnostic_cache.last_update.get(file_path)
        if last is None:
            return False
        return (time.time() - last) > settle_time

    # ── PIDE extension queries ──────────────────────────────────────────

    async def get_goals_at_position(
        self, file_path: str, line: int, character: int,
    ) -> list[str]:
        """Get proof goals at a position using PIDE state panels.

        Terminal proof commands (``by``, ``done``, ``qed``) produce empty
        proof state — Isabelle's state panel sends no ``state_output`` for
        them.  We detect this via STATE_OUTPUT_GRACE: if the server stays
        active but no output arrives within that window, return ``[]``.
        """
        uri = file_path_to_uri(file_path)
        panel_id: int | None = None

        init_future: asyncio.Future[tuple[int, str]] = (
            asyncio.get_running_loop().create_future()
        )

        try:
            async with self._caret_lock:
                await self.notify("PIDE/caret_update", {
                    "uri": uri, "line": line, "character": character, "focus": True,
                })
                await asyncio.sleep(0.15)

                self._state_init_waiters.append(init_future)
                await self.notify("PIDE/state_init", {})

                try:
                    result = await self._wait_for_state_output(
                        init_future, file_path,
                    )
                except IsabelleToolError:
                    with contextlib.suppress(ValueError):
                        self._state_init_waiters.remove(init_future)
                    raise self._enrich_timeout_error(file_path)

            if result is None:
                return []
            panel_id, html = result
            return parse_goals_from_html(html)

        finally:
            if panel_id is not None:
                with contextlib.suppress(IsabelleToolError):
                    await self.notify("PIDE/state_exit", {"id": panel_id})

    async def _wait_for_state_output(
        self,
        future: asyncio.Future[tuple[int, str]],
        file_path: str,
    ) -> tuple[int, str] | None:
        """Wait for state_output with empty-proof-state detection.

        Returns None when the server is active but no state_output arrives
        within STATE_OUTPUT_GRACE — the command has no proof state to show.
        """
        start = time.time()
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), timeout=self.PROGRESS_CHECK_INTERVAL,
                )
            except asyncio.TimeoutError:
                if future.done():
                    return future.result()
                self._check_server_health(self.STALL_TIMEOUT)
                elapsed = time.time() - start
                process_alive = (
                    self.process is not None
                    and self.process.returncode is None
                )
                if elapsed > self.STATE_OUTPUT_GRACE and process_alive:
                    with contextlib.suppress(ValueError):
                        self._state_init_waiters.remove(future)
                    logger.debug(
                        "No state_output after %.1fs (server active) — "
                        "empty proof state at %s",
                        elapsed, file_path,
                    )
                    return None

    def _enrich_timeout_error(self, file_path: str) -> IsabelleToolError:
        diags = self.diagnostic_cache.diagnostics.get(file_path, [])
        errors = [
            d.get("message", "")
            for d in diags
            if isinstance(d, dict) and d.get("severity") in (1, 2)
        ]
        if errors:
            summary = "; ".join(errors[:3])
            if len(errors) > 3:
                summary += f" (+{len(errors) - 3} more)"
            return IsabelleToolError(
                f"Timed out waiting for proof state. "
                f"File has {len(errors)} error(s): {summary}"
            )
        if not diags:
            return IsabelleToolError(
                "Timed out waiting for proof state. "
                "No diagnostics received — file may not have been processed."
            )
        return IsabelleToolError("Timed out waiting for proof state.")

    async def get_dynamic_output(
        self, file_path: str, line: int, character: int = 0,
    ) -> str:
        """Get dynamic output at position (progress-monitored).

        Holds the caret lock for the duration since dynamic output depends
        on the current caret position (unlike state panels which bind an
        overlay to a specific command). Returns cached/empty output once the
        file appears fully processed with no output at this position.
        """
        uri = file_path_to_uri(file_path)
        key = (file_path, line, character)

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        waiter = (key, future)
        self._dynamic_output_waiters.append(waiter)

        try:
            async with self._caret_lock:
                await self.notify("PIDE/caret_update", {
                    "uri": uri, "line": line, "character": character,
                })
                while True:
                    try:
                        return await asyncio.wait_for(
                            asyncio.shield(future), timeout=self.PROGRESS_CHECK_INTERVAL,
                        )
                    except asyncio.TimeoutError:
                        if future.done():
                            return future.result()
                        self._check_server_health(self.STALL_TIMEOUT)
                        if self.diagnostics_settled(file_path, settle_time=3.0):
                            return self._dynamic_output_cache_by_position.get(key, "")
        finally:
            with contextlib.suppress(ValueError):
                self._dynamic_output_waiters.remove(waiter)

    async def request_preview(
        self, file_path: str, column: int = 0,
    ) -> JsonDict:
        """Request document preview (progress-monitored, no fixed timeout)."""
        uri = file_path_to_uri(file_path)
        key = (uri, column)
        future: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
        self._preview_waiters[key] = future

        try:
            await self.notify("PIDE/preview_request", {"uri": uri, "column": column})
            return await self._wait_with_progress(future)
        finally:
            self._preview_waiters.pop(key, None)
