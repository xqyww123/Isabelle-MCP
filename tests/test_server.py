"""Tests for MCP server tool wrappers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


class TestMCPServerTools:
    @pytest.mark.asyncio
    async def test_hover(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.hover_response = {"contents": "test"}
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_hover(temp_theory_file, 5, "my_const")
        assert len(result.results) >= 1
        assert result.results[0].info == "test"
        assert result.symbol == "my_const"

    @pytest.mark.asyncio
    async def test_definition(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.definition_response = []
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_definition(temp_theory_file, 8, "my_const")
        assert result.locations == []
        assert result.symbol == "my_const"

    @pytest.mark.asyncio
    async def test_local_occurrences(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.highlights_response = []
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_local_occurrences(temp_theory_file, 8, "my_const")
        assert result.occurrences == []

    @pytest.mark.asyncio
    async def test_goal_without_after_text(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.command_at_position_response = (
            "by simp", {"start": {"line": 8, "character": 2}, "end": {"line": 8, "character": 9}},
        )
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_goal(temp_theory_file, 9)
        assert result.subgoals == []
        assert result.command is not None
        assert result.command.text == "by simp"

    @pytest.mark.asyncio
    async def test_goal_with_after_text(self, temp_theory_file, mock_lsp_client):
        # Line 9 is "  by (simp add: my_const_def)"
        mock_lsp_client.command_at_position_response = (
            "by simp", {"start": {"line": 8, "character": 2}, "end": {"line": 8, "character": 9}},
        )
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_goal(temp_theory_file, 9, after_text="by")
        assert result.subgoals == []
        assert result.command is not None

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
        assert result.current_session == "HOL"

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
    client.logic = logic
    client.isabelle_version = "Isabelle2024"
    client.start = AsyncMock()
    client.shutdown = AsyncMock()
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
        assert result.current_session == "HOL"
        assert result.version == "Isabelle2024"

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
        assert result.current_session == "HOL"

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
