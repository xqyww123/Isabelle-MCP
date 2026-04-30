"""
LSP Client Wrapper for Isabelle vscode_server.

Manages communication with `isabelle vscode_server` process via JSON-RPC 2.0.
"""

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

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
    """State of an open document in the LSP session."""
    file_path: str
    uri: str
    version: int
    content: str
    language_id: str = "isabelle"


@dataclass
class DiagnosticCache:
    """Cache for diagnostics received via publishDiagnostics notifications."""
    diagnostics: dict[str, list[dict]] = field(default_factory=dict)
    last_update: dict[str, float] = field(default_factory=dict)


class IsabelleLSPClient:
    """LSP client for isabelle vscode_server.

    Manages the lifecycle of the `isabelle vscode_server` subprocess and
    handles JSON-RPC 2.0 communication over stdin/stdout.

    Args:
        logic: Session name (e.g., "HOL", "HOL-Analysis")
        session_dirs: Additional session directories
        verbose: Enable verbose logging
    """

    def __init__(
        self,
        logic: str = "HOL",
        session_dirs: list[str] | None = None,
        verbose: bool = False,
    ):
        self.logic = logic
        self.session_dirs = session_dirs or []
        self.verbose = verbose

        # Process management
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

        # Request/response correlation
        self.request_id = 0
        self.pending_requests: dict[int, asyncio.Future[Any]] = {}

        # Document state
        self.open_documents: dict[str, DocumentState] = {}

        # Diagnostics cache
        self.diagnostic_cache = DiagnosticCache()

        # PIDE extension state
        self._state_panel_id = 0
        self._state_output_waiters: dict[int, asyncio.Future[str]] = {}
        self._dynamic_output_waiters: list[asyncio.Future[str]] = []
        self._dynamic_output_cache: str = ""
        self._dynamic_output_last_update: float = 0.0
        self._preview_waiters: dict[tuple[str, int], asyncio.Future[JsonDict]] = {}

        # Server info
        self.server_capabilities: dict[str, Any] = {}
        self.isabelle_version: str = ""
        self.start_time: float = 0.0

    async def start(self) -> None:
        """Start the isabelle vscode_server process."""
        logger.info(f"Starting isabelle vscode_server with logic: {self.logic}")

        # Build command
        cmd = ["isabelle", "vscode_server", "-l", self.logic]

        for d in self.session_dirs:
            cmd.extend(["-d", d])

        if self.verbose:
            cmd.append("-v")

        logger.debug(f"Command: {' '.join(cmd)}")

        # Start process
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

        # Start background reader
        self.reader_task = asyncio.create_task(self._read_loop())
        self.stderr_task = asyncio.create_task(self._drain_stderr())

        # Send initialize request
        await self.initialize()

        logger.info("LSP client started successfully")

    async def initialize(self) -> dict[str, Any]:
        """Send LSP initialize request.

        Returns:
            Initialize result with server capabilities
        """
        logger.debug("Sending LSP initialize request")

        response = await self.request("initialize", {
            "processId": None,
            "rootUri": None,
            "capabilities": {},
        })
        result = response if isinstance(response, dict) else {}

        if result:
            self.server_capabilities = result.get("capabilities", {})

            # Get Isabelle version
            server_info = result.get("serverInfo", {})
            self.isabelle_version = server_info.get("version", "unknown")

        # Send initialized notification
        await self.notify("initialized", {})

        logger.info(f"LSP initialized. Server version: {self.isabelle_version}")

        return result

    async def shutdown(self) -> None:
        """Gracefully shutdown the LSP server."""
        logger.info("Shutting down LSP client")

        if self.process and self.process.returncode is None:
            # Send shutdown request
            try:
                await asyncio.wait_for(
                    self.request("shutdown", {}),
                    timeout=5.0
                )
            except IsabelleToolError as exc:
                logger.warning(f"Shutdown request failed: {exc}")

            # Send exit notification
            with contextlib.suppress(IsabelleToolError):
                await self.notify("exit", {})

            # Cancel reader task
            await self._cancel_background_tasks()

            # Terminate process
            try:
                await asyncio.wait_for(
                    self.process.wait(),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning("Process did not terminate, killing")
                self.process.kill()
                await self.process.wait()

        self.open_documents.clear()
        self.pending_requests.clear()
        self._state_output_waiters.clear()
        self._dynamic_output_waiters.clear()
        self._preview_waiters.clear()

        logger.info("LSP client shutdown complete")

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 30.0
    ) -> Any:
        """Send LSP request and wait for response.

        Args:
            method: LSP method name
            params: Request parameters
            timeout: Timeout in seconds

        Returns:
            Response result

        Raises:
            IsabelleToolError: On timeout or error response
        """
        self.request_id += 1
        req_id = self.request_id

        message = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        # Create future for response
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = future

        # Send request
        try:
            await self._send(message)
        except Exception:
            self.pending_requests.pop(req_id, None)
            raise

        # Wait for response with timeout
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError as exc:
            self.pending_requests.pop(req_id, None)
            raise IsabelleToolError(
                f"LSP request '{method}' timed out after {timeout}s"
            ) from exc

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send LSP notification (no response expected).

        Args:
            method: LSP method name
            params: Notification parameters
        """
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        await self._send(message)

    async def _send(self, message: JsonDict) -> None:
        """Send JSON-RPC message with LSP framing.

        Args:
            message: JSON-RPC message
        """
        if not self.process or not self.process.stdin:
            raise IsabelleToolError("LSP process not running")

        # Serialize message
        content = json.dumps(message).encode('utf-8')

        # Add Content-Length header
        header = f"Content-Length: {len(content)}\r\n\r\n".encode('ascii')

        # Write to stdin
        async with self._write_lock:
            try:
                self.process.stdin.write(header + content)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                raise IsabelleToolError("Failed to write to LSP process") from exc

        logger.debug(f"Sent: {message.get('method', message.get('id'))}")

    async def _read_loop(self) -> None:
        """Background task to read LSP messages."""
        logger.debug("Starting read loop")

        try:
            while True:
                if not self.process or not self.process.stdout:
                    break

                message = await self._read_message()
                if message is None:
                    logger.debug("EOF reached")
                    break

                # Handle message
                await self._handle_message(message)

        except asyncio.CancelledError:
            logger.debug("Read loop cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in read loop: {e}", exc_info=True)
            self._fail_pending_waiters(IsabelleToolError(f"LSP read loop failed: {e}"))

    async def _read_message(self) -> JsonDict | None:
        """Read one framed LSP message from stdout."""
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
            if not sep:
                logger.warning(f"Invalid LSP header: {header_line!r}")
                continue
            headers[name.lower()] = value.strip()

        raw_length = headers.get("content-length")
        if raw_length is None:
            logger.warning("LSP message missing Content-Length header")
            return {}

        try:
            content_length = int(raw_length)
        except ValueError:
            logger.warning(f"Invalid Content-Length header: {raw_length}")
            return {}

        content = await self.process.stdout.readexactly(content_length)
        message = json.loads(content.decode("utf-8"))
        if not isinstance(message, dict):
            logger.warning(f"Ignoring non-object LSP message: {message!r}")
            return {}
        return message

    async def _drain_stderr(self) -> None:
        """Drain stderr so a verbose Isabelle process cannot block on a full pipe."""
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
        except Exception as exc:
            logger.debug(f"Error while draining LSP stderr: {exc}", exc_info=True)

    async def _cancel_background_tasks(self) -> None:
        """Cancel background IO tasks."""
        tasks = [task for task in (self.reader_task, self.stderr_task) if task is not None]
        for task in tasks:
            task.cancel()

        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        self.reader_task = None
        self.stderr_task = None

    async def _handle_message(self, message: JsonDict) -> None:
        """Handle incoming LSP message.

        Args:
            message: JSON-RPC message
        """
        # Response to our request
        if "id" in message and message["id"] in self.pending_requests:
            req_id = message["id"]
            future = self.pending_requests.pop(req_id)

            if "result" in message:
                future.set_result(message["result"])
            elif "error" in message:
                error = message["error"]
                future.set_exception(
                    IsabelleToolError(f"LSP error: {error.get('message', 'Unknown')}")
                )

        # Notification from server
        elif "method" in message:
            method = message["method"]
            params = message.get("params", {})

            await self._handle_notification(method, params)

    async def _handle_notification(self, method: str, params: Any) -> None:
        """Handle server notification.

        Args:
            method: Notification method
            params: Notification parameters
        """
        if method == "textDocument/publishDiagnostics":
            # Cache diagnostics
            uri = params.get("uri", "")
            diagnostics = params.get("diagnostics", [])

            file_path = uri_to_file_path(uri)
            self.diagnostic_cache.diagnostics[file_path] = diagnostics
            self.diagnostic_cache.last_update[file_path] = time.time()

            logger.debug(f"Cached {len(diagnostics)} diagnostics for {file_path}")

        elif method == "PIDE/state_output":
            self._handle_state_output(params)

        elif method == "PIDE/dynamic_output":
            self._handle_dynamic_output(params)

        elif method == "PIDE/preview_response":
            self._handle_preview_response(params)

        else:
            logger.debug(f"Unhandled notification: {method}")

    def _handle_state_output(self, params: Any) -> None:
        """Resolve waiters for PIDE state panel output."""
        if not isinstance(params, dict):
            return

        raw_panel_id = params.get("id")
        html = str(params.get("content", params.get("output", "")))

        panel_id = raw_panel_id if isinstance(raw_panel_id, int) else None
        if panel_id is None and len(self._state_output_waiters) == 1:
            panel_id = next(iter(self._state_output_waiters))

        if panel_id is None:
            logger.debug("PIDE/state_output without matching panel id")
            return

        future = self._state_output_waiters.pop(panel_id, None)
        if future and not future.done():
            future.set_result(html)

    def _handle_dynamic_output(self, params: Any) -> None:
        """Cache and publish the latest PIDE dynamic output notification."""
        if not isinstance(params, dict):
            return

        html = str(params.get("content", ""))
        self._dynamic_output_cache = html
        self._dynamic_output_last_update = time.time()

        waiters = self._dynamic_output_waiters
        self._dynamic_output_waiters = []
        for future in waiters:
            if not future.done():
                future.set_result(html)

    def _handle_preview_response(self, params: Any) -> None:
        """Resolve waiters for PIDE preview responses."""
        if not isinstance(params, dict):
            return

        uri = str(params.get("uri", ""))
        column = params.get("column", 0)
        if not isinstance(column, int):
            column = 0

        key = (uri, column)
        future = self._preview_waiters.pop(key, None)
        if future and not future.done():
            future.set_result(params)

    def _fail_pending_waiters(self, exc: Exception) -> None:
        """Fail outstanding JSON-RPC/PIDE waiters after transport failure."""
        for waiters in (
            self.pending_requests.values(),
            self._state_output_waiters.values(),
            self._dynamic_output_waiters,
            self._preview_waiters.values(),
        ):
            for future in list(waiters):
                if not future.done():
                    future.set_exception(exc)

        self.pending_requests.clear()
        self._state_output_waiters.clear()
        self._dynamic_output_waiters.clear()
        self._preview_waiters.clear()

    # ========================================================================
    # High-level LSP methods
    # ========================================================================

    async def open_document(
        self,
        file_path: str,
        content: str | None = None
    ) -> None:
        """Open document in LSP session.

        Args:
            file_path: Absolute path to theory file
            content: File content (if None, read from file)
        """
        if file_path in self.open_documents:
            logger.debug(f"Document already open: {file_path}")
            return

        # Read content if not provided
        if content is None:
            with open(file_path, encoding='utf-8') as f:
                content = f.read()

        uri = file_path_to_uri(file_path)

        # Send didOpen
        await self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "isabelle",
                "version": 1,
                "text": content,
            }
        })

        # Update state
        self.open_documents[file_path] = DocumentState(
            file_path=file_path,
            uri=uri,
            version=1,
            content=content,
        )

        logger.info(f"Opened document: {file_path}")

        # Wait for initial processing (heuristic)
        await asyncio.sleep(2.0)

    async def close_document(self, file_path: str) -> None:
        """Close document in LSP session.

        Args:
            file_path: Absolute path to theory file
        """
        if file_path not in self.open_documents:
            logger.warning(f"Document not open: {file_path}")
            return

        doc = self.open_documents[file_path]

        await self.notify("textDocument/didClose", {
            "textDocument": {"uri": doc.uri}
        })

        del self.open_documents[file_path]

        logger.info(f"Closed document: {file_path}")

    async def get_hover(
        self,
        file_path: str,
        line: int,
        character: int
    ) -> JsonDict | None:
        """Get hover information at position.

        Args:
            file_path: Absolute path to theory file
            line: Line number (0-indexed for LSP)
            character: Character number (0-indexed for LSP)

        Returns:
            Hover result or None
        """
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
        character: int
    ) -> JsonDict | None:
        """Get completions at position.

        Args:
            file_path: Absolute path to theory file
            line: Line number (0-indexed for LSP)
            character: Character number (0-indexed for LSP)

        Returns:
            Completion result or None
        """
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")

        result = await self.request("textDocument/completion", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })

        return result if isinstance(result, dict) or result is None else None

    async def get_definition(
        self,
        file_path: str,
        line: int,
        character: int
    ) -> Any | None:
        """Get definition location at position.

        Args:
            file_path: Absolute path to theory file
            line: Line number (0-indexed for LSP)
            character: Character number (0-indexed for LSP)

        Returns:
            Definition location(s) or None
        """
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")

        result = await self.request("textDocument/definition", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })

        return result

    async def get_highlights(
        self,
        file_path: str,
        line: int,
        character: int
    ) -> list[JsonDict] | None:
        """Get document highlights at position.

        Args:
            file_path: Absolute path to theory file
            line: Line number (0-indexed for LSP)
            character: Character number (0-indexed for LSP)

        Returns:
            List of highlights or None
        """
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")

        result = await self.request("textDocument/documentHighlight", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })

        if result is None:
            return None
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return None

    def get_cached_diagnostics(self, file_path: str) -> list[dict]:
        """Get cached diagnostics for file.

        Args:
            file_path: Absolute path to theory file

        Returns:
            List of diagnostic dictionaries
        """
        return self.diagnostic_cache.diagnostics.get(file_path, [])

    def is_processing_complete(self, file_path: str) -> bool:
        """Check if PIDE finished processing file (heuristic).

        Returns False for files never seen (no diagnostics received yet).
        Returns True if no diagnostic updates in last 0.5s.
        """
        if file_path not in self.diagnostic_cache.last_update:
            return False
        return (time.time() - self.diagnostic_cache.last_update[file_path]) > 0.5

    async def get_goals_at_position(
        self,
        file_path: str,
        line: int,
        character: int,
        timeout: float = 5.0,
    ) -> list[str]:
        """Get proof goals at an LSP position using PIDE state panel output."""
        uri = file_path_to_uri(file_path)
        self._state_panel_id += 1
        panel_id = self._state_panel_id
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._state_output_waiters[panel_id] = future

        try:
            await self.notify("PIDE/state_init", {})
            await self.notify("PIDE/caret_update", {
                "uri": uri,
                "line": line,
                "character": character,
            })

            html = await asyncio.wait_for(future, timeout=timeout)
            return parse_goals_from_html(html)
        except asyncio.TimeoutError as exc:
            raise IsabelleToolError("Timed out waiting for PIDE proof state") from exc
        finally:
            self._state_output_waiters.pop(panel_id, None)
            with contextlib.suppress(IsabelleToolError):
                await self.notify("PIDE/state_exit", {"id": panel_id})

    async def get_dynamic_output(
        self,
        file_path: str,
        line: int,
        character: int = 0,
        timeout: float = 2.0,
    ) -> str:
        """Move the caret and return the latest PIDE dynamic output HTML."""
        uri = file_path_to_uri(file_path)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._dynamic_output_waiters.append(future)

        await self.notify("PIDE/caret_update", {
            "uri": uri,
            "line": line,
            "character": character,
        })

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ValueError):
                self._dynamic_output_waiters.remove(future)
            return self._dynamic_output_cache

    async def request_preview(
        self,
        file_path: str,
        column: int = 0,
        timeout: float = 30.0,
    ) -> JsonDict:
        """Request an HTML preview and wait for the matching PIDE response."""
        uri = file_path_to_uri(file_path)
        key = (uri, column)
        future: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
        self._preview_waiters[key] = future

        try:
            await self.notify("PIDE/preview_request", {
                "uri": uri,
                "column": column,
            })
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise IsabelleToolError("Timed out waiting for PIDE preview") from exc
        finally:
            self._preview_waiters.pop(key, None)
