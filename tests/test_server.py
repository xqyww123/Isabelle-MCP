"""
Unit tests for MCP server.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from isa_lsp.server import (
    isabelle_build,
    isabelle_command_output,
    isabelle_completions,
    isabelle_definition,
    isabelle_diagnostics,
    isabelle_goal,
    isabelle_highlights,
    isabelle_hover,
    isabelle_preview,
    isabelle_session_info,
)


def _patch_ensure(mock_client):
    """Patch _ensure_lsp_started to return mock_client."""
    return patch(
        'isa_lsp.server._ensure_lsp_started',
        new_callable=AsyncMock,
        return_value=mock_client,
    )


class TestMCPServerTools:
    """Test MCP server tool wrappers."""

    @pytest.mark.asyncio
    async def test_isabelle_hover_wrapper(self, temp_theory_file, mock_lsp_client):
        """Test isabelle_hover MCP wrapper."""
        mock_lsp_client.hover_response = {"contents": "test"}
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_hover(temp_theory_file, 5, 15)

        assert result is not None
        assert hasattr(result, 'symbol')
        assert hasattr(result, 'info')

    @pytest.mark.asyncio
    async def test_isabelle_completions_wrapper(self, temp_theory_file, mock_lsp_client):
        """Test isabelle_completions MCP wrapper."""
        mock_lsp_client.completion_response = {"items": []}
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_completions(temp_theory_file, 8, 1)

        assert result is not None
        assert hasattr(result, 'items')

    @pytest.mark.asyncio
    async def test_isabelle_definition_wrapper(self, temp_theory_file, mock_lsp_client):
        """Test isabelle_definition MCP wrapper."""
        mock_lsp_client.definition_response = []
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_definition(temp_theory_file, 8, 20)

        assert result is not None
        assert hasattr(result, 'symbol')
        assert hasattr(result, 'locations')

    @pytest.mark.asyncio
    async def test_isabelle_highlights_wrapper(self, temp_theory_file, mock_lsp_client):
        """Test isabelle_highlights MCP wrapper."""
        mock_lsp_client.highlights_response = []
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_highlights(temp_theory_file, 5, 15)

        assert result is not None
        assert hasattr(result, 'symbol')
        assert hasattr(result, 'highlights')

    @pytest.mark.asyncio
    async def test_isabelle_diagnostics_wrapper(self, temp_theory_file, mock_lsp_client):
        """Test isabelle_diagnostics MCP wrapper."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []
        mock_lsp_client.processing_status[temp_theory_file] = True
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_diagnostics(temp_theory_file)

        assert result is not None
        assert hasattr(result, 'success')
        assert hasattr(result, 'items')

    @pytest.mark.asyncio
    async def test_isabelle_diagnostics_with_line_filter(self, temp_theory_file, mock_lsp_client):
        """Test isabelle_diagnostics with line filtering."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []
        mock_lsp_client.processing_status[temp_theory_file] = True
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_diagnostics(
                temp_theory_file,
                start_line=5,
                end_line=10,
            )

        assert result is not None

    @pytest.mark.asyncio
    async def test_isabelle_goal_wrapper(self, temp_theory_file, mock_lsp_client):
        """Test isabelle_goal MCP wrapper."""
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_goal(temp_theory_file, 8)

        assert result is not None
        assert hasattr(result, 'line_context')
        assert hasattr(result, 'goals_before')
        assert hasattr(result, 'goals_after')

    @pytest.mark.asyncio
    async def test_isabelle_goal_with_column(self, temp_theory_file, mock_lsp_client):
        """Test isabelle_goal with column parameter."""
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_goal(temp_theory_file, 8, column=10)

        assert result is not None
        assert hasattr(result, 'goals')

    @pytest.mark.asyncio
    async def test_isabelle_command_output_wrapper(self, temp_theory_file, mock_lsp_client):
        """Test isabelle_command_output MCP wrapper."""
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_command_output(temp_theory_file, 8)

        assert result is not None
        assert hasattr(result, 'line_context')
        assert hasattr(result, 'messages')

    @pytest.mark.asyncio
    async def test_isabelle_preview_wrapper(self, temp_theory_file, mock_lsp_client):
        """Test isabelle_preview MCP wrapper."""
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_preview(temp_theory_file)

        assert result is not None
        assert hasattr(result, 'html')

    @pytest.mark.asyncio
    async def test_isabelle_preview_with_line(self, temp_theory_file, mock_lsp_client):
        """Test isabelle_preview with line parameter."""
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_preview(temp_theory_file, line=5)

        assert result is not None
        assert hasattr(result, 'line_context')

    @pytest.mark.asyncio
    async def test_isabelle_session_info_wrapper(self, mock_lsp_client):
        """Test isabelle_session_info MCP wrapper."""
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_session_info()

        assert result is not None
        assert hasattr(result, 'current_session')
        assert hasattr(result, 'available_sessions')
        assert result.current_session == "HOL"

    @pytest.mark.asyncio
    async def test_isabelle_build_wrapper(self, mock_lsp_client):
        """Test isabelle_build MCP wrapper."""
        with _patch_ensure(mock_lsp_client):
            with patch('asyncio.create_subprocess_exec') as mock_subprocess:
                mock_process = AsyncMock()
                mock_process.communicate = AsyncMock(return_value=(b"Success", b""))
                mock_process.returncode = 0
                mock_subprocess.return_value = mock_process

                result = await isabelle_build("HOL")

        assert result is not None
        assert hasattr(result, 'success')
        assert hasattr(result, 'session')
        assert hasattr(result, 'messages')

    @pytest.mark.asyncio
    async def test_isabelle_build_with_clean(self, mock_lsp_client):
        """Test isabelle_build with clean parameter."""
        with _patch_ensure(mock_lsp_client):
            with patch('asyncio.create_subprocess_exec') as mock_subprocess:
                mock_process = AsyncMock()
                mock_process.communicate = AsyncMock(return_value=(b"Success", b""))
                mock_process.returncode = 0
                mock_subprocess.return_value = mock_process

                result = await isabelle_build("HOL", clean=True)

        call_args = mock_subprocess.call_args[0]
        assert "-c" in call_args


class TestServerLifespan:
    """Test server lifespan management."""

    @pytest.mark.asyncio
    async def test_server_lifespan_creates_client(self):
        """Test server lifespan creates LSP client (lazy init, no start)."""
        import isa_lsp.server as server_mod
        from isa_lsp.server import server_lifespan

        with patch('isa_lsp.server.IsabelleLSPClient') as MockClient:
            mock_instance = MagicMock()
            mock_instance.process = None
            MockClient.return_value = mock_instance

            async with server_lifespan(MagicMock()):
                assert server_mod._lsp_client is mock_instance
                # Lazy init: start() should NOT be called during lifespan entry
                mock_instance.start.assert_not_called()

    @pytest.mark.asyncio
    async def test_server_lifespan_custom_session(self):
        """Test server startup with custom session from env."""
        from isa_lsp.server import server_lifespan

        with patch.dict('os.environ', {'ISABELLE_SESSION': 'Main'}):
            with patch('isa_lsp.server.IsabelleLSPClient') as MockClient:
                mock_instance = MagicMock()
                mock_instance.process = None
                MockClient.return_value = mock_instance

                async with server_lifespan(MagicMock()):
                    MockClient.assert_called_with(logic='Main')


class TestServerMain:
    """Test server main entry point."""

    def test_main_version(self):
        """Test main with --version flag."""
        import sys

        from isa_lsp.server import main

        with patch.object(sys, 'argv', ['isa-lsp', '--version']):
            with patch('builtins.print') as mock_print:
                main()
                mock_print.assert_called_once()
                assert "version" in mock_print.call_args[0][0].lower()

    def test_main_run(self):
        """Test main runs MCP server."""
        from isa_lsp.server import main, mcp

        with patch.object(mcp, 'run') as mock_run:
            main()
            mock_run.assert_called_once()
