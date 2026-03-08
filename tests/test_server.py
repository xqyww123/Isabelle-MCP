"""
Unit tests for MCP server.
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from isa_lsp.server import (
    isabelle_hover,
    isabelle_completions,
    isabelle_definition,
    isabelle_highlights,
    isabelle_diagnostics,
    isabelle_goal,
    isabelle_command_output,
    isabelle_preview,
    isabelle_session_info,
    isabelle_build,
)


class TestMCPServerTools:
    """Test MCP server tool wrappers."""

    @pytest.mark.asyncio
    async def test_isabelle_hover_wrapper(self, temp_theory_file):
        """Test isabelle_hover MCP wrapper."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            mock_client.open_documents = {}
            mock_client.diagnostics_cache = {}
            mock_client.hover_response = None
            mock_client.open_document = AsyncMock()

            result = await isabelle_hover(temp_theory_file, 5, 15)

            assert result is not None
            assert hasattr(result, 'symbol')
            assert hasattr(result, 'info')

    @pytest.mark.asyncio
    async def test_isabelle_completions_wrapper(self, temp_theory_file):
        """Test isabelle_completions MCP wrapper."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            mock_client.open_documents = {}
            mock_client.completion_response = {"items": []}
            mock_client.open_document = AsyncMock()

            result = await isabelle_completions(temp_theory_file, 8, 1)

            assert result is not None
            assert hasattr(result, 'items')

    @pytest.mark.asyncio
    async def test_isabelle_definition_wrapper(self, temp_theory_file):
        """Test isabelle_definition MCP wrapper."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            mock_client.open_documents = {}
            mock_client.definition_response = []
            mock_client.open_document = AsyncMock()

            result = await isabelle_definition(temp_theory_file, 8, 20)

            assert result is not None
            assert hasattr(result, 'symbol')
            assert hasattr(result, 'locations')

    @pytest.mark.asyncio
    async def test_isabelle_highlights_wrapper(self, temp_theory_file):
        """Test isabelle_highlights MCP wrapper."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            mock_client.open_documents = {}
            mock_client.highlights_response = []
            mock_client.open_document = AsyncMock()

            result = await isabelle_highlights(temp_theory_file, 5, 15)

            assert result is not None
            assert hasattr(result, 'symbol')
            assert hasattr(result, 'highlights')

    @pytest.mark.asyncio
    async def test_isabelle_diagnostics_wrapper(self, temp_theory_file):
        """Test isabelle_diagnostics MCP wrapper."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            mock_client.open_documents = {}
            mock_client.diagnostics_cache = {}
            mock_client.processing_status = {}
            mock_client.open_document = AsyncMock()

            result = await isabelle_diagnostics(temp_theory_file)

            assert result is not None
            assert hasattr(result, 'success')
            assert hasattr(result, 'items')

    @pytest.mark.asyncio
    async def test_isabelle_diagnostics_with_line_filter(self, temp_theory_file):
        """Test isabelle_diagnostics with line filtering."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            mock_client.open_documents = {}
            mock_client.diagnostics_cache = {}
            mock_client.processing_status = {}
            mock_client.open_document = AsyncMock()

            result = await isabelle_diagnostics(
                temp_theory_file,
                start_line=5,
                end_line=10
            )

            assert result is not None

    @pytest.mark.asyncio
    async def test_isabelle_goal_wrapper(self, temp_theory_file):
        """Test isabelle_goal MCP wrapper."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            mock_client.open_documents = {}
            mock_client.open_document = AsyncMock()
            mock_client.notify = AsyncMock()

            result = await isabelle_goal(temp_theory_file, 8)

            assert result is not None
            assert hasattr(result, 'line_context')
            assert hasattr(result, 'goals_before')
            assert hasattr(result, 'goals_after')

    @pytest.mark.asyncio
    async def test_isabelle_goal_with_column(self, temp_theory_file):
        """Test isabelle_goal with column parameter."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            mock_client.open_documents = {}
            mock_client.open_document = AsyncMock()
            mock_client.notify = AsyncMock()

            result = await isabelle_goal(temp_theory_file, 8, column=10)

            assert result is not None
            assert hasattr(result, 'goals')

    @pytest.mark.asyncio
    async def test_isabelle_command_output_wrapper(self, temp_theory_file):
        """Test isabelle_command_output MCP wrapper."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            mock_client.open_documents = {}
            mock_client.open_document = AsyncMock()

            result = await isabelle_command_output(temp_theory_file, 8)

            assert result is not None
            assert hasattr(result, 'line_context')
            assert hasattr(result, 'messages')

    @pytest.mark.asyncio
    async def test_isabelle_preview_wrapper(self, temp_theory_file):
        """Test isabelle_preview MCP wrapper."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            mock_client.open_documents = {}
            mock_client.open_document = AsyncMock()
            mock_client.notify = AsyncMock()

            result = await isabelle_preview(temp_theory_file)

            assert result is not None
            assert hasattr(result, 'html')

    @pytest.mark.asyncio
    async def test_isabelle_preview_with_line(self, temp_theory_file):
        """Test isabelle_preview with line parameter."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            mock_client.open_documents = {}
            mock_client.open_document = AsyncMock()
            mock_client.notify = AsyncMock()

            result = await isabelle_preview(temp_theory_file, line=5)

            assert result is not None
            assert hasattr(result, 'line_context')

    @pytest.mark.asyncio
    async def test_isabelle_session_info_wrapper(self):
        """Test isabelle_session_info MCP wrapper."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            mock_client.logic = "HOL"

            result = await isabelle_session_info()

            assert result is not None
            assert hasattr(result, 'current_session')
            assert hasattr(result, 'available_sessions')
            assert result.current_session == "HOL"

    @pytest.mark.asyncio
    async def test_isabelle_build_wrapper(self):
        """Test isabelle_build MCP wrapper."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
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
    async def test_isabelle_build_with_clean(self):
        """Test isabelle_build with clean parameter."""
        with patch('isa_lsp.server._lsp_client') as mock_client:
            with patch('asyncio.create_subprocess_exec') as mock_subprocess:
                mock_process = AsyncMock()
                mock_process.communicate = AsyncMock(return_value=(b"Success", b""))
                mock_process.returncode = 0
                mock_subprocess.return_value = mock_process

                result = await isabelle_build("HOL", clean=True)

                assert result is not None
                # Verify clean flag was passed
                call_args = mock_subprocess.call_args[0]
                assert "-c" in call_args


class TestServerLifespan:
    """Test server lifespan management."""

    @pytest.mark.asyncio
    async def test_server_lifespan_startup(self):
        """Test server startup with lifespan."""
        from isa_lsp.server import server_lifespan
        from isa_lsp.server import _lsp_client

        with patch('isa_lsp.lsp_client.IsabelleLSPClient') as MockClient:
            mock_instance = AsyncMock()
            mock_instance.start = AsyncMock()
            mock_instance.shutdown = AsyncMock()
            MockClient.return_value = mock_instance

            async with server_lifespan():
                # Client should be started
                mock_instance.start.assert_called_once()

            # Client should be shut down
            mock_instance.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_server_lifespan_custom_session(self):
        """Test server startup with custom session from env."""
        from isa_lsp.server import server_lifespan

        with patch.dict('os.environ', {'ISABELLE_SESSION': 'Main'}):
            with patch('isa_lsp.lsp_client.IsabelleLSPClient') as MockClient:
                mock_instance = AsyncMock()
                mock_instance.start = AsyncMock()
                mock_instance.shutdown = AsyncMock()
                MockClient.return_value = mock_instance

                async with server_lifespan():
                    # Should use Main session
                    MockClient.assert_called_with(logic='Main')


class TestServerMain:
    """Test server main entry point."""

    def test_main_version(self):
        """Test main with --version flag."""
        from isa_lsp.server import main
        import sys

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
