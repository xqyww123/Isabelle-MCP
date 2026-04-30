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

    @pytest.mark.asyncio
    async def test_read_message_with_multiple_headers(self):
        """Test LSP message framing with more than Content-Length."""
        client = IsabelleLSPClient()
        message = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        content = json.dumps(message).encode("utf-8")

        client.process = MagicMock()
        client.process.stdout = MagicMock()
        client.process.stdout.readline = AsyncMock(
            side_effect=[
                f"Content-Length: {len(content)}\r\n".encode("ascii"),
                b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n",
                b"\r\n",
            ]
        )
        client.process.stdout.readexactly = AsyncMock(return_value=content)

        assert await client._read_message() == message

    @pytest.mark.asyncio
    async def test_handle_state_output_resolves_waiter(self):
        """Test PIDE/state_output resolves the matching proof-state waiter."""
        client = IsabelleLSPClient()
        future = asyncio.get_running_loop().create_future()
        client._state_output_waiters[7] = future

        client._handle_state_output({"id": 7, "content": "<pre>1. P</pre>"})

        assert future.done()
        assert future.result() == "<pre>1. P</pre>"
        assert 7 not in client._state_output_waiters

    @pytest.mark.asyncio
    async def test_handle_state_output_resolves_init_waiter_with_server_id(self):
        """Test PIDE/state_output teaches the server-assigned state panel id."""
        client = IsabelleLSPClient()
        future = asyncio.get_running_loop().create_future()
        client._state_init_waiters.append(future)

        client._handle_state_output({"id": 42, "content": "<pre>1. P</pre>"})

        assert future.done()
        assert future.result() == (42, "<pre>1. P</pre>")
        assert client._state_init_waiters == []

    @pytest.mark.asyncio
    async def test_handle_dynamic_output_caches_and_resolves_waiter(self):
        """Test PIDE/dynamic_output updates cache and notifies waiters."""
        client = IsabelleLSPClient()
        future = asyncio.get_running_loop().create_future()
        key = ("/tmp/Test.thy", 3, 0)
        client._dynamic_output_waiters.append((key, future))

        client._handle_dynamic_output({"content": "<div class='writeln'>ok</div>"})

        assert future.done()
        assert future.result() == "<div class='writeln'>ok</div>"
        assert client._dynamic_output_cache == "<div class='writeln'>ok</div>"
        assert client._dynamic_output_cache_by_position[key] == "<div class='writeln'>ok</div>"

    @pytest.mark.asyncio
    async def test_handle_preview_response_resolves_matching_waiter(self):
        """Test PIDE/preview_response resolves only the matching URI/column waiter."""
        client = IsabelleLSPClient()
        future = asyncio.get_running_loop().create_future()
        client._preview_waiters[("file:///tmp/Test.thy", 0)] = future

        client._handle_preview_response({
            "uri": "file:///tmp/Test.thy",
            "column": 0,
            "content": "<html>Preview</html>",
        })

        assert future.done()
        assert future.result()["content"] == "<html>Preview</html>"
        assert ("file:///tmp/Test.thy", 0) not in client._preview_waiters

    @pytest.mark.asyncio
    async def test_get_goals_at_position_uses_server_assigned_state_id(self):
        """Test state query learns and exits the server-assigned panel id."""
        client = IsabelleLSPClient()
        calls = []

        async def fake_notify(method, params):
            calls.append((method, params))
            if method == "PIDE/state_init":
                client._handle_state_output({"id": 99, "content": "<pre>1. P</pre>"})

        client.notify = AsyncMock(side_effect=fake_notify)

        goals = await client.get_goals_at_position("/tmp/Test.thy", 7, 3)

        assert goals == ["P"]
        assert calls[0][0] == "PIDE/caret_update"
        assert calls[1] == ("PIDE/state_init", {})
        assert calls[-1] == ("PIDE/state_exit", {"id": 99})

    @pytest.mark.asyncio
    async def test_dynamic_output_timeout_does_not_reuse_other_position_cache(self):
        """Test command output does not return stale content from another position."""
        client = IsabelleLSPClient()
        client.notify = AsyncMock()
        client._dynamic_output_cache = "<div class='writeln'>old</div>"
        client._dynamic_output_cache_by_position[("/tmp/Other.thy", 1, 0)] = (
            "<div class='writeln'>old</div>"
        )

        result = await client.get_dynamic_output("/tmp/Test.thy", 1, timeout=0.01)

        assert result == ""

    def test_uri_conversion(self):
        """Test file path to URI conversion in client."""
        from isa_lsp.utils import file_path_to_uri

        path = "/home/user/test.thy"
        uri = file_path_to_uri(path)

        assert uri.startswith("file://")
        assert "test.thy" in uri
