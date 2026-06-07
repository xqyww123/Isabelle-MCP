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
    isabelle_local_occurrences,
    isabelle_session_info,
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
    async def test_custom_session(self):
        import isabelle_mcp.server as server_mod
        from isabelle_mcp.server import server_lifespan

        server_mod._server_logic = "HOL-Analysis"
        server_mod._server_extra_args = ["-d", "/extra"]
        try:
            with patch('isabelle_mcp.server.IsabelleLSPClient') as MockClient:
                mock_instance = MagicMock()
                mock_instance.process = None
                MockClient.return_value = mock_instance
                async with server_lifespan(MagicMock()):
                    MockClient.assert_called_with(
                        logic='HOL-Analysis', extra_args=["-d", "/extra"],
                        project_root=None,
                    )
        finally:
            server_mod._server_logic = "HOL"
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
        with patch.object(sys, 'argv', ['isabelle-mcp', '-s', 'HOL']):
            with patch.object(mcp, 'run') as mock_run:
                main()
                mock_run.assert_called_once()

    def test_session_required(self):
        import sys

        from isabelle_mcp.server import main
        with patch.object(sys, 'argv', ['isabelle-mcp']):
            with pytest.raises(SystemExit, match="2"):
                main()

    def test_extra_args_passthrough(self):
        import sys

        import isabelle_mcp.server as server_mod
        from isabelle_mcp.server import main, mcp
        with patch.object(sys, 'argv', ['isabelle-mcp', '-s', 'HOL-Analysis', '--', '-d', '/extra', '-o', 'threads=4']):
            with patch.object(mcp, 'run'):
                main()
                assert server_mod._server_logic == "HOL-Analysis"
                assert server_mod._server_extra_args == ["-d", "/extra", "-o", "threads=4"]

    def test_typo_rejected(self):
        import sys

        from isabelle_mcp.server import main
        with patch.object(sys, 'argv', ['isabelle-mcp', '-s', 'HOL', '--httpp']):
            with pytest.raises(SystemExit, match="2"):
                main()
