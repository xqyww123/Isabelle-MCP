"""
Unit tests for LSP client.
"""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from isa_lsp.lsp_client import DiagnosticCache, IsabelleLSPClient
from isa_lsp.utils import IsabelleToolError


class TestIsabelleLSPClient:
    """Test LSP client initialization and lifecycle."""

    def test_init_default(self):
        """Test client initialization with defaults."""
        client = IsabelleLSPClient()
        assert client.logic == "HOL"
        assert client.process is None
        assert client.request_id == 0
        assert client.open_documents == {}
        assert isinstance(client.diagnostic_cache, DiagnosticCache)
        assert client.diagnostic_cache.diagnostics == {}

    def test_init_custom_logic(self):
        """Test client initialization with custom logic."""
        client = IsabelleLSPClient(logic="Main")
        assert client.logic == "Main"

    def test_init_session_dirs(self):
        """Test client initialization with session dirs."""
        client = IsabelleLSPClient(session_dirs=["/extra/sessions"])
        assert client.session_dirs == ["/extra/sessions"]

    @pytest.mark.asyncio
    async def test_send_message(self):
        """Test sending JSON-RPC message via _send."""
        client = IsabelleLSPClient()

        client.process = MagicMock()
        client.process.stdin = MagicMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        message = {"jsonrpc": "2.0", "id": 1, "method": "test", "params": {}}
        await client._send(message)

        client.process.stdin.write.assert_called_once()
        written = client.process.stdin.write.call_args[0][0]
        assert b"Content-Length:" in written
        assert b'"method": "test"' in written

    @pytest.mark.asyncio
    async def test_send_notification(self):
        """Test sending notification."""
        client = IsabelleLSPClient()

        client.process = MagicMock()
        client.process.stdin = MagicMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        await client.notify("test/notification", {"param": "value"})

        assert len(client.pending_requests) == 0

    def test_message_serialization(self):
        """Test LSP message serialization."""
        message = {"jsonrpc": "2.0", "method": "test", "params": {}}
        content = json.dumps(message).encode('utf-8')
        header = f"Content-Length: {len(content)}\r\n\r\n".encode('ascii')
        assert len(content) > 0
        assert header.startswith(b"Content-Length: ")

    @pytest.mark.asyncio
    async def test_handle_response(self):
        """Test handling JSON-RPC response."""
        client = IsabelleLSPClient()

        future = asyncio.Future()
        client.pending_requests[1] = future

        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"success": True}
        }

        await client._handle_message(response)

        assert future.done()
        assert future.result() == {"success": True}
        assert 1 not in client.pending_requests

    @pytest.mark.asyncio
    async def test_handle_error_response(self):
        """Test handling JSON-RPC error response."""
        client = IsabelleLSPClient()

        future = asyncio.Future()
        client.pending_requests[1] = future

        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32600,
                "message": "Invalid Request"
            }
        }

        await client._handle_message(response)

        assert future.done()
        with pytest.raises(IsabelleToolError, match="Invalid Request"):
            future.result()

    @pytest.mark.asyncio
    async def test_handle_notification(self):
        """Test handling server notifications."""
        client = IsabelleLSPClient()

        notification = {
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": "file:///test.thy",
                "diagnostics": [
                    {
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 10}
                        },
                        "severity": 1,
                        "message": "Error"
                    }
                ]
            }
        }

        await client._handle_message(notification)

        assert "/test.thy" in client.diagnostic_cache.diagnostics
        assert len(client.diagnostic_cache.diagnostics["/test.thy"]) == 1

    @pytest.mark.asyncio
    async def test_open_document_tracking(self):
        """Test document open tracking."""
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdin = MagicMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.thy', delete=False) as f:
            f.write("theory Test imports Main begin end")
            temp_file = f.name

        try:
            with patch('asyncio.sleep', new_callable=AsyncMock):
                await client.open_document(temp_file)

            assert temp_file in client.open_documents
            assert client.open_documents[temp_file].version == 1
        finally:
            Path(temp_file).unlink()

    @pytest.mark.asyncio
    async def test_open_document_idempotent(self):
        """Test that opening same document multiple times is idempotent."""
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdin = MagicMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.thy', delete=False) as f:
            f.write("theory Test imports Main begin end")
            temp_file = f.name

        try:
            with patch('asyncio.sleep', new_callable=AsyncMock):
                await client.open_document(temp_file)
                version1 = client.open_documents[temp_file].version

                await client.open_document(temp_file)
                version2 = client.open_documents[temp_file].version

            assert version1 == version2
        finally:
            Path(temp_file).unlink()

    @pytest.mark.asyncio
    async def test_close_document(self):
        """Test closing a document."""
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdin = MagicMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.thy', delete=False) as f:
            f.write("theory Test imports Main begin end")
            temp_file = f.name

        try:
            with patch('asyncio.sleep', new_callable=AsyncMock):
                await client.open_document(temp_file)
            assert temp_file in client.open_documents

            await client.close_document(temp_file)
            assert temp_file not in client.open_documents
        finally:
            Path(temp_file).unlink()

    def test_diagnostics_cache(self):
        """Test diagnostics caching."""
        client = IsabelleLSPClient()

        file_path = "/test.thy"
        diagnostics = [
            {"severity": 1, "message": "Error"},
            {"severity": 2, "message": "Warning"}
        ]

        client.diagnostic_cache.diagnostics[file_path] = diagnostics

        cached = client.get_cached_diagnostics(file_path)
        assert len(cached) == 2
        assert cached == diagnostics

    def test_diagnostics_cache_empty(self):
        """Test getting diagnostics for unopened file."""
        client = IsabelleLSPClient()

        cached = client.get_cached_diagnostics("/nonexistent.thy")
        assert cached == []

    def test_processing_status_default(self):
        """Test processing status for unknown file."""
        client = IsabelleLSPClient()

        assert client.is_processing_complete("/test.thy") is False

    def test_processing_status_tracking(self):
        """Test processing status via diagnostic update timestamps."""
        client = IsabelleLSPClient()

        client.diagnostic_cache.last_update["/test.thy"] = time.time() - 10.0
        assert client.is_processing_complete("/test.thy") is True

        client.diagnostic_cache.last_update["/test.thy"] = time.time()
        assert client.is_processing_complete("/test.thy") is False

    @pytest.mark.asyncio
    async def test_request_timeout(self):
        """Test request timeout handling."""
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdin = MagicMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        with pytest.raises(IsabelleToolError, match="timed out"):
            await client.request("test/method", {}, timeout=0.1)

    def test_uri_conversion(self):
        """Test file path to URI conversion in client."""
        from isa_lsp.utils import file_path_to_uri

        path = "/home/user/test.thy"
        uri = file_path_to_uri(path)

        assert uri.startswith("file://")
        assert "test.thy" in uri
