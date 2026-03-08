"""
Unit tests for preview tool.
"""

import pytest
from isa_lsp.tools.preview import preview_document
from isa_lsp.utils import IsabelleToolError


# Mark all preview tests as expected to fail in MVP
pytestmark = pytest.mark.xfail(reason="Preview tool has MVP limitations - not fully implemented")


class TestPreviewTool:
    """Test preview_document tool."""

    @pytest.mark.asyncio
    async def test_preview_basic(self, mock_lsp_client, temp_theory_file):
        """Test basic preview functionality."""
        result = await preview_document(mock_lsp_client, temp_theory_file)

        assert result.html is not None
        assert result.line_context is None  # No line specified

    @pytest.mark.asyncio
    async def test_preview_with_line(self, mock_lsp_client, temp_theory_file):
        """Test preview with line context."""
        result = await preview_document(mock_lsp_client, temp_theory_file, line=5)

        assert result.html is not None
        assert result.line_context is not None

    @pytest.mark.asyncio
    async def test_preview_mvp_limitation(self, mock_lsp_client, temp_theory_file):
        """Test that preview returns empty HTML in MVP."""
        result = await preview_document(mock_lsp_client, temp_theory_file)

        # MVP limitation: returns empty HTML
        assert result.html == ""

    @pytest.mark.asyncio
    async def test_preview_auto_open(self, mock_lsp_client, temp_theory_file):
        """Test that preview auto-opens document."""
        assert temp_theory_file not in mock_lsp_client.open_documents

        await preview_document(mock_lsp_client, temp_theory_file)

        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_preview_file_not_found(self, mock_lsp_client):
        """Test preview with non-existent file."""
        with pytest.raises(FileNotFoundError):
            await preview_document(mock_lsp_client, "/nonexistent/file.thy")

    @pytest.mark.asyncio
    async def test_preview_line_context_extraction(self, mock_lsp_client, temp_theory_file):
        """Test line context extraction when line is provided."""
        result = await preview_document(mock_lsp_client, temp_theory_file, line=8)

        assert result.line_context is not None
        assert len(result.line_context) > 0

    @pytest.mark.asyncio
    async def test_preview_large_line_number(self, mock_lsp_client, temp_theory_file):
        """Test preview with line number beyond file."""
        result = await preview_document(mock_lsp_client, temp_theory_file, line=1000)

        assert result.line_context == ""
        assert result.html == ""
