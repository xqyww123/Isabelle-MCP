"""
LSP Client Wrapper for Isabelle vscode_server.

Manages communication with `isabelle vscode_server` process via JSON-RPC 2.0.
"""

import asyncio
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from isa_lsp.utils import (
    IsabelleToolError,
    file_path_to_uri,
    uri_to_file_path,
)

logger = logging.getLogger(__name__)


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
    diagnostics: Dict[str, List[Dict]] = field(default_factory=dict)
    last_update: Dict[str, float] = field(default_factory=dict)


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
        session_dirs: Optional[List[str]] = None,
        verbose: bool = False,
    ):
        self.logic = logic
        self.session_dirs = session_dirs or []
        self.verbose = verbose

        # Process management
        self.process: Optional[asyncio.subprocess.Process] = None
        self.reader_task: Optional[asyncio.Task] = None

        # Request/response correlation
        self.request_id = 0
        self.pending_requests: Dict[int, asyncio.Future] = {}

        # Document state
        self.open_documents: Dict[str, DocumentState] = {}

        # Diagnostics cache
        self.diagnostic_cache = DiagnosticCache()

        # Server info
        self.server_capabilities: Dict[str, Any] = {}
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
        except FileNotFoundError:
            raise IsabelleToolError(
                "isabelle command not found. Is Isabelle installed and in PATH?"
            )

        self.start_time = time.time()

        # Start background reader
        self.reader_task = asyncio.create_task(self._read_loop())

        # Send initialize request
        await self.initialize()

        logger.info("LSP client started successfully")

    async def initialize(self) -> Dict[str, Any]:
        """Send LSP initialize request.

        Returns:
            Initialize result with server capabilities
        """
        logger.debug("Sending LSP initialize request")

        result = await self.request("initialize", {
            "processId": None,
            "rootUri": None,
            "capabilities": {},
        })

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
            except asyncio.TimeoutError:
                logger.warning("Shutdown request timed out")

            # Send exit notification
            await self.notify("exit", {})

            # Cancel reader task
            if self.reader_task:
                self.reader_task.cancel()
                try:
                    await self.reader_task
                except asyncio.CancelledError:
                    pass

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

        logger.info("LSP client shutdown complete")

    async def request(
        self,
        method: str,
        params: Dict[str, Any],
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
        future: asyncio.Future = asyncio.Future()
        self.pending_requests[req_id] = future

        # Send request
        await self._send(message)

        # Wait for response with timeout
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self.pending_requests.pop(req_id, None)
            raise IsabelleToolError(f"LSP request '{method}' timed out after {timeout}s")

    async def notify(self, method: str, params: Dict[str, Any]) -> None:
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

    async def _send(self, message: Dict) -> None:
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
        self.process.stdin.write(header + content)
        await self.process.stdin.drain()

        logger.debug(f"Sent: {message.get('method', message.get('id'))}")

    async def _read_loop(self) -> None:
        """Background task to read LSP messages."""
        logger.debug("Starting read loop")

        try:
            while True:
                if not self.process or not self.process.stdout:
                    break

                # Read header
                header_line = await self.process.stdout.readline()
                if not header_line:
                    logger.debug("EOF reached")
                    break

                # Parse Content-Length
                match = re.match(b"Content-Length: (\\d+)\r\n", header_line)
                if not match:
                    logger.warning(f"Invalid header: {header_line}")
                    continue

                content_length = int(match.group(1))

                # Skip blank line
                await self.process.stdout.readline()

                # Read content
                content = await self.process.stdout.readexactly(content_length)
                message = json.loads(content.decode('utf-8'))

                # Handle message
                await self._handle_message(message)

        except asyncio.CancelledError:
            logger.debug("Read loop cancelled")
            raise
        except Exception as e:
            logger.error(f"Error in read loop: {e}", exc_info=True)

    async def _handle_message(self, message: Dict) -> None:
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

        elif method.startswith("PIDE/"):
            # PIDE-specific notifications (handled by tools)
            logger.debug(f"PIDE notification: {method}")

        else:
            logger.debug(f"Unhandled notification: {method}")

    # ========================================================================
    # High-level LSP methods
    # ========================================================================

    async def open_document(
        self,
        file_path: str,
        content: Optional[str] = None
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
            with open(file_path, 'r', encoding='utf-8') as f:
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
    ) -> Optional[Dict]:
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

        return result

    async def get_completions(
        self,
        file_path: str,
        line: int,
        character: int
    ) -> Optional[Dict]:
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

        return result

    async def get_definition(
        self,
        file_path: str,
        line: int,
        character: int
    ) -> Optional[Any]:
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
    ) -> Optional[List[Dict]]:
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

        return result

    def get_cached_diagnostics(self, file_path: str) -> List[Dict]:
        """Get cached diagnostics for file.

        Args:
            file_path: Absolute path to theory file

        Returns:
            List of diagnostic dictionaries
        """
        return self.diagnostic_cache.diagnostics.get(file_path, [])

    def is_processing_complete(self, file_path: str) -> bool:
        """Check if PIDE finished processing file (heuristic).

        Args:
            file_path: Absolute path to theory file

        Returns:
            True if likely complete (no updates in last 0.5s)
        """
        last_update = self.diagnostic_cache.last_update.get(file_path, 0)
        return (time.time() - last_update) > 0.5
