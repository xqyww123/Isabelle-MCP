"""Tests for LSP client."""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from isa_lsp.lsp_client import DocumentState, IsabelleLSPClient
from isa_lsp.utils import IsabelleToolError


class TestIsabelleLSPClient:
    def test_init_default(self):
        client = IsabelleLSPClient()
        assert client.logic == "HOL"
        assert client.process is None
        assert client.request_id == 0
        assert client.open_documents == {}
        assert client.diagnostic_cache.diagnostics == {}

    def test_init_custom_logic(self):
        client = IsabelleLSPClient(logic="Main")
        assert client.logic == "Main"

    def test_init_session_dirs(self):
        client = IsabelleLSPClient(session_dirs=["/extra/sessions"])
        assert client.session_dirs == ["/extra/sessions"]

    @pytest.mark.asyncio
    async def test_send_message(self):
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdin = MagicMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        await client._send({"jsonrpc": "2.0", "id": 1, "method": "test", "params": {}})

        written = client.process.stdin.write.call_args[0][0]
        assert b"Content-Length:" in written
        assert b'"method": "test"' in written

    @pytest.mark.asyncio
    async def test_send_notification(self):
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdin = MagicMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        await client.notify("test/notification", {"param": "value"})
        assert len(client.pending_requests) == 0

    @pytest.mark.asyncio
    async def test_send_without_process_raises(self):
        client = IsabelleLSPClient()
        with pytest.raises(IsabelleToolError, match="LSP process not running"):
            await client._send({"jsonrpc": "2.0", "method": "test", "params": {}})

    @pytest.mark.asyncio
    async def test_send_broken_pipe_raises_tool_error(self):
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdin = MagicMock()
        client.process.stdin.write = MagicMock(side_effect=BrokenPipeError)
        client.process.stdin.drain = AsyncMock()

        with pytest.raises(IsabelleToolError, match="Failed to write"):
            await client._send({"jsonrpc": "2.0", "method": "test", "params": {}})

    @pytest.mark.asyncio
    async def test_request_send_failure_clears_pending_request(self):
        client = IsabelleLSPClient()
        client._send = AsyncMock(side_effect=IsabelleToolError("write failed"))

        with pytest.raises(IsabelleToolError, match="write failed"):
            await client.request("test/method", {})

        assert client.pending_requests == {}

    @pytest.mark.asyncio
    async def test_handle_response(self):
        client = IsabelleLSPClient()
        future = asyncio.Future()
        client.pending_requests[1] = future

        await client._handle_message({"jsonrpc": "2.0", "id": 1, "result": {"success": True}})

        assert future.done()
        assert future.result() == {"success": True}
        assert 1 not in client.pending_requests

    @pytest.mark.asyncio
    async def test_handle_error_response(self):
        client = IsabelleLSPClient()
        future = asyncio.Future()
        client.pending_requests[1] = future

        await client._handle_message({
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": -32600, "message": "Invalid Request"},
        })

        assert future.done()
        with pytest.raises(IsabelleToolError, match="Invalid Request"):
            future.result()

    @pytest.mark.asyncio
    async def test_handle_malformed_response_fails_request(self):
        client = IsabelleLSPClient()
        future = asyncio.Future()
        client.pending_requests[1] = future

        await client._handle_message({"jsonrpc": "2.0", "id": 1})

        assert future.done()
        with pytest.raises(IsabelleToolError, match="missing result/error"):
            future.result()
        assert 1 not in client.pending_requests

    @pytest.mark.asyncio
    async def test_handle_non_dict_error_response(self):
        client = IsabelleLSPClient()
        future = asyncio.Future()
        client.pending_requests[1] = future

        await client._handle_message({"jsonrpc": "2.0", "id": 1, "error": "boom"})

        assert future.done()
        with pytest.raises(IsabelleToolError, match="boom"):
            future.result()

    @pytest.mark.asyncio
    async def test_handle_diagnostics_notification(self):
        client = IsabelleLSPClient()

        await client._handle_message({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {
                "uri": "file:///test.thy",
                "diagnostics": [
                    {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 10}},
                     "severity": 1, "message": "Error"},
                ],
            },
        })

        assert "/test.thy" in client.diagnostic_cache.diagnostics
        assert len(client.diagnostic_cache.diagnostics["/test.thy"]) == 1

    @pytest.mark.asyncio
    async def test_diagnostics_notification_sets_event(self):
        client = IsabelleLSPClient()
        event = asyncio.Event()
        client._first_diagnostic_event["/test.thy"] = event

        await client._handle_notification("textDocument/publishDiagnostics", {
            "uri": "file:///test.thy", "diagnostics": [],
        })

        assert event.is_set()

    @pytest.mark.asyncio
    async def test_malformed_diagnostics_notification_is_ignored(self):
        client = IsabelleLSPClient()

        await client._handle_notification("textDocument/publishDiagnostics", [])
        await client._handle_notification("textDocument/publishDiagnostics", {})
        await client._handle_notification("textDocument/publishDiagnostics", {
            "uri": "not-a-file-uri",
            "diagnostics": [],
        })

        assert client.diagnostic_cache.diagnostics == {}

    @pytest.mark.asyncio
    async def test_open_document_tracking(self):
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
            await client.open_document(temp_file, wait_for_diagnostics=False)
            assert temp_file in client.open_documents
            assert client.open_documents[temp_file].version == 1
        finally:
            Path(temp_file).unlink()

    @pytest.mark.asyncio
    async def test_open_document_idempotent(self):
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
            await client.open_document(temp_file, wait_for_diagnostics=False)
            v1 = client.open_documents[temp_file].version
            await client.open_document(temp_file, wait_for_diagnostics=False)
            v2 = client.open_documents[temp_file].version
            assert v1 == v2
        finally:
            Path(temp_file).unlink()

    @pytest.mark.asyncio
    async def test_close_document(self):
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
            await client.open_document(temp_file, wait_for_diagnostics=False)
            assert temp_file in client.open_documents
            await client.close_document(temp_file)
            assert temp_file not in client.open_documents
        finally:
            Path(temp_file).unlink()

    @pytest.mark.asyncio
    async def test_close_document_clears_diagnostic_state(self):
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
            await client.open_document(temp_file, wait_for_diagnostics=False)
            client.diagnostic_cache.diagnostics[temp_file] = [{"message": "old"}]
            client.diagnostic_cache.last_update[temp_file] = time.time()
            client._first_diagnostic_event[temp_file] = asyncio.Event()

            await client.close_document(temp_file)

            assert temp_file not in client.diagnostic_cache.diagnostics
            assert temp_file not in client.diagnostic_cache.last_update
            assert temp_file not in client._first_diagnostic_event
        finally:
            Path(temp_file).unlink()

    def test_diagnostics_cache(self):
        client = IsabelleLSPClient()
        diagnostics = [{"severity": 1, "message": "Error"}, {"severity": 2, "message": "Warning"}]
        client.diagnostic_cache.diagnostics["/test.thy"] = diagnostics
        assert client.get_cached_diagnostics("/test.thy") == diagnostics

    @pytest.mark.asyncio
    async def test_shutdown_clears_diagnostic_state(self):
        client = IsabelleLSPClient()
        client.diagnostic_cache.diagnostics["/test.thy"] = [{"message": "old"}]
        client.diagnostic_cache.last_update["/test.thy"] = time.time()
        client._first_diagnostic_event["/test.thy"] = asyncio.Event()

        await client.shutdown()

        assert client.diagnostic_cache.diagnostics == {}
        assert client.diagnostic_cache.last_update == {}
        assert client._first_diagnostic_event == {}

    @pytest.mark.asyncio
    async def test_wait_for_first_diagnostics_returns_false_without_stale_cache(self):
        client = IsabelleLSPClient()
        assert await client.wait_for_first_diagnostics("/test.thy", timeout=0.01) is False

    @pytest.mark.asyncio
    async def test_wait_for_first_diagnostics_returns_true_when_event_is_set(self):
        client = IsabelleLSPClient()
        wait_task = asyncio.create_task(
            client.wait_for_first_diagnostics("/test.thy", timeout=1)
        )

        await asyncio.sleep(0)
        client._first_diagnostic_event["/test.thy"].set()

        assert await wait_task is True

    def test_diagnostics_cache_empty(self):
        client = IsabelleLSPClient()
        assert client.get_cached_diagnostics("/nonexistent.thy") == []

    def test_diagnostics_settled_default(self):
        client = IsabelleLSPClient()
        assert client.diagnostics_settled("/test.thy") is False

    def test_diagnostics_settled_tracking(self):
        client = IsabelleLSPClient()
        client.diagnostic_cache.last_update["/test.thy"] = time.time() - 10.0
        assert client.diagnostics_settled("/test.thy") is True
        client.diagnostic_cache.last_update["/test.thy"] = time.time()
        assert client.diagnostics_settled("/test.thy") is False

    @pytest.mark.asyncio
    async def test_request_timeout(self):
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdin = MagicMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        with pytest.raises(IsabelleToolError, match="timed out"):
            await client.request("test/method", {}, timeout=0.1)

    @pytest.mark.asyncio
    async def test_get_completions_accepts_lsp_completion_item_list(self):
        client = IsabelleLSPClient()
        client.open_documents["/tmp/Test.thy"] = DocumentState(
            file_path="/tmp/Test.thy",
            uri="file:///tmp/Test.thy",
            version=1,
            content="",
        )
        client.request = AsyncMock(return_value=[{"label": "lemma"}])

        result = await client.get_completions("/tmp/Test.thy", 0, 0)

        assert result == [{"label": "lemma"}]

    @pytest.mark.asyncio
    async def test_read_message_with_multiple_headers(self):
        client = IsabelleLSPClient()
        message = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        content = json.dumps(message).encode("utf-8")

        client.process = MagicMock()
        client.process.stdout = MagicMock()
        client.process.stdout.readline = AsyncMock(side_effect=[
            f"Content-Length: {len(content)}\r\n".encode("ascii"),
            b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n",
            b"\r\n",
        ])
        client.process.stdout.readexactly = AsyncMock(return_value=content)
        assert await client._read_message() == message

    @pytest.mark.asyncio
    async def test_read_message_missing_content_length_returns_empty_message(self):
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdout = MagicMock()
        client.process.stdout.readline = AsyncMock(side_effect=[
            b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n",
            b"\r\n",
        ])

        assert await client._read_message() == {}

    @pytest.mark.asyncio
    async def test_read_message_invalid_content_length_returns_empty_message(self):
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdout = MagicMock()
        client.process.stdout.readline = AsyncMock(side_effect=[
            b"Content-Length: nope\r\n",
            b"\r\n",
        ])

        assert await client._read_message() == {}

    @pytest.mark.asyncio
    async def test_read_message_invalid_json_returns_empty_message(self):
        client = IsabelleLSPClient()
        content = b"{not json"
        client.process = MagicMock()
        client.process.stdout = MagicMock()
        client.process.stdout.readline = AsyncMock(side_effect=[
            f"Content-Length: {len(content)}\r\n".encode("ascii"),
            b"\r\n",
        ])
        client.process.stdout.readexactly = AsyncMock(return_value=content)

        assert await client._read_message() == {}

    @pytest.mark.asyncio
    async def test_handle_state_output_resolves_waiter(self):
        client = IsabelleLSPClient()
        future = asyncio.get_running_loop().create_future()
        client._state_output_waiters[7] = future

        client._handle_state_output({"id": 7, "content": "<pre>1. P</pre>"})

        assert future.done()
        assert future.result() == "<pre>1. P</pre>"
        assert 7 not in client._state_output_waiters

    @pytest.mark.asyncio
    async def test_handle_state_output_resolves_init_waiter(self):
        client = IsabelleLSPClient()
        future = asyncio.get_running_loop().create_future()
        client._state_init_waiters.append(future)

        client._handle_state_output({"id": 42, "content": "<pre>1. P</pre>"})

        assert future.done()
        assert future.result() == (42, "<pre>1. P</pre>")

    @pytest.mark.asyncio
    async def test_handle_dynamic_output(self):
        client = IsabelleLSPClient()
        future = asyncio.get_running_loop().create_future()
        key = ("/tmp/Test.thy", 3, 0)
        client._dynamic_output_waiters.append((key, future))

        client._handle_dynamic_output({"content": "<div class='writeln'>ok</div>"})

        assert future.done()
        assert future.result() == "<div class='writeln'>ok</div>"
        assert client._dynamic_output_cache_by_position[key] == "<div class='writeln'>ok</div>"

    @pytest.mark.asyncio
    async def test_handle_preview_response(self):
        client = IsabelleLSPClient()
        future = asyncio.get_running_loop().create_future()
        client._preview_waiters[("file:///tmp/Test.thy", 0)] = future

        client._handle_preview_response({
            "uri": "file:///tmp/Test.thy", "column": 0, "content": "<html>Preview</html>",
        })

        assert future.done()
        assert future.result()["content"] == "<html>Preview</html>"

    @pytest.mark.asyncio
    async def test_get_goals_uses_state_panel(self):
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
    async def test_get_goals_timeout_cleans_init_waiter(self):
        client = IsabelleLSPClient()
        client.notify = AsyncMock()

        with pytest.raises(IsabelleToolError, match="Timed out waiting for PIDE proof state"):
            await client.get_goals_at_position("/tmp/Test.thy", 7, 3, timeout=0.01)

        assert client._state_init_waiters == []

    @pytest.mark.asyncio
    async def test_dynamic_output_timeout_no_stale_data(self):
        client = IsabelleLSPClient()
        client.notify = AsyncMock()
        client._dynamic_output_cache_by_position[("/tmp/Other.thy", 1, 0)] = "old"

        result = await client.get_dynamic_output("/tmp/Test.thy", 1, timeout=0.01)
        assert result == ""

    @pytest.mark.asyncio
    async def test_dynamic_output_queries_are_serialized(self):
        client = IsabelleLSPClient()
        first_notify_entered = asyncio.Event()
        release_first = asyncio.Event()
        calls = []

        async def fake_notify(method, params):
            calls.append((method, params))
            if params["line"] == 1:
                first_notify_entered.set()
                await release_first.wait()
                client._handle_dynamic_output({"content": "first"})
            else:
                client._handle_dynamic_output({"content": "second"})

        client.notify = AsyncMock(side_effect=fake_notify)

        first = asyncio.create_task(client.get_dynamic_output("/tmp/Test.thy", 1, timeout=1))
        await asyncio.wait_for(first_notify_entered.wait(), timeout=1)

        second = asyncio.create_task(client.get_dynamic_output("/tmp/Test.thy", 2, timeout=1))
        await asyncio.sleep(0)
        assert [call[1]["line"] for call in calls] == [1]

        release_first.set()
        assert await first == "first"
        assert await second == "second"
        assert [call[1]["line"] for call in calls] == [1, 2]

    def test_fail_pending_waiters_fails_and_clears_all_waiters(self):
        client = IsabelleLSPClient()
        loop = asyncio.get_event_loop()
        request_future = loop.create_future()
        state_init_future = loop.create_future()
        state_output_future = loop.create_future()
        dynamic_future = loop.create_future()
        preview_future = loop.create_future()

        client.pending_requests[1] = request_future
        client._state_init_waiters.append(state_init_future)
        client._state_output_waiters[7] = state_output_future
        client._dynamic_output_waiters.append((("/tmp/Test.thy", 1, 0), dynamic_future))
        client._dynamic_output_cache_by_position[("/tmp/Test.thy", 1, 0)] = "stale"
        client._preview_waiters[("file:///tmp/Test.thy", 0)] = preview_future

        exc = IsabelleToolError("transport failed")
        client._fail_pending_waiters(exc)

        for future in (
            request_future,
            state_init_future,
            state_output_future,
            dynamic_future,
            preview_future,
        ):
            assert future.done()
            assert future.exception() is exc
        assert client.pending_requests == {}
        assert client._state_init_waiters == []
        assert client._state_output_waiters == {}
        assert client._dynamic_output_waiters == []
        assert client._dynamic_output_cache_by_position == {}
        assert client._preview_waiters == {}

    @pytest.mark.asyncio
    async def test_preview_requests_are_serialized(self):
        client = IsabelleLSPClient()
        first_notify_entered = asyncio.Event()
        release_first = asyncio.Event()
        calls = []

        async def fake_notify(method, params):
            calls.append((method, params))
            if params["column"] == 0:
                first_notify_entered.set()
                await release_first.wait()
                client._handle_preview_response({
                    "uri": "file:///tmp/Test.thy",
                    "column": 0,
                    "content": "first",
                })
            else:
                client._handle_preview_response({
                    "uri": "file:///tmp/Test.thy",
                    "column": 1,
                    "content": "second",
                })

        client.notify = AsyncMock(side_effect=fake_notify)

        first = asyncio.create_task(client.request_preview("/tmp/Test.thy", column=0, timeout=1))
        await asyncio.wait_for(first_notify_entered.wait(), timeout=1)

        second = asyncio.create_task(client.request_preview("/tmp/Test.thy", column=1, timeout=1))
        await asyncio.sleep(0)
        assert [call[1]["column"] for call in calls] == [0]

        release_first.set()
        assert (await first)["content"] == "first"
        assert (await second)["content"] == "second"
        assert [call[1]["column"] for call in calls] == [0, 1]

    @pytest.mark.asyncio
    async def test_preview_timeout_cleans_waiter(self):
        client = IsabelleLSPClient()
        client.notify = AsyncMock()

        with pytest.raises(IsabelleToolError, match="Timed out waiting for PIDE preview"):
            await client.request_preview("/tmp/Test.thy", timeout=0.01)

        assert client._preview_waiters == {}
