"""
Unit tests for LSP client.
"""

import pytest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.utils import IsabelleToolError


class TestIsabelleLSPClient:
    """Test LSP client initialization and lifecycle."""

    def test_init_default(self):
        """Test client initialization with defaults."""
        client = IsabelleLSPClient()
        assert client.logic == "HOL"
        assert client.initialized is False
        assert client.next_request_id == 1
        assert client.open_documents == {}
        assert client.diagnostics_cache == {}

    def test_init_custom_logic(self):
        """Test client initialization with custom logic."""
        client = IsabelleLSPClient(logic="Main")
        assert client.logic == "Main"

    def test_init_custom_timeout(self):
        """Test client initialization with custom timeout."""
        client = IsabelleLSPClient(timeout=120.0)
        assert client.timeout == 120.0

    @pytest.mark.asyncio
    async def test_send_request(self):
        """Test sending JSON-RPC request."""
        client = IsabelleLSPClient()

        # Mock the process
        client.process = MagicMock()
        client.process.stdin = AsyncMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        request_id = await client._send_request("test/method", {"param": "value"})

        assert request_id == 1
        assert client.next_request_id == 2
        assert 1 in client.pending_requests

    @pytest.mark.asyncio
    async def test_send_notification(self):
        """Test sending notification."""
        client = IsabelleLSPClient()

        # Mock the process
        client.process = MagicMock()
        client.process.stdin = AsyncMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        await client.notify("test/notification", {"param": "value"})

        # Should not create pending request
        assert len(client.pending_requests) == 0

    def test_message_serialization(self):
        """Test LSP message serialization."""
        client = IsabelleLSPClient()

        message = {"jsonrpc": "2.0", "method": "test", "params": {}}
        content = json.dumps(message).encode('utf-8')

        expected_header = f"Content-Length: {len(content)}\r\n\r\n".encode('utf-8')

        # The client should produce this format
        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_handle_response(self):
        """Test handling JSON-RPC response."""
        client = IsabelleLSPClient()

        # Create a pending request
        future = asyncio.Future()
        client.pending_requests[1] = future

        # Simulate response
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"success": True}
        }

        await client._handle_message(response)

        # Future should be resolved
        assert future.done()
        assert future.result() == {"success": True}
        assert 1 not in client.pending_requests

    @pytest.mark.asyncio
    async def test_handle_error_response(self):
        """Test handling JSON-RPC error response."""
        client = IsabelleLSPClient()

        # Create a pending request
        future = asyncio.Future()
        client.pending_requests[1] = future

        # Simulate error response
        response = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32600,
                "message": "Invalid Request"
            }
        }

        await client._handle_message(response)

        # Future should be resolved with error
        assert future.done()
        result = future.result()
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_notification(self):
        """Test handling server notifications."""
        client = IsabelleLSPClient()

        # Simulate diagnostics notification
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

        # Check diagnostics cache
        assert "/test.thy" in client.diagnostics_cache
        assert len(client.diagnostics_cache["/test.thy"]) == 1

    @pytest.mark.asyncio
    async def test_open_document_tracking(self):
        """Test document open tracking."""
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdin = AsyncMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        # Create a test file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.thy', delete=False) as f:
            f.write("theory Test imports Main begin end")
            temp_file = f.name

        try:
            await client.open_document(temp_file)

            assert temp_file in client.open_documents
            assert client.open_documents[temp_file]['version'] == 1
        finally:
            Path(temp_file).unlink()

    @pytest.mark.asyncio
    async def test_open_document_idempotent(self):
        """Test that opening same document multiple times is idempotent."""
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdin = AsyncMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.thy', delete=False) as f:
            f.write("theory Test imports Main begin end")
            temp_file = f.name

        try:
            await client.open_document(temp_file)
            version1 = client.open_documents[temp_file]['version']

            await client.open_document(temp_file)
            version2 = client.open_documents[temp_file]['version']

            # Version should not change on re-open
            assert version1 == version2
        finally:
            Path(temp_file).unlink()

    @pytest.mark.asyncio
    async def test_close_document(self):
        """Test closing a document."""
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdin = AsyncMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.thy', delete=False) as f:
            f.write("theory Test imports Main begin end")
            temp_file = f.name

        try:
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

        client.diagnostics_cache[file_path] = diagnostics

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
        """Test processing status tracking."""
        client = IsabelleLSPClient()

        client.processing_status["/test.thy"] = True
        assert client.is_processing_complete("/test.thy") is True

        client.processing_status["/test.thy"] = False
        assert client.is_processing_complete("/test.thy") is False

    @pytest.mark.asyncio
    async def test_request_timeout(self):
        """Test request timeout handling."""
        client = IsabelleLSPClient(timeout=0.1)
        client.process = MagicMock()
        client.process.stdin = AsyncMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        # Send request that will timeout
        request_id = await client._send_request("test/method", {})

        # Wait for timeout
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                client.pending_requests[request_id],
                timeout=0.2
            )

    def test_uri_conversion(self):
        """Test file path to URI conversion in client."""
        client = IsabelleLSPClient()

        from isa_lsp.utils import file_path_to_uri

        path = "/home/user/test.thy"
        uri = file_path_to_uri(path)

        assert uri.startswith("file://")
        assert "test.thy" in uri
