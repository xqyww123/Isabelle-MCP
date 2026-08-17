"""Tests for MCP server tool wrappers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from isabelle_mcp.server import (
    isabelle_cancel_evaluation,
    isabelle_command_output,
    isabelle_definition,
    isabelle_evaluate_to,
    isabelle_evaluation_status,
    isabelle_goal,
    isabelle_hover,
    isabelle_launch,
    isabelle_local_occurrences,
    isabelle_session_info,
    isabelle_terminate,
)


def _patch_ensure(mock_client):
    return patch('isabelle_mcp.server._ensure_lsp_started', new_callable=AsyncMock, return_value=mock_client)


def _yaml(result):
    """The model-shaped tools answer with one YAML text block; parse it back."""
    return yaml.safe_load(result.content[0].text)


class TestMCPServerTools:
    @pytest.mark.asyncio
    async def test_hover(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.hover_response = {"contents": "test"}
        with _patch_ensure(mock_lsp_client):
            data = _yaml(await isabelle_hover(temp_theory_file, 5, "my_const"))
        assert len(data["results"]) >= 1
        assert data["results"][0]["info"] == "test"
        assert data["symbol"] == "my_const"

    @pytest.mark.asyncio
    async def test_definition(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.definition_response = []
        with _patch_ensure(mock_lsp_client):
            data = _yaml(await isabelle_definition(temp_theory_file, 8, "my_const"))
        assert data["locations"] == []
        assert data["symbol"] == "my_const"

    @pytest.mark.asyncio
    async def test_local_occurrences(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.highlights_response = []
        with _patch_ensure(mock_lsp_client):
            data = _yaml(await isabelle_local_occurrences(temp_theory_file, 8, "my_const"))
        assert data["occurrences"] == []

    @pytest.mark.asyncio
    async def test_goal_without_after_text(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.command_at_position_response = (
            "by simp", {"start": {"line": 8, "character": 2}, "end": {"line": 8, "character": 9}},
        )
        with _patch_ensure(mock_lsp_client):
            data = _yaml(await isabelle_goal(temp_theory_file, 9))
        assert data["subgoals"] == []
        assert data["command"] is not None
        assert data["command"]["text"] == "by simp"

    @pytest.mark.asyncio
    async def test_goal_with_after_text(self, temp_theory_file, mock_lsp_client):
        # Line 9 is "  by (simp add: my_const_def)"
        mock_lsp_client.command_at_position_response = (
            "by simp", {"start": {"line": 8, "character": 2}, "end": {"line": 8, "character": 9}},
        )
        with _patch_ensure(mock_lsp_client):
            data = _yaml(await isabelle_goal(temp_theory_file, 9, after_text="by"))
        assert data["subgoals"] == []
        assert data["command"] is not None

    @pytest.mark.asyncio
    async def test_command_output(self, temp_theory_file, mock_lsp_client):
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_command_output(temp_theory_file, 8)
        # Returns a ToolResult carrying the formatted plain-text block.
        assert result.content[0].text == "No command at line 8."

    @pytest.mark.asyncio
    async def test_session_info(self, mock_lsp_client):
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_session_info()
        assert _yaml(result)["current_session"] == "HOL"

    @pytest.mark.asyncio
    async def test_evaluate_to(self, temp_theory_file, mock_lsp_client):
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_evaluate_to(temp_theory_file, 5)
        assert "complete" in result.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_evaluation_status_no_eval(self, mock_lsp_client):
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_evaluation_status()
        assert result.content[0].text == "No evaluation in progress."

    @pytest.mark.asyncio
    async def test_cancel_evaluation_no_eval(self, mock_lsp_client):
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_cancel_evaluation()
        assert result.content[0].text == "No evaluation in progress."


class TestServerLifespan:
    @pytest.mark.asyncio
    async def test_creates_client(self):
        import isabelle_mcp.server as server_mod
        from isabelle_mcp.server import server_lifespan

        with patch('isabelle_mcp.server.IsabelleLSPClient') as MockClient:
            mock_instance = MagicMock()
            mock_instance.process = None
            MockClient.return_value = mock_instance
            async with server_lifespan(MagicMock()):
                assert server_mod._lsp_client is mock_instance
                mock_instance.start.assert_not_called()

    @pytest.mark.asyncio
    async def test_passes_extra_args_and_cwd_root(self):
        import os

        import isabelle_mcp.server as server_mod
        from isabelle_mcp.server import server_lifespan

        server_mod._server_extra_args = ["-o", "threads=4"]
        try:
            with patch('isabelle_mcp.server.IsabelleLSPClient') as MockClient:
                mock_instance = MagicMock()
                mock_instance.process = None
                MockClient.return_value = mock_instance
                async with server_lifespan(MagicMock()):
                    # No logic= (session is chosen at run time via isabelle_launch);
                    # project_root is the server's cwd.
                    MockClient.assert_called_with(
                        extra_args=["-o", "threads=4"],
                        project_root=os.path.realpath(os.getcwd()),
                    )
        finally:
            server_mod._server_extra_args = []

class TestHookRetirement:
    def test_notify_file_change_route_removed(self):
        import isabelle_mcp.server as server_mod
        assert not hasattr(server_mod, "notify_file_change")

    def test_no_periodic_sync_loop(self):
        import isabelle_mcp.server as server_mod
        assert not hasattr(server_mod, "_periodic_sync_loop")
        assert not hasattr(server_mod, "SYNC_INTERVAL")
        assert not hasattr(server_mod, "_sync_task")

    def test_lifespan_wires_watcher_sink(self):
        import isabelle_mcp.server as server_mod
        # The event-driven sink the FileWatcher schedules on every relevant edit.
        assert callable(server_mod._file_change_sink)


class TestServerMain:
    def test_version(self):
        import sys

        from isabelle_mcp.server import main
        with patch.object(sys, 'argv', ['isabelle-mcp', '--version']):
            with patch('builtins.print') as mock_print:
                main()
                assert "version" in mock_print.call_args[0][0].lower()

    def test_run(self):
        import sys

        from isabelle_mcp.server import main, mcp
        with patch.object(sys, 'argv', ['isabelle-mcp']):
            with patch.object(mcp, 'run') as mock_run:
                main()
                # stdio: run() called with no transport kwargs.
                mock_run.assert_called_once_with()

    def test_extra_args_passthrough(self):
        import sys

        import isabelle_mcp.server as server_mod
        from isabelle_mcp.server import main, mcp
        with patch.object(sys, 'argv', ['isabelle-mcp', '--', '-d', '/extra', '-o', 'threads=4']):
            with patch.object(mcp, 'run'):
                main()
                assert server_mod._server_extra_args == ["-d", "/extra", "-o", "threads=4"]

    def test_typo_rejected(self):
        import sys

        from isabelle_mcp.server import main
        with patch.object(sys, 'argv', ['isabelle-mcp', '--nope']):
            with pytest.raises(SystemExit, match="2"):
                main()


def _launch_mock(*, running: bool, logic: str = "HOL"):
    """A mock IsabelleLSPClient for the launch/terminate tests."""
    client = MagicMock()
    client.process = MagicMock() if running else None
    if running:
        client.process.returncode = None  # the no-op path checks liveness
    client.logic = logic
    # Explicit: the launch identity check compares this against the requested
    # debug value; a MagicMock auto-attribute would never equal False.
    client.debug = False
    client.extra_args = []
    client.isabelle_version = "Isabelle2024"
    client.start = AsyncMock()
    client.shutdown = AsyncMock()
    client.reap = AsyncMock()
    client.kill = MagicMock()
    client.enumerate_heap_sources = AsyncMock()
    # Explicit build-status verdict: the staleness gate must be skipped on the
    # success path by intent, not by MagicMock-auto-attribute accident.
    client.heap_built = True
    client.unfinished_sessions = []
    client.build_hint = MagicMock(return_value="isabelle build -b HOL")
    return client


class TestSessionManagement:
    @pytest.mark.asyncio
    async def test_ensure_raises_before_launch(self):
        import isabelle_mcp.server as server_mod
        from isabelle_mcp.utils import IsabelleToolError

        client = _launch_mock(running=False)
        with patch.object(server_mod, '_lsp_client', client):
            with pytest.raises(IsabelleToolError, match="isabelle_launch"):
                await server_mod._ensure_lsp_started()

    @pytest.mark.asyncio
    async def test_launch_starts_prover_with_default_dirs(self):
        import isabelle_mcp.server as server_mod

        client = _launch_mock(running=False)
        with patch.object(server_mod, '_lsp_client', client), \
                patch.object(server_mod, '_default_session_dirs', return_value=["/root"]):
            result = await isabelle_launch("HOL")
        client.start.assert_awaited_once()
        client.shutdown.assert_not_awaited()
        assert client.logic == "HOL"
        assert client.session_dirs == ["/root"]
        assert _yaml(result)["current_session"] == "HOL"
        assert _yaml(result)["version"] == "Isabelle2024"

    def test_default_session_dirs_with_root(self, tmp_path):
        import os

        import isabelle_mcp.server as server_mod

        root = os.path.realpath(str(tmp_path))
        (tmp_path / "ROOTS").write_text("contrib\n")
        with patch('isabelle_mcp.server.os.getcwd', return_value=root):
            assert server_mod._default_session_dirs() == [root]

    def test_default_session_dirs_without_root(self, tmp_path):
        import os

        import isabelle_mcp.server as server_mod

        root = os.path.realpath(str(tmp_path))
        with patch('isabelle_mcp.server.os.getcwd', return_value=root):
            # isabelle rejects a -d dir without ROOT/ROOTS, so default to none.
            assert server_mod._default_session_dirs() == []

    @pytest.mark.asyncio
    async def test_launch_explicit_session_dirs(self):
        import isabelle_mcp.server as server_mod

        client = _launch_mock(running=False)
        with patch.object(server_mod, '_lsp_client', client):
            await isabelle_launch("Minilang", session_dirs=["/proj"])
        assert client.session_dirs == ["/proj"]

    @pytest.mark.asyncio
    async def test_launch_idempotent_same_session(self):
        import isabelle_mcp.server as server_mod

        client = _launch_mock(running=True, logic="HOL")
        with patch.object(server_mod, '_lsp_client', client):
            result = await isabelle_launch("HOL")
        client.start.assert_not_awaited()
        client.shutdown.assert_not_awaited()
        assert _yaml(result)["current_session"] == "HOL"

    @pytest.mark.asyncio
    async def test_launch_debug_mismatch_errors_both_ways(self):
        # The launch identity is (session, debug): same session, differing
        # debug value → error, no restart, in BOTH directions — a routine
        # launch must never silently kill a running debug session.
        import isabelle_mcp.server as server_mod
        from isabelle_mcp.utils import IsabelleToolError

        client = _launch_mock(running=True, logic="HOL")
        with patch.object(server_mod, '_lsp_client', client):
            with pytest.raises(IsabelleToolError, match="isabelle_terminate"):
                await isabelle_launch("HOL", debug=True)
        client.shutdown.assert_not_awaited()
        client.start.assert_not_awaited()

        client = _launch_mock(running=True, logic="HOL")
        client.debug = True
        with patch.object(server_mod, '_lsp_client', client):
            with pytest.raises(IsabelleToolError, match="isabelle_terminate"):
                await isabelle_launch("HOL", debug=False)
        client.shutdown.assert_not_awaited()
        client.start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_launch_idempotent_same_session_same_debug(self):
        import isabelle_mcp.server as server_mod

        client = _launch_mock(running=True, logic="HOL")
        client.debug = True
        with patch.object(server_mod, '_lsp_client', client):
            result = await isabelle_launch("HOL", debug=True)
        client.start.assert_not_awaited()
        assert _yaml(result)["debug"] is True

    @pytest.mark.asyncio
    async def test_launch_different_session_applies_debug(self):
        # A different session name already implies a relaunch; debug simply
        # applies to the new prover — no identity error.
        import isabelle_mcp.server as server_mod

        client = _launch_mock(running=True, logic="HOL")
        with patch.object(server_mod, '_lsp_client', client):
            result = await isabelle_launch("HOL-Analysis", debug=True)
        client.shutdown.assert_awaited_once()
        client.start.assert_awaited_once()
        assert client.debug is True
        assert _yaml(result)["debug"] is True

    @pytest.mark.asyncio
    async def test_launch_switches_session_restarts(self):
        import isabelle_mcp.server as server_mod

        client = _launch_mock(running=True, logic="HOL")
        with patch.object(server_mod, '_lsp_client', client):
            await isabelle_launch("HOL-Analysis")
        client.shutdown.assert_awaited_once()
        client.start.assert_awaited_once()
        assert client.logic == "HOL-Analysis"

    @pytest.mark.asyncio
    async def test_launch_restarts_crashed_server(self):
        # A lingering process object with a returncode is a CRASHED server,
        # not a running one: the same-session no-op must not report success.
        import isabelle_mcp.server as server_mod

        client = _launch_mock(running=True, logic="HOL")
        client.process.returncode = 1
        with patch.object(server_mod, '_lsp_client', client):
            await isabelle_launch("HOL")
        client.shutdown.assert_awaited_once()
        client.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_launch_rejects_unverified_heap(self):
        # Probe verdict heap_built=False (outdated/missing/no build record)
        # → launch refuses, names the unfinished sessions, gives the build
        # command, and tears the just-started server down.
        import isabelle_mcp.server as server_mod
        from isabelle_mcp.utils import IsabelleToolError

        client = _launch_mock(running=False)
        client.heap_built = False
        client.unfinished_sessions = ["HOL-Library", "Minilang"]
        with patch.object(server_mod, '_lsp_client', client):
            with pytest.raises(IsabelleToolError) as exc_info:
                await isabelle_launch("Minilang", session_dirs=["/proj"])
        assert "HOL-Library, Minilang" in str(exc_info.value)
        assert "isabelle build -b HOL" in str(exc_info.value)
        client.kill.assert_called_once()
        client.reap.assert_awaited_once()
        assert client.process is None

    @pytest.mark.asyncio
    async def test_launch_unverified_heap_unnamed_fallback(self):
        # No "Unfinished session(s)" line parsed → generic wording, no crash.
        import isabelle_mcp.server as server_mod
        from isabelle_mcp.utils import IsabelleToolError

        client = _launch_mock(running=False)
        client.heap_built = False
        with patch.object(server_mod, '_lsp_client', client):
            with pytest.raises(IsabelleToolError, match="dependency chain"):
                await isabelle_launch("HOL")

    @pytest.mark.asyncio
    async def test_launch_heap_gate_bypassed_for_requirements_mode(self):
        # -R/-A in the server's extra args run the logic on its requirements
        # heaps; the session itself need not be built → gate must not fire.
        import isabelle_mcp.server as server_mod

        client = _launch_mock(running=False)
        client.heap_built = False
        client.extra_args = ["-R", "Foo"]
        with patch.object(server_mod, '_lsp_client', client):
            result = await isabelle_launch("Foo")
        client.start.assert_awaited_once()
        client.kill.assert_not_called()
        assert _yaml(result)["version"] == "Isabelle2024"

    @pytest.mark.asyncio
    async def test_launch_start_failure_cleans_up(self):
        # start() failing (missing heap / undefined session) must leave NO
        # half-started server behind: kill-first cleanup, process detached —
        # otherwise the next same-session launch would no-op on a wedged one.
        import isabelle_mcp.server as server_mod
        from isabelle_mcp.utils import IsabelleToolError

        client = _launch_mock(running=False)
        client.process = MagicMock()  # as if start() spawned before failing
        client.start = AsyncMock(side_effect=IsabelleToolError("Missing heap image"))
        with patch.object(server_mod, '_lsp_client', client):
            with pytest.raises(IsabelleToolError, match="Missing heap image"):
                await isabelle_launch("HOL")
        client.kill.assert_called_once()
        assert client.process is None

    @pytest.mark.asyncio
    async def test_launch_probe_failure_is_fail_closed(self):
        # enumerate_heap_sources raising (probe could not run) aborts the
        # launch and cleans up, even though start() itself succeeded.
        import isabelle_mcp.server as server_mod
        from isabelle_mcp.utils import IsabelleToolError

        client = _launch_mock(running=False)
        client.enumerate_heap_sources = AsyncMock(
            side_effect=IsabelleToolError("Could not verify the session's build status")
        )
        with patch.object(server_mod, '_lsp_client', client):
            with pytest.raises(IsabelleToolError, match="build status"):
                await isabelle_launch("HOL")
        client.kill.assert_called_once()
        assert client.process is None

    @pytest.mark.asyncio
    async def test_terminate_running(self):
        import isabelle_mcp.server as server_mod

        client = _launch_mock(running=True)
        watcher = MagicMock()
        with patch.object(server_mod, '_lsp_client', client), \
                patch.object(server_mod, '_file_watcher', watcher):
            result = await isabelle_terminate()
        client.shutdown.assert_awaited_once()
        assert client.process is None
        watcher.clear_watches.assert_called_once()
        assert "terminated" in result.content[0].text.lower()

    @pytest.mark.asyncio
    async def test_terminate_not_running(self):
        import isabelle_mcp.server as server_mod

        client = _launch_mock(running=False)
        with patch.object(server_mod, '_lsp_client', client):
            result = await isabelle_terminate()
        client.shutdown.assert_not_awaited()
        assert "No Isabelle session" in result.content[0].text


class TestFooterPlumbing:
    """The footer is computed at tool entry and appended at tool exit; those are
    two different frames of the same call, so it travels in a ContextVar."""

    @pytest.mark.asyncio
    async def test_footer_set_during_the_call_is_appended_after_it(self):
        from fastmcp.tools.tool import ToolResult
        from mcp.types import TextContent

        from isabelle_mcp import unicode_guard
        from isabelle_mcp.server import UnicodeWarningMiddleware, _pending_footer

        # The warning queue is module-global; an integration test run in the
        # same process (isabelle on PATH) may have left a queued warning.
        unicode_guard.drain_warnings()

        async def call_next(_ctx):
            # What _ensure_lsp_started(footer=True) does, one frame deeper.
            _pending_footer.set("Evaluating towards Foo.thy:20.")
            return ToolResult(content=[TextContent(type="text", text="the answer")])

        result = await UnicodeWarningMiddleware().on_call_tool(None, call_next)
        assert [c.text for c in result.content] == [
            "the answer", "Evaluating towards Foo.thy:20.",
        ]

    @pytest.mark.asyncio
    async def test_a_footer_from_an_earlier_call_is_not_reused(self):
        from fastmcp.tools.tool import ToolResult
        from mcp.types import TextContent

        from isabelle_mcp import unicode_guard
        from isabelle_mcp.server import UnicodeWarningMiddleware, _pending_footer

        unicode_guard.drain_warnings()          # start from a clean queue
        _pending_footer.set("stale footer from a previous call")

        async def call_next(_ctx):
            return ToolResult(content=[TextContent(type="text", text="the answer")])

        result = await UnicodeWarningMiddleware().on_call_tool(None, call_next)
        assert [c.text for c in result.content] == ["the answer"]

    @pytest.mark.asyncio
    async def test_only_the_tools_that_display_it_compute_it(self, mock_lsp_client):
        """A computation that a tool does not display must not run on its path:
        it can end an evaluation, and isabelle_evaluation_status would then
        report "No evaluation in progress." instead of the completion."""
        from unittest.mock import AsyncMock, patch

        from isabelle_mcp import server

        calls = []
        with patch.object(server, "_lsp_client", mock_lsp_client), \
                patch.object(server, "resync_and_check_freshness", new_callable=AsyncMock), \
                patch.object(server, "evaluation_footer", new_callable=AsyncMock) as footer:
            mock_lsp_client.process = object()
            footer.side_effect = lambda c: calls.append(1) or "the footer"

            await server._ensure_lsp_started()
            assert calls == [] and server._pending_footer.get() == ""

            await server._ensure_lsp_started(footer=True)
            assert calls == [1] and server._pending_footer.get() == "the footer"
        server._pending_footer.set("")


class TestFooterScope:
    """§4.2 constraint 1: the footer is computed only by the tools that show it.

    The helper-level test above pins _ensure_lsp_started's own behaviour; this one
    pins the call sites, so dropping `footer=True` from a query tool — or adding it
    to isabelle_evaluation_status, the case the constraint was written to
    prevent — reds here."""

    @pytest.mark.asyncio
    async def test_which_tools_ask_for_a_footer(self, temp_theory_file, mock_lsp_client):
        import contextlib

        from isabelle_mcp import server

        seen: dict[str, bool] = {}

        def spy(name):
            async def _ensure(*, footer: bool = False):
                seen[name] = footer
                return mock_lsp_client
            return _ensure

        line, sym = 5, "my_const"
        calls = {
            "isabelle_hover": lambda: server.isabelle_hover(temp_theory_file, line, sym),
            "isabelle_definition": lambda: server.isabelle_definition(temp_theory_file, line, sym),
            "isabelle_local_occurrences": lambda: server.isabelle_local_occurrences(temp_theory_file, line, sym),
            "isabelle_goal": lambda: server.isabelle_goal(temp_theory_file, line),
            "isabelle_find_theorems": lambda: server.isabelle_find_theorems(temp_theory_file, line),
            "isabelle_command_output": lambda: server.isabelle_command_output(temp_theory_file, line),
            "isabelle_evaluate_to": lambda: server.isabelle_evaluate_to(temp_theory_file, line),
            "isabelle_evaluation_status": lambda: server.isabelle_evaluation_status(),
            "isabelle_cancel_evaluation": lambda: server.isabelle_cancel_evaluation(),
            "isabelle_session_info": lambda: server.isabelle_session_info(),
        }
        for name, call in calls.items():
            with patch.object(server, "_ensure_lsp_started", spy(name)), \
                    contextlib.suppress(Exception):
                await call()

        assert seen["isabelle_hover"] is True
        assert seen["isabelle_definition"] is True
        assert seen["isabelle_local_occurrences"] is True
        assert seen["isabelle_goal"] is True
        assert seen["isabelle_find_theorems"] is True
        assert seen["isabelle_command_output"] is True
        # These three state the same facts in their own bodies, and computing the
        # footer here would let isabelle_evaluation_status answer "No evaluation
        # in progress." instead of reporting the completion it just observed.
        assert seen["isabelle_evaluate_to"] is False
        assert seen["isabelle_evaluation_status"] is False
        assert seen["isabelle_cancel_evaluation"] is False
        assert seen["isabelle_session_info"] is False

    @pytest.mark.asyncio
    async def test_unicode_warning_comes_before_the_footer(self):
        """§4.2: a rare, actionable warning must not be buried under a constant
        line the agent learns to skim."""
        from fastmcp.tools.tool import ToolResult
        from mcp.types import TextContent

        from isabelle_mcp import unicode_guard
        from isabelle_mcp.server import UnicodeWarningMiddleware, _pending_footer

        unicode_guard.drain_warnings()          # start from a clean queue
        unicode_guard.record_warning("/p/A.thy", "- A.thy: 3 glyphs rewritten")

        async def call_next(_ctx):
            _pending_footer.set("Evaluating towards A.thy:20.")
            return ToolResult(content=[TextContent(type="text", text="the answer")])

        result = await UnicodeWarningMiddleware().on_call_tool(None, call_next)
        texts = [c.text for c in result.content]
        assert texts[0] == "the answer"
        assert texts[1].startswith("⚠️ NON-ASCII DETECTED")
        assert texts[2] == "Evaluating towards A.thy:20."
