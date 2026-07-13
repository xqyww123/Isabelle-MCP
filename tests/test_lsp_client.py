"""Tests for LSP client."""

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from isabelle_mcp.lsp_client import DocumentState, IsabelleLSPClient
from isabelle_mcp.utils import IsabelleToolError, LSPCharacter, LSPLine, MCPLine


@pytest.fixture(autouse=True)
def _pin_isabelle_version():
    # Tests must not depend on the host's installed Isabelle version. Pin the
    # cached version detector to a known pre-2025 value; individual tests that
    # care about a specific version override it in-body.
    import isabelle_mcp.lsp_client as lc
    saved = lc._isabelle_version_cache
    lc._isabelle_version_cache = ("Isabelle2024", 2024)
    yield
    lc._isabelle_version_cache = saved


def _status_result(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


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
    async def test_shutdown_resets_evaluation_state(self):
        # A terminate mid-evaluation must not leave the global eval singleton active,
        # else the next launched session rejects every evaluate_to.
        from isabelle_mcp.evaluation import evaluation_state

        evaluation_state.start("/tmp/x.thy", MCPLine(5))
        evaluation_state.auto_opened_files.add("/tmp/y.thy")
        assert evaluation_state.active

        client = IsabelleLSPClient()  # process is None → shutdown skips teardown
        await client.shutdown()

        assert evaluation_state.active is False
        assert evaluation_state.auto_opened_files == set()

    @pytest.mark.asyncio
    async def test_start_is_reentrant_noop_when_running(self):
        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.returncode = None  # "alive"
        with patch('asyncio.create_subprocess_exec') as spawn:
            await client.start()
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_html_output_version_gated(self):
        # vscode_html_output=true is required on 2025+ (the plain-text state panel is
        # broken upstream) but does NOT exist pre-2025 — passing it would abort the
        # server. Gate it on the detected major year. Capture the launch cmd by raising
        # right after it is built.
        import isabelle_mcp.lsp_client as lc
        captured = {}

        async def fake_exec(*cmd, **kw):
            captured["cmd"] = cmd
            raise RuntimeError("stop after cmd built")

        with patch("asyncio.create_subprocess_exec", fake_exec), \
                patch("isabelle_mcp.lsp_client.ensure_component"):
            lc._isabelle_version_cache = ("Isabelle2025-2", 2025)
            with pytest.raises(RuntimeError):
                await IsabelleLSPClient().start()
            assert "vscode_html_output=true" in captured["cmd"]

            lc._isabelle_version_cache = ("Isabelle2024", 2024)
            with pytest.raises(RuntimeError):
                await IsabelleLSPClient().start()
            assert "vscode_html_output=true" not in captured["cmd"]

    @pytest.mark.asyncio
    async def test_start_registers_the_component_before_spawning(self):
        # `isabelle mcp_server` only exists if our Scala component is registered, so the
        # registration must happen BEFORE the spawn — never leave a doomed process behind.
        with patch("asyncio.create_subprocess_exec") as spawn, \
                patch("isabelle_mcp.lsp_client.ensure_component",
                      side_effect=IsabelleToolError("Isabelle-MCP does not support Isabelle2024")):
            with pytest.raises(IsabelleToolError, match="does not support"):
                await IsabelleLSPClient().start()
        spawn.assert_not_called()

    def test_version_detector_reads_isabelle_version(self):
        # `isabelle version` is the single source for the version string AND the
        # major year used to branch version-specific protocol (state_init / unicode
        # option). It is probed once and cached for the process. (The autouse
        # fixture restores the cache afterwards.)
        import isabelle_mcp.lsp_client as lc
        lc._isabelle_version_cache = None
        run = MagicMock(return_value=MagicMock(stdout="Isabelle2025-2\n"))
        with patch("isabelle_mcp.lsp_client.subprocess.run", run):
            assert lc.isabelle_version() == "Isabelle2025-2"
            assert lc.isabelle_year() == 2025
        run.assert_called_once()  # cached: the second access does not re-probe

    def test_version_detector_unknown_on_failure(self):
        import isabelle_mcp.lsp_client as lc
        lc._isabelle_version_cache = None
        with patch("isabelle_mcp.lsp_client.subprocess.run", MagicMock(side_effect=OSError)):
            assert lc.isabelle_version() == "unknown"
            assert lc.isabelle_year() is None

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
    async def test_open_document_registers_before_didopen(self):
        # S2: registration must precede the didOpen send, so a cancel re-delivered at
        # the didOpen drain still leaves an open_documents entry — close_document can
        # then send the matching didClose instead of orphaning a server-opened doc.
        import asyncio

        from isabelle_mcp.lsp_client import _canon

        client = IsabelleLSPClient()
        client.process = MagicMock()
        client.process.stdin = MagicMock()
        client.process.stdin.write = MagicMock()
        client.process.stdin.drain = AsyncMock()

        async def notify_cancel(method, params):
            if method == "textDocument/didOpen":
                raise asyncio.CancelledError()

        client.notify = notify_cancel

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.thy', delete=False) as f:
            f.write("theory Test imports Main begin end")
            temp_file = f.name

        try:
            with pytest.raises(asyncio.CancelledError):
                await client.open_document(temp_file, wait_for_diagnostics=False)
            assert _canon(temp_file) in client.open_documents
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
    async def test_handle_state_output_resolves_init_waiter(self):
        client = IsabelleLSPClient()
        future = asyncio.get_running_loop().create_future()
        client._state_init_waiters.append(future)

        client._handle_state_output({"id": 42, "content": "<pre>1. P</pre>"})

        assert future.done()
        assert future.result() == (42, "<pre>1. P</pre>")
        assert client._state_init_waiters == []

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
    async def test_get_goals_uses_state_panel(self):
        client = IsabelleLSPClient()
        calls = []

        async def fake_notify(method, params):
            calls.append((method, params))
            if method == "PIDE/state_init":
                client._handle_state_output({"id": 99, "content": "<pre>1. P</pre>"})

        client.notify = AsyncMock(side_effect=fake_notify)
        goals = await client.get_goals_at_position("/tmp/Test.thy", LSPLine(7), 3)

        assert goals == ["P"]
        assert calls[0][0] == "PIDE/caret_update"
        assert calls[1] == ("PIDE/state_init", {})
        assert calls[-1] == ("PIDE/state_exit", {"id": 99})

    @pytest.mark.asyncio
    async def test_get_goals_2025_uses_state_init_request(self):
        # Isabelle2025 made PIDE/state_init a request (replies with the panel's
        # state_id). The panel still pushes state_output as a notification, which
        # the waiter captures — but the panel is only created if state_init is sent
        # as a *request*. Sent as a notification (pre-2025 path), 2025 returns no
        # goals (the bug this branch fixes).
        import isabelle_mcp.lsp_client as lc
        lc._isabelle_version_cache = ("Isabelle2025-2", 2025)
        client = IsabelleLSPClient()
        calls = []

        async def fake_notify(method, params):
            calls.append((method, params))

        async def fake_request(method, params, timeout=None):
            calls.append(("REQUEST", method, params))
            if method == "PIDE/state_init":
                client._handle_state_output({"id": 7, "content": "<pre>1. Q</pre>"})
            return {"state_id": 7}

        client.notify = AsyncMock(side_effect=fake_notify)
        client.request = AsyncMock(side_effect=fake_request)
        goals = await client.get_goals_at_position("/tmp/Test.thy", LSPLine(7), 3)

        assert goals == ["Q"]
        # state_init went out as a REQUEST, never as a notification.
        assert ("REQUEST", "PIDE/state_init", {}) in calls
        assert ("PIDE/state_init", {}) not in calls
        assert calls[-1] == ("PIDE/state_exit", {"id": 7})

    @pytest.mark.asyncio
    async def test_get_goals_timeout_cleans_init_waiter(self):
        client = IsabelleLSPClient()
        client.notify = AsyncMock()
        client.STALL_TIMEOUT = 0.01
        client.STATE_OUTPUT_GRACE = 0.01
        client.PROGRESS_CHECK_INTERVAL = 0.01
        client._last_server_activity = time.time() - 1.0

        with pytest.raises(IsabelleToolError):
            await client.get_goals_at_position("/tmp/Test.thy", LSPLine(7), 3)

        assert client._state_init_waiters == []

    @pytest.mark.asyncio
    async def test_dynamic_output_timeout_no_stale_data(self):
        client = IsabelleLSPClient()
        client.notify = AsyncMock()
        client.PROGRESS_CHECK_INTERVAL = 0.01
        client.diagnostic_cache.last_update["/tmp/Test.thy"] = time.time() - 10.0
        client._dynamic_output_cache_by_position[("/tmp/Other.thy", 1, 0)] = "old"

        result = await client.get_dynamic_output("/tmp/Test.thy", LSPLine(1))
        assert result == ""

    @pytest.mark.asyncio
    async def test_dynamic_output_queries_are_serialized(self):
        client = IsabelleLSPClient()
        first_notify_entered = asyncio.Event()
        release_first = asyncio.Event()
        calls = []

        async def fake_notify(method, params):
            calls.append((method, params))
            if params.get("line") == 1:
                first_notify_entered.set()
                await release_first.wait()
                client._handle_dynamic_output({"content": "first"})
            elif params.get("line") == 2:
                client._handle_dynamic_output({"content": "second"})

        client.notify = AsyncMock(side_effect=fake_notify)

        first = asyncio.create_task(client.get_dynamic_output("/tmp/Test.thy", LSPLine(1)))
        await asyncio.wait_for(first_notify_entered.wait(), timeout=1)

        second = asyncio.create_task(client.get_dynamic_output("/tmp/Test.thy", LSPLine(2)))
        await asyncio.sleep(0)
        assert [call[1]["line"] for call in calls if "line" in call[1]] == [1]

        release_first.set()
        assert await first == "first"
        assert await second == "second"
        assert [call[1]["line"] for call in calls if "line" in call[1]] == [1, 2]

    async def test_fail_pending_waiters_fails_and_clears_all_waiters(self):
        # async (not sync + get_event_loop): pytest-asyncio >= 1.x clears the
        # event loop after every async test, so a later sync get_event_loop()
        # raises "There is no current event loop" on Python 3.12+.
        client = IsabelleLSPClient()
        loop = asyncio.get_running_loop()
        request_future = loop.create_future()
        state_init_future = loop.create_future()
        dynamic_future = loop.create_future()
        preview_future = loop.create_future()

        client.pending_requests[1] = request_future
        client._state_init_waiters.append(state_init_future)
        client._dynamic_output_waiters.append((("/tmp/Test.thy", 1, 0), dynamic_future))
        client._dynamic_output_cache_by_position[("/tmp/Test.thy", 1, 0)] = "stale"
        client._preview_waiters[("file:///tmp/Test.thy", 0)] = preview_future

        exc = IsabelleToolError("transport failed")
        client._fail_pending_waiters(exc)

        for future in (
            request_future,
            state_init_future,
            dynamic_future,
            preview_future,
        ):
            assert future.done()
            assert future.exception() is exc
        assert client.pending_requests == {}
        assert client._state_init_waiters == []
        assert client._dynamic_output_waiters == []
        assert client._dynamic_output_cache_by_position == {}
        assert client._preview_waiters == {}


def _mock_process_client() -> IsabelleLSPClient:
    client = IsabelleLSPClient()
    client.process = MagicMock()
    client.process.stdin = MagicMock()
    client.process.stdin.write = MagicMock()
    client.process.stdin.drain = AsyncMock()
    return client


class TestPreHandshakeFailFast:
    """Pre-handshake type-1 server messages must fail the pending initialize
    immediately (with `vscode_server -n`, a missing heap wedges the server
    before it ever answers initialize — the message is the only signal)."""

    def _client(self) -> IsabelleLSPClient:
        return IsabelleLSPClient(logic="Minilang", session_dirs=["/proj"])

    @pytest.mark.asyncio
    async def test_type1_before_handshake_fails_pending_request(self):
        client = self._client()
        fut = asyncio.get_running_loop().create_future()
        client.pending_requests[1] = fut
        client._surface_server_message(
            {"type": 1, "message": 'Missing heap image for session "X"'})
        assert client.startup_errors == ['Missing heap image for session "X"']
        with pytest.raises(IsabelleToolError) as exc_info:
            fut.result()
        msg = str(exc_info.value)
        assert 'Missing heap image for session "X"' in msg
        # The fix command: options BEFORE the session name (Isabelle stops
        # option parsing at the first positional argument).
        assert "isabelle build -b -d /proj Minilang" in msg

    @pytest.mark.asyncio
    async def test_type1_after_handshake_only_logs(self):
        client = self._client()
        client._handshake_done = True
        fut = asyncio.get_running_loop().create_future()
        client.pending_requests[1] = fut
        client._surface_server_message({"type": 1, "message": "later error"})
        assert not fut.done()
        assert client.startup_errors == []
        fut.cancel()

    @pytest.mark.asyncio
    async def test_non_error_types_never_fail_requests(self):
        client = self._client()
        fut = asyncio.get_running_loop().create_future()
        client.pending_requests[1] = fut
        client._surface_server_message({"type": 3, "message": "Welcome to Isabelle"})
        client._surface_server_message({"type": 2, "message": "some warning"})
        assert not fut.done()
        assert client.startup_errors == []
        fut.cancel()

    @pytest.mark.asyncio
    async def test_done_futures_left_untouched(self):
        # A future already resolved (or cancelled by wait_for) must not get
        # set_exception → no InvalidStateError, no never-retrieved warning.
        client = self._client()
        fut = asyncio.get_running_loop().create_future()
        fut.set_result("done")
        client.pending_requests[1] = fut
        client._surface_server_message({"type": 1, "message": "late error"})
        assert fut.result() == "done"

    @pytest.mark.asyncio
    async def test_initialize_timeout_attaches_startup_errors(self):
        # Blind timeout (the type-1 raced past the request) → the buffered
        # server message is appended so the error is never just "timed out".
        client = self._client()
        client.startup_errors = ['Missing heap image for session "X"']

        async def fake_request(method, params, timeout=None):
            try:
                raise asyncio.TimeoutError
            except asyncio.TimeoutError as exc:
                raise IsabelleToolError(
                    "LSP request 'initialize' timed out after 30.0s") from exc

        client.request = fake_request
        with pytest.raises(IsabelleToolError, match="Missing heap image"):
            await client.initialize()

    @pytest.mark.asyncio
    async def test_initialize_other_errors_pass_through_unchanged(self):
        # A JSON-RPC error reply ("Undefined session(s)") is self-explanatory;
        # appending heap hints there would mislead.
        client = self._client()
        client.startup_errors = ["unrelated noise"]

        async def fake_request(method, params, timeout=None):
            raise IsabelleToolError('Undefined session(s): "Nope"')

        client.request = fake_request
        with pytest.raises(IsabelleToolError) as exc_info:
            await client.initialize()
        assert "unrelated noise" not in str(exc_info.value)
        assert "Undefined session" in str(exc_info.value)


class TestStatSigAndResync:
    @pytest.mark.asyncio
    async def test_open_records_stat_sig(self, tmp_path):
        from isabelle_mcp.lsp_client import _stat_sig
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f), wait_for_diagnostics=False)
        doc = client.open_documents[str(f)]
        assert doc.stat_sig is not None
        assert doc.stat_sig == _stat_sig(str(f))

    @pytest.mark.asyncio
    async def test_open_already_open_is_ensure_only(self, tmp_path):
        """Re-opening an open doc must NOT re-read disk or send didChange."""
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f), wait_for_diagnostics=False)
        v1 = client.open_documents[str(f)].version
        f.write_text("theory Foo begin (*changed on disk*) end")
        client.notify = AsyncMock()
        await client.open_document(str(f), wait_for_diagnostics=False)
        # No didChange, version unchanged, cached content still the OLD content.
        client.notify.assert_not_called()
        assert client.open_documents[str(f)].version == v1
        assert "changed on disk" not in client.open_documents[str(f)].content

    @pytest.mark.asyncio
    async def test_resync_detects_and_pushes_change(self, tmp_path):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f), wait_for_diagnostics=False)
        v1 = client.open_documents[str(f)].version
        f.write_text("theory Foo begin (*v2*) end")
        client.notify = AsyncMock()
        await client.resync_changed_open_documents()
        client.notify.assert_called_once()
        method, params = client.notify.call_args[0]
        assert method == "textDocument/didChange"
        assert "v2" in params["contentChanges"][0]["text"]
        assert client.open_documents[str(f)].version == v1 + 1

    @pytest.mark.asyncio
    async def test_resync_uses_inequality_not_greater(self, tmp_path):
        """A backdated mtime with different content is still detected (!= not >)."""
        import os
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f), wait_for_diagnostics=False)
        f.write_text("theory Foo begin (*older-but-different*) end")
        os.utime(str(f), (1_000_000.0, 1_000_000.0))  # mtime far in the PAST
        client.notify = AsyncMock()
        await client.resync_changed_open_documents()
        client.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_resync_identical_content_sends_nothing(self, tmp_path):
        """A bare metadata touch (same bytes) refreshes stat_sig but sends no didChange."""
        import os
        from isabelle_mcp.lsp_client import _stat_sig
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f), wait_for_diagnostics=False)
        os.utime(str(f), None)  # touch: new mtime, identical content
        client.notify = AsyncMock()
        await client.resync_changed_open_documents()
        client.notify.assert_not_called()
        assert client.open_documents[str(f)].stat_sig == _stat_sig(str(f))

    @pytest.mark.asyncio
    async def test_resync_handles_deletion(self, tmp_path):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f), wait_for_diagnostics=False)
        f.unlink()
        client.notify = AsyncMock()
        await client.resync_changed_open_documents()  # must not raise
        client.notify.assert_not_called()
        assert client.open_documents[str(f)].stat_sig is None

    @pytest.mark.asyncio
    async def test_dirty_ml_does_not_force_resync_open_thy(self, tmp_path):
        """The removed '.ML changed → re-sync all open .thy' behavior must be gone."""
        client = _mock_process_client()
        thy = tmp_path / "Foo.thy"
        thy.write_text("theory Foo begin end")
        await client.open_document(str(thy), wait_for_diagnostics=False)
        ml = tmp_path / "Helper.ML"          # a dependency, NOT in open_documents
        ml.write_text("val x = 1;")
        client.notify = AsyncMock()
        await client.sync_dirty_files({str(ml)})
        client.notify.assert_not_called()    # open .thy is NOT force-resynced

    @pytest.mark.asyncio
    async def test_open_document_realpath_keying(self, tmp_path):
        """open_documents is keyed by realpath; a symlinked path resolves to it."""
        import os
        client = _mock_process_client()
        real = tmp_path / "Foo.thy"
        real.write_text("theory Foo begin end")
        link = tmp_path / "Link.thy"
        os.symlink(real, link)
        await client.open_document(str(link), wait_for_diagnostics=False)
        assert os.path.realpath(str(link)) in client.open_documents
        # set_caret via the symlink path resolves to the same DocumentState (no error).
        await client.set_caret(str(link), LSPLine(0))



class TestEditStampWiring:
    """Every path that puts content into the server's document model must bump
    the global edit clock (processing.note_edit_sent) — otherwise the freshness
    gate silently never engages and the pre-0.1.1 stale-cache races return."""

    @staticmethod
    def _reset_clock(monkeypatch):
        from isabelle_mcp import processing
        monkeypatch.setattr(processing, "_last_edit_sent", float("-inf"))

    @staticmethod
    def _clock_running() -> bool:
        from isabelle_mcp import processing
        return processing._grace_remaining() > 0.0

    @pytest.mark.asyncio
    async def test_did_open_bumps_edit_clock(self, tmp_path, monkeypatch):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        self._reset_clock(monkeypatch)
        await client.open_document(str(f), wait_for_diagnostics=False)
        assert self._clock_running()

    @pytest.mark.asyncio
    async def test_sync_dirty_files_bumps_edit_clock_only_on_change(
        self, tmp_path, monkeypatch,
    ):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f), wait_for_diagnostics=False)

        self._reset_clock(monkeypatch)
        await client.sync_dirty_files({str(f)})   # content unchanged: no didChange
        assert not self._clock_running()

        f.write_text("theory Foo begin (*v2*) end")
        await client.sync_dirty_files({str(f)})   # didChange sent
        assert self._clock_running()

    @pytest.mark.asyncio
    async def test_force_interrupt_bumps_edit_clock(self, tmp_path, monkeypatch):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f), wait_for_diagnostics=False)
        client.request = AsyncMock()              # PIDE/cancel_execution
        self._reset_clock(monkeypatch)
        await client.force_interrupt(str(f))      # synthetic didChange
        assert self._clock_running()

    @pytest.mark.asyncio
    async def test_reopen_already_open_does_not_bump(self, tmp_path, monkeypatch):
        """open_document on an already-open doc early-returns BEFORE the bump —
        otherwise every tool call (each re-enters open_document) would re-arm
        the grace and a tight polling client could never see a fresh cache."""
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f), wait_for_diagnostics=False)
        self._reset_clock(monkeypatch)
        await client.open_document(str(f), wait_for_diagnostics=False)
        assert not self._clock_running()
