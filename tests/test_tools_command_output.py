"""
Unit tests for command_output tool.
"""

import pytest

from isa_lsp.tools.command_output import command_output


class TestCommandOutputTool:
    """Test command_output tool."""

    @pytest.mark.asyncio
    async def test_command_output_basic(self, mock_lsp_client, temp_theory_file):
        """Test basic command output functionality."""
        result = await command_output(mock_lsp_client, temp_theory_file, 8)

        assert result.line_context is not None
        assert result.messages is not None

    @pytest.mark.asyncio
    async def test_command_output_mvp_limitation(self, mock_lsp_client, temp_theory_file):
        """Test that command output returns empty in MVP."""
        result = await command_output(mock_lsp_client, temp_theory_file, 8)

        # MVP limitation: returns empty messages
        assert result.messages == []

    @pytest.mark.asyncio
    async def test_command_output_auto_open(self, mock_lsp_client, temp_theory_file):
        """Test that command output auto-opens document."""
        assert temp_theory_file not in mock_lsp_client.open_documents

        await command_output(mock_lsp_client, temp_theory_file, 8)

        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_command_output_line_context(self, mock_lsp_client, temp_theory_file):
        """Test line context extraction."""
        result = await command_output(mock_lsp_client, temp_theory_file, 5)

        assert result.line_context is not None
        assert len(result.line_context) > 0

    @pytest.mark.asyncio
    async def test_command_output_file_not_found(self, mock_lsp_client):
        """Test command output with non-existent file."""
        with pytest.raises(FileNotFoundError):
            await command_output(mock_lsp_client, "/nonexistent/file.thy", 1)

    @pytest.mark.asyncio
    async def test_command_output_empty_line(self, mock_lsp_client, temp_theory_file):
        """Test command output on empty line."""
        result = await command_output(mock_lsp_client, temp_theory_file, 4)

        assert result.line_context == ""
        assert result.messages == []

    @pytest.mark.asyncio
    async def test_command_output_large_line_number(self, mock_lsp_client, temp_theory_file):
        """Test command output with line number beyond file."""
        result = await command_output(mock_lsp_client, temp_theory_file, 1000)

        assert result.line_context == ""
        assert result.messages == []
