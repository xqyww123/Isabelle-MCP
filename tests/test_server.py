"""Tests for MCP server tool wrappers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from isabelle_mcp.server import (
    isabelle_cancel_evaluation,
    isabelle_command_output,
    isabelle_definition,
    isabelle_diagnostics,
    isabelle_evaluate_to,
    isabelle_evaluation_status,
    isabelle_goal,
    isabelle_highlights,
    isabelle_hover,
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
    async def test_highlights(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.highlights_response = []
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_highlights(temp_theory_file, 5, 15)
        assert result.highlights == []

    @pytest.mark.asyncio
    async def test_diagnostics(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []
        mock_lsp_client.processing_status[temp_theory_file] = True
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_diagnostics(temp_theory_file, 1, -1)
        assert result.success is True
        assert result.items == []

    @pytest.mark.asyncio
    async def test_diagnostics_with_line_filter(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []
        mock_lsp_client.processing_status[temp_theory_file] = True
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_diagnostics(temp_theory_file, start_line=5, end_line=10)
        assert result.items == []

    @pytest.mark.asyncio
    async def test_goal_without_column(self, temp_theory_file, mock_lsp_client):
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_goal(temp_theory_file, 8)
        assert result.goals_before == []
        assert result.goals_after == []
        assert result.goals is None

    @pytest.mark.asyncio
    async def test_goal_with_column(self, temp_theory_file, mock_lsp_client):
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_goal(temp_theory_file, 8, column=10)
        assert result.goals == []
        assert result.goals_before is None

    @pytest.mark.asyncio
    async def test_command_output(self, temp_theory_file, mock_lsp_client):
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_command_output(temp_theory_file, 8)
        assert result.messages == []
        assert isinstance(result.line_context, str)

    @pytest.mark.asyncio
    async def test_session_info(self, mock_lsp_client):
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_session_info()
        assert result.current_session == "HOL"

    @pytest.mark.asyncio
    async def test_evaluate_to(self, temp_theory_file, mock_lsp_client):
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_evaluate_to(temp_theory_file, 5)
        assert result.status == "complete"

    @pytest.mark.asyncio
    async def test_evaluation_status_no_eval(self, mock_lsp_client):
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_evaluation_status()
        assert result.status == "no_evaluation"

    @pytest.mark.asyncio
    async def test_cancel_evaluation_no_eval(self, mock_lsp_client):
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_cancel_evaluation()
        assert result.status == "no_evaluation"


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
                    )
        finally:
            server_mod._server_logic = "HOL"
            server_mod._server_extra_args = []


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
