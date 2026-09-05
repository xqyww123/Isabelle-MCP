"""Tests for LSP client."""

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from isabelle_mcp.lsp_client import DocumentState, IsabelleLSPClient
from isabelle_mcp.utils import IsabelleToolError, LSPLine, MCPLine
from tests.conftest import full_decoration_entries

# Two real document versions (ids tick downward: NEWER is newer than OLDER).
OLDER = -19
NEWER = -40


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
        assert evaluation_state.active

        client = IsabelleLSPClient()  # process is None → shutdown skips teardown
        await client.shutdown()

        assert evaluation_state.active is False

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
    async def test_start_debug_adds_ml_debugger_option(self):
        # debug=True (design 2.1) spawns the server with -o ML_debugger=true;
        # the default spawns without it. Capture the launch cmd by raising
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
                await IsabelleLSPClient(debug=True).start()
            assert "ML_debugger=true" in captured["cmd"]

            with pytest.raises(RuntimeError):
                await IsabelleLSPClient().start()
            assert "ML_debugger=true" not in captured["cmd"]

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

    @staticmethod
    def _open(client: IsabelleLSPClient, path: str = "/test.thy") -> None:
        client.open_documents[path] = DocumentState(path, f"file://{path}", 1, "")

    @pytest.mark.asyncio
    async def test_a_folded_push_notifies_the_freshness_condition_after_the_tracker_is_filled(self):
        # Mutation control M-24: whoever wakes on the client-level condition
        # finds the tracker built and filled at the moment of the wake-up.
        client = IsabelleLSPClient()
        self._open(client)
        seen = []

        async def waiter():
            async with client.freshness.condition:
                await client.freshness.condition.wait()
                tracker = client._processing_trackers.get("/test.thy")
                seen.append(tracker.get_unprocessed_ranges() if tracker else None)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        await client._handle_notification("PIDE/decoration", {
            "uri": "file:///test.thy", "document_version": OLDER,
            "entries": full_decoration_entries(background_unprocessed1=[(3, 0, 9, 0)]),
        })
        await asyncio.wait_for(task, 1.0)
        assert seen == [[(3, 0, 9, 0)]]
        tracker = client._processing_trackers["/test.thy"]
        assert tracker.initialized and tracker.document_version == OLDER
        assert client.freshness.newest_document_version == OLDER

    @pytest.mark.asyncio
    async def test_a_push_for_a_file_not_open_builds_no_tracker_but_advances_the_version(self):
        client = IsabelleLSPClient()
        await client._handle_notification("PIDE/decoration", {
            "uri": "file:///test.thy", "document_version": NEWER,
            "entries": full_decoration_entries(),
        })
        assert "/test.thy" not in client._processing_trackers
        assert client.freshness.newest_document_version == NEWER

    @pytest.mark.asyncio
    async def test_a_stampless_push_is_dropped_and_never_freshens(self, caplog):
        # Mutation control M-18 from the client's side: after the handshake
        # the stamp is a required key; a push without it is dropped with one
        # ERROR line, so the file never becomes fresh.
        import logging
        client = IsabelleLSPClient()
        self._open(client)
        with caplog.at_level(logging.ERROR, logger="isabelle_mcp.lsp_client"):
            await client._handle_notification("PIDE/decoration", {
                "uri": "file:///test.thy", "entries": full_decoration_entries(),
            })
        assert "/test.thy" not in client._processing_trackers
        assert any("without document_version" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_newer_flush_reply_unfreshens_every_other_tracker(self):
        # Mutation controls M-1 / M-2: a flush reply advances the newest
        # version, and a picture stamped older than it is no longer fresh.
        client = _mock_process_client()
        self._open(client, "/a.thy")
        self._open(client, "/b.thy")
        for path in ("/a.thy", "/b.thy"):
            await client._handle_notification("PIDE/decoration", {
                "uri": f"file://{path}", "document_version": OLDER,
                "entries": full_decoration_entries(),
            })
        a, b = client._processing_trackers["/a.thy"], client._processing_trackers["/b.thy"]
        assert a.fresh and b.fresh
        await self._flush(client, {"document_version": NEWER, "changed_uris": []})
        assert client.freshness.newest_document_version == NEWER
        assert not a.fresh and not b.fresh
        await client._handle_notification("PIDE/decoration", {      # the ack for a
            "uri": "file:///a.thy", "document_version": NEWER, "entries": [],
        })
        assert a.fresh and not b.fresh

    @staticmethod
    async def _flush(client: IsabelleLSPClient, reply: dict, *, resync: bool = False):
        """client.flush against the mocked stdin: the reply is fed in once the
        request is pending."""
        async def answer():
            while not client.pending_requests:
                await asyncio.sleep(0)
            (req_id,) = client.pending_requests
            await client._handle_message({"jsonrpc": "2.0", "id": req_id, "result": reply})

        answering = asyncio.create_task(answer())
        result = await client.flush(resync_dependencies=resync)
        await answering
        return result

    @pytest.mark.asyncio
    async def test_a_flush_on_a_virgin_session_replies_zero_and_is_accepted(self):
        client = _mock_process_client()
        result, snapshot = await self._flush(client, {"document_version": 0, "changed_uris": []})
        assert result["document_version"] == 0 and snapshot == 0
        assert client.freshness.newest_document_version == 0
        assert not client.freshness.unflushed_content

    @pytest.mark.asyncio
    async def test_a_stampless_flush_reply_is_a_catastrophe(self):
        from isabelle_mcp.utils import IsabelleCatastrophe
        client = _mock_process_client()
        with pytest.raises(IsabelleCatastrophe, match="document_version"):
            await self._flush(client, {"changed_uris": []})

    @pytest.mark.asyncio
    async def test_a_flush_reply_marks_its_changed_dependencies_dirty_by_path(self, tmp_path):
        # The reply names URIs; the breakpoint registry wants real paths
        # (mark_dirty realpaths a string and never raises, so a URI would be a
        # garbage key and the mark lost silently).
        from isabelle_mcp import debugger
        client = _mock_process_client()
        dep = tmp_path / "Lib.ML"
        dep.write_text("val x = 1;")
        debugger.registry.pop_dirty()
        await self._flush(client, {
            "document_version": OLDER, "changed_uris": [dep.as_uri()],
        }, resync=True)
        assert debugger.registry.pop_dirty() == {os.path.realpath(dep)}

    @pytest.mark.asyncio
    async def test_out_of_order_flush_replies_leave_the_monotone_maximum(self):
        # Two flushes in flight: the counters reconcile by maximum, never by
        # assignment, so the later snapshot survives an earlier reply landing
        # second.
        client = _mock_process_client()
        client.freshness.content_sends = 3
        first = asyncio.create_task(client.flush(resync_dependencies=False))
        while len(client.pending_requests) < 1:
            await asyncio.sleep(0)
        client.freshness.content_sends = 5                     # a send between the two
        second = asyncio.create_task(client.flush(resync_dependencies=False))
        while len(client.pending_requests) < 2:
            await asyncio.sleep(0)
        ids = sorted(client.pending_requests)
        for req_id in reversed(ids):                          # the second reply first
            await client._handle_message({"jsonrpc": "2.0", "id": req_id,
                                          "result": {"document_version": OLDER, "changed_uris": []}})
        _, s1 = await first
        _, s2 = await second
        assert (s1, s2) == (3, 5)
        assert client.freshness.content_sends_flushed == 5

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
            await client.open_document(temp_file)
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
            await client.open_document(temp_file)
            v1 = client.open_documents[temp_file].version
            await client.open_document(temp_file)
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

        async def notify_cancel(method, params, **kw):
            if method == "textDocument/didOpen":
                raise asyncio.CancelledError()

        client.notify = notify_cancel

        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.thy', delete=False) as f:
            f.write("theory Test imports Main begin end")
            temp_file = f.name

        try:
            with pytest.raises(asyncio.CancelledError):
                await client.open_document(temp_file)
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
            await client.open_document(temp_file)
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
            await client.open_document(temp_file)
            client.diagnostic_cache.diagnostics[temp_file] = [{"message": "old"}]
            client.diagnostic_cache.last_update[temp_file] = time.time()

            await client.close_document(temp_file)

            assert temp_file not in client.diagnostic_cache.diagnostics
            assert temp_file not in client.diagnostic_cache.last_update
        finally:
            Path(temp_file).unlink()

    def test_diagnostics_cache(self):
        client = IsabelleLSPClient()
        diagnostics = [{"severity": 1, "message": "Error"}, {"severity": 2, "message": "Warning"}]
        client.diagnostic_cache.diagnostics["/test.thy"] = diagnostics
        assert client.get_cached_diagnostics("/test.thy") == diagnostics

    @pytest.mark.asyncio
    async def test_shutdown_clears_diagnostic_state_and_the_freshness_state(self):
        client = IsabelleLSPClient()
        client.diagnostic_cache.diagnostics["/test.thy"] = [{"message": "old"}]
        client.diagnostic_cache.last_update["/test.thy"] = time.time()
        client.freshness.advance(NEWER)
        client.freshness.content_sends = 2

        await client.shutdown()

        assert client.diagnostic_cache.diagnostics == {}
        assert client.diagnostic_cache.last_update == {}
        assert client.freshness.newest_document_version == 0
        assert client.freshness.content_sends == 0

    @pytest.mark.asyncio
    async def test_a_differential_push_is_folded_but_initializes_nothing(self):
        # Every push for an open document is folded; only a FULL push makes a
        # picture (I-5, enforced in the tracker whatever the call site does —
        # mutation control M-8). A slice folded into a blank tracker is never
        # served: the all-empty ghost that reads `processed` everywhere is not
        # representable.
        client = IsabelleLSPClient()
        self._open(client)
        await client._handle_notification("PIDE/decoration", {
            "uri": "file:///test.thy", "document_version": OLDER,
            "entries": [{"type": "background_bad", "content": []},
                        {"type": "background_sorry", "content": []}],
        })
        tracker = client._processing_trackers["/test.thy"]
        assert not tracker.initialized and not tracker.fresh
        # Once a full push has made the picture, a differential one updates it.
        await client._handle_notification("PIDE/decoration", {
            "uri": "file:///test.thy", "document_version": OLDER,
            "entries": full_decoration_entries(background_sorry=[(4, 2, 4, 7)]),
        })
        assert tracker.initialized and tracker.fresh
        await client._handle_notification("PIDE/decoration", {
            "uri": "file:///test.thy", "document_version": OLDER,
            "entries": [{"type": "background_sorry", "content": []}],
        })
        assert tracker.get_sorry_ranges() == []

    @pytest.mark.asyncio
    async def test_the_handshake_refuses_a_mismatched_protocol_version(self):
        # D-G (probe P-9's unit form): a jar without the field is version 0;
        # the launch is refused with the approved sentence, verbatim.
        from isabelle_mcp.lsp_client import PROTOCOL_MISMATCH_MESSAGE, PROTOCOL_VERSION
        client = IsabelleLSPClient()
        client.request = AsyncMock(return_value={"capabilities": {}})
        client.notify = AsyncMock()
        with pytest.raises(IsabelleToolError) as exc:
            await client.initialize()
        assert str(exc.value) == PROTOCOL_MISMATCH_MESSAGE == (
            "Isabelle-MCP's Scala component does not match this isabelle-mcp package. "
            "Run `isabelle-mcp install` to update it."
        )
        client.notify.assert_not_awaited()
        client.request = AsyncMock(return_value={
            "capabilities": {"x": 1}, "protocol_version": PROTOCOL_VERSION})
        await client.initialize()
        assert client.server_capabilities == {"x": 1}
        client.notify.assert_awaited_once()

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
    async def test_query_at_position_is_one_request_and_a_cancel(self):
        # No caret update, no panel, no sleep: one request carrying the token and
        # the backstop, and a cancel naming the same token on the way out.
        client = IsabelleLSPClient()
        client.open_documents["/tmp/Test.thy"] = DocumentState(
            file_path="/tmp/Test.thy", uri="file:///tmp/Test.thy", version=1, content="",
        )
        sent = []
        client.notify = AsyncMock(side_effect=lambda m, p: sent.append((m, p)))
        client.request = AsyncMock(return_value={
            "status": "ok", "comment": False, "forked": True, "content": "<pre>1. P</pre>",
        })

        reply = await client.get_proof_state_at_position("/tmp/Test.thy", LSPLine(7), 3)

        assert reply.status == "ok"
        assert reply.forked is True
        assert reply.content == "<pre>1. P</pre>"
        method, params = client.request.call_args[0]
        assert method == "PIDE/proof_state_at_position"
        assert params["position"] == {"line": 7, "character": 3}
        assert params["timeout"] == client.QUERY_BACKSTOP
        assert sent == [("PIDE/query_cancel", {"token": params["token"]})]

    @pytest.mark.asyncio
    async def test_query_at_position_cancels_even_when_the_request_fails(self):
        # The prover must not keep working on a query nobody will read.
        client = IsabelleLSPClient()
        client.open_documents["/tmp/Test.thy"] = DocumentState(
            file_path="/tmp/Test.thy", uri="file:///tmp/Test.thy", version=1, content="",
        )
        sent = []
        client.notify = AsyncMock(side_effect=lambda m, p: sent.append((m, p)))
        client.request = AsyncMock(side_effect=IsabelleToolError("boom"))

        with pytest.raises(IsabelleToolError):
            await client.get_proof_state_at_position("/tmp/Test.thy", LSPLine(7), 3)

        assert [m for m, _ in sent] == ["PIDE/query_cancel"]

    @pytest.mark.asyncio
    async def test_find_theorems_at_position_passes_its_arguments_through(self):
        client = IsabelleLSPClient()
        client.open_documents["/tmp/Test.thy"] = DocumentState(
            file_path="/tmp/Test.thy", uri="file:///tmp/Test.thy", version=1, content="",
        )
        client.notify = AsyncMock()
        client.request = AsyncMock(return_value={"status": "ok", "content": "<pre/>"})

        await client.get_find_theorems_at_position(
            "/tmp/Test.thy", LSPLine(7), 3, "name: foo", "5", "false",
        )

        _, params = client.request.call_args[0]
        assert params["query"] == "name: foo"
        assert params["limit"] == "5"
        # The prover reads allow_dups inverted: only the exact string "false"
        # removes duplicates.
        assert params["allow_dups"] == "false"

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
        await client.open_document(str(f))
        doc = client.open_documents[str(f)]
        assert doc.stat_sig is not None
        assert doc.stat_sig == _stat_sig(str(f))

    @pytest.mark.asyncio
    async def test_open_already_open_is_ensure_only(self, tmp_path):
        """Re-opening an open doc must NOT re-read disk or send didChange."""
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f))
        v1 = client.open_documents[str(f)].version
        f.write_text("theory Foo begin (*changed on disk*) end")
        client.notify = AsyncMock()
        await client.open_document(str(f))
        # No didChange, version unchanged, cached content still the OLD content.
        client.notify.assert_not_called()
        assert client.open_documents[str(f)].version == v1
        assert "changed on disk" not in client.open_documents[str(f)].content

    @pytest.mark.asyncio
    async def test_resync_detects_and_pushes_change(self, tmp_path):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo\nimports Main\nbegin\nend\n")
        await client.open_document(str(f))
        v1 = client.open_documents[str(f)].version
        f.write_text("theory Foo\nimports Main\nbegin\n(*v2*)\nend\n")
        client.notify = AsyncMock()
        await client.resync_changed_open_documents()
        client.notify.assert_called_once()
        method, params = client.notify.call_args[0]
        assert method == "textDocument/didChange"
        # The RANGED shape, pinned exactly: one minimal hunk, not the whole
        # document (a range-less didChange re-executes the entire file).
        assert params["contentChanges"] == [{
            "range": {"start": {"line": 3, "character": 0},
                      "end": {"line": 3, "character": 0}},
            "text": "(*v2*)\n",
        }]
        assert client.open_documents[str(f)].version == v1 + 1
        assert "(*v2*)" in client.open_documents[str(f)].content
        # Phase D wiring: the didChange that went out marked the file dirty
        # for breakpoint reconciliation.
        from isabelle_mcp import debugger
        assert str(f) in debugger.registry.pop_dirty()

    @pytest.mark.asyncio
    async def test_rejected_didchange_forces_full_text_recovery(self, tmp_path):
        """The server drops a didChange it cannot apply, with only a type=1 log
        message -- the client's model has already committed, so the divergence
        is silent.  The hook must force the next sync to push FULL text (a
        ranged diff against the server's unknown base would corrupt it)."""
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo\nbegin\nend\n")
        await client.open_document(str(f))
        doc = client.open_documents[str(f)]

        client._surface_server_message(
            {"type": 1, "message": "Failed to apply document change: Remove(...)"})
        assert doc.needs_full_sync is True
        assert doc.stat_sig is None

        # Content on disk is UNCHANGED, yet the recovery must still push.
        client.notify = AsyncMock()
        await client.resync_changed_open_documents()
        client.notify.assert_called_once()
        method, params = client.notify.call_args[0]
        assert method == "textDocument/didChange"
        assert params["contentChanges"] == [{"text": "theory Foo\nbegin\nend\n"}]
        assert doc.needs_full_sync is False
        assert doc.stat_sig is not None

    @pytest.mark.asyncio
    async def test_resync_uses_inequality_not_greater(self, tmp_path):
        """A backdated mtime with different content is still detected (!= not >)."""
        import os
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f))
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
        await client.open_document(str(f))
        os.utime(str(f), None)  # touch: new mtime, identical content
        client.notify = AsyncMock()
        from isabelle_mcp import debugger
        debugger.registry.pop_dirty()   # isolate from earlier tests
        await client.resync_changed_open_documents()
        client.notify.assert_not_called()
        assert client.open_documents[str(f)].stat_sig == _stat_sig(str(f))
        # No didChange, no dirty mark: marked iff an edit actually went out.
        assert str(f) not in debugger.registry.pop_dirty()

    @pytest.mark.asyncio
    async def test_resync_handles_deletion(self, tmp_path):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f))
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
        await client.open_document(str(thy))
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
        await client.open_document(str(link))
        assert os.path.realpath(str(link)) in client.open_documents
        # set_caret via the symlink path resolves to the same DocumentState (no error).
        await client.set_caret(str(link), LSPLine(0))



class TestContentCounting:
    """Every message that puts content into the server's document model —
    didOpen, didChange — is counted as unflushed content inside _send's write
    section (mutation controls M-12 / M-14 / M-15); a cancel reply advances
    the newest version instead. Otherwise freshness silently never engages
    and the stale-cache races return."""

    @pytest.mark.asyncio
    async def test_did_open_counts_one_content_send(self, tmp_path):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f))
        assert client.freshness.content_sends == 1
        assert client.freshness.unflushed_content

    @pytest.mark.asyncio
    async def test_sync_dirty_files_counts_only_on_change(self, tmp_path):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f))

        await client.sync_dirty_files({str(f)})   # content unchanged: no didChange
        assert client.freshness.content_sends == 1

        f.write_text("theory Foo begin (*v2*) end")
        await client.sync_dirty_files({str(f)})   # didChange sent
        assert client.freshness.content_sends == 2

    @pytest.mark.asyncio
    async def test_a_content_send_notifies_the_freshness_condition_last(self, tmp_path):
        # The notify is _send's LAST act, after the timed write: a parked
        # wait wakes to find the counter already raised.
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        seen = []

        async def waiter():
            async with client.freshness.condition:
                await client.freshness.condition.wait()
                seen.append(client.freshness.content_sends)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        await client.open_document(str(f))
        await asyncio.wait_for(task, 1.0)
        assert seen == [1]

    @pytest.mark.asyncio
    async def test_the_flush_snapshot_is_taken_in_the_write_section(self, tmp_path):
        """A didOpen still off-loop in sanitize_read when the flush request is
        written is NOT covered by that flush: counted after the snapshot, it
        stays unflushed until the next reply (mutation control M-15: a count
        taken at the call site before _send is awaited would let the wait
        return fresh with the didOpen in flight)."""
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        gate = asyncio.Event()
        real_to_thread = asyncio.to_thread

        async def slow_read(fn, *args):
            await gate.wait()                      # the didOpen is "in flight"
            return await real_to_thread(fn, *args)

        with patch("isabelle_mcp.lsp_client.asyncio.to_thread", slow_read):
            opening = asyncio.create_task(client.open_document(str(f)))
            await asyncio.sleep(0)
            flushing = asyncio.create_task(client.flush(resync_dependencies=False))
            while not client.pending_requests:
                await asyncio.sleep(0)
            gate.set()                             # the didOpen goes out now
            await opening
            (req_id,) = client.pending_requests
            await client._handle_message({"jsonrpc": "2.0", "id": req_id,
                                          "result": {"document_version": OLDER, "changed_uris": []}})
            _, snapshot = await flushing
        assert snapshot == 0 and client.freshness.content_sends == 1
        assert client.freshness.unflushed_content       # the didOpen is not covered

    @pytest.mark.asyncio
    async def test_a_send_written_before_the_flush_request_is_covered_by_its_reply(self, tmp_path):
        """The count happens INSIDE the write section, right after the bytes:
        a didChange whose write completed before the flush request's write is
        counted before the flush's snapshot and covered by its reply (mutation
        control M-14: a count at the call site after the send lands after the
        snapshot, and the send stays unflushed until the next reply)."""
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f))
        f.write_text("theory Foo begin (*v2*) end")
        gate = asyncio.Event()

        async def slow_drain():
            await gate.wait()                      # the didChange holds the write lock

        client.process.stdin.drain = slow_drain
        syncing = asyncio.create_task(client.sync_dirty_files({str(f)}))
        while not client._write_lock.locked():
            await asyncio.sleep(0)
        flushing = asyncio.create_task(client.flush(resync_dependencies=False))
        await asyncio.sleep(0)
        gate.set()                                 # the didChange's write section ends
        await syncing
        while not client.pending_requests:
            await asyncio.sleep(0)
        (req_id,) = client.pending_requests
        await client._handle_message({"jsonrpc": "2.0", "id": req_id,
                                      "result": {"document_version": OLDER, "changed_uris": []}})
        _, snapshot = await flushing
        assert snapshot == 2 and client.freshness.content_sends == 2
        assert not client.freshness.unflushed_content

    @pytest.mark.asyncio
    async def test_force_interrupt_advances_the_newest_version(self, tmp_path):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f))
        client.request = AsyncMock(               # PIDE/cancel_evaluation
            return_value={"outcome": "nothing_running", "document_version": NEWER})
        await client.force_interrupt()            # re-minted ids, emptied perspective
        assert client.freshness.newest_document_version == NEWER

    @pytest.mark.asyncio
    async def test_reopen_already_open_counts_nothing(self, tmp_path):
        """open_document on an already-open doc early-returns BEFORE the send —
        otherwise every tool call (each re-enters open_document) would leave
        content unflushed and a tight polling client could never see a fresh
        picture."""
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f))
        await client.open_document(str(f))
        assert client.freshness.content_sends == 1


class TestDebuggerRequestWrappers:
    """The section-7.1 wrappers pin the wire shape: method name, param keys,
    fresh correlation tokens, and the crashed fallback on a non-dict reply."""

    def _client(self):
        client = IsabelleLSPClient()
        client.request = AsyncMock(return_value={"status": "ok"})
        return client

    @pytest.mark.asyncio
    async def test_breakpoints_wire_shape(self, tmp_path):
        client = self._client()
        f = tmp_path / "Foo.thy"
        await client.debugger_breakpoints(str(f), timeout=12)
        method, params = client.request.call_args.args[:2]
        assert method == "PIDE/debugger_breakpoints"
        assert params["uri"].startswith("file://")
        assert params["uri"].endswith("/Foo.thy")
        assert params["timeout"] == 12.0
        assert isinstance(params["timeout"], float)
        assert set(params) == {"uri", "token", "timeout"}
        # Progress-monitored by default: no hard transport deadline.
        assert client.request.call_args.kwargs == {"timeout": None}

    @pytest.mark.asyncio
    async def test_toggle_wire_shape(self, tmp_path):
        client = self._client()
        f = tmp_path / "Foo.thy"
        await client.debugger_toggle_breakpoint(
            str(f), 4711, True, timeout=9, request_timeout=60.0)
        method, params = client.request.call_args.args[:2]
        assert method == "PIDE/debugger_toggle_breakpoint"
        assert params["serial"] == 4711
        assert params["state"] is True
        assert params["timeout"] == 9.0
        assert set(params) == {"uri", "serial", "state", "token", "timeout"}
        assert client.request.call_args.kwargs == {"timeout": 60.0}

    @pytest.mark.asyncio
    async def test_eval_wire_shape(self):
        client = self._client()
        await client.debugger_eval("worker-1", "n + 1", frame=2, timeout=5)
        method, params = client.request.call_args.args[:2]
        assert method == "PIDE/debugger_eval"
        assert params["thread"] == "worker-1"
        assert params["frame"] == 2
        assert params["expr"] == "n + 1"
        assert params["timeout"] == 5.0
        assert set(params) == {"token", "thread", "frame", "expr", "timeout"}

    @pytest.mark.asyncio
    async def test_print_vals_wire_shape(self):
        client = self._client()
        await client.debugger_print_vals("worker-1", timeout=7)
        method, params = client.request.call_args.args[:2]
        assert method == "PIDE/debugger_print_vals"
        assert params["frame"] == 0
        # No expr on the wire: the server realises locals through the eval verb.
        assert set(params) == {"token", "thread", "frame", "timeout"}

    @pytest.mark.asyncio
    async def test_abort_and_input_wire_shapes(self):
        client = self._client()
        await client.debugger_abort("worker-1")
        method, params = client.request.call_args.args[:2]
        assert (method, params) == ("PIDE/debugger_abort", {"thread": "worker-1"})

        await client.debugger_input("worker-1", ["continue"])
        method, params = client.request.call_args.args[:2]
        assert method == "PIDE/debugger_input"
        assert params == {"thread": "worker-1", "verbs": ["continue"]}

    @pytest.mark.asyncio
    async def test_tokens_are_fresh_and_shared_with_the_query_counter(self, tmp_path):
        # The debugger requests live in the same Scala-side handler table as
        # the position-explicit queries, so tokens must be unique ACROSS both.
        client = self._client()
        f = tmp_path / "Foo.thy"
        await client.debugger_breakpoints(str(f))
        t1 = client.request.call_args.args[1]["token"]
        await client.debugger_eval("worker-1", "1")
        t2 = client.request.call_args.args[1]["token"]
        assert t1 != t2
        assert client._query_seq == 2

    @pytest.mark.asyncio
    async def test_non_dict_reply_becomes_crashed(self):
        client = IsabelleLSPClient()
        client.request = AsyncMock(return_value=None)
        reply = await client.debugger_eval("worker-1", "1")
        assert reply == {"status": "crashed"}


class TestCloseAndReopen:
    """A closed document leaves no record behind: its tracker goes with it, a
    reopen sends a plain didOpen (counted as content), and the reopened file's
    picture is the full push the server sends at a version at least as new as
    the flush that covers the didOpen — whether or not the file was written
    meanwhile."""

    @pytest.fixture
    def client(self):
        client = IsabelleLSPClient()

        async def notify(method, params, **kw):
            if kw.get("content"):
                client.freshness.content_sends += 1

        client.notify = notify
        return client

    def _thy(self, tmp_path, text="theory T imports Main begin end\n") -> str:
        path = tmp_path / "T.thy"
        path.write_text(text)
        return str(path)

    @pytest.mark.asyncio
    async def test_a_close_notifies_the_freshness_condition(self, client, tmp_path):
        # A parked wait re-evaluates its wait set on the close's notify.
        path = self._thy(tmp_path)
        await client.open_document(path)
        woken = []

        async def waiter():
            async with client.freshness.condition:
                await client.freshness.condition.wait()
                woken.append(path in client.open_documents)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        await client.close_document(path)
        await asyncio.wait_for(task, 1.0)
        assert woken == [False]

    @pytest.mark.asyncio
    async def test_a_reopen_starts_from_no_picture(self, client, tmp_path):
        path = self._thy(tmp_path)
        await client.open_document(path)
        await client._handle_decoration({
            "uri": client.open_documents[path].uri, "document_version": OLDER,
            "entries": full_decoration_entries(),
        })
        tracker = client.get_processing_tracker(path)
        assert tracker.initialized and not tracker.fresh      # the didOpen is unflushed
        client.freshness.content_sends_flushed = client.freshness.content_sends
        assert tracker.fresh
        await client.close_document(path)
        await client.open_document(path)
        assert client.get_processing_tracker(path) is None
        assert client.freshness.content_sends == 2            # both didOpens counted
