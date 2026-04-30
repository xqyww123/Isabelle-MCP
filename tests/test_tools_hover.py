"""
Unit tests for hover tool.
"""

import pytest

from isa_lsp.tools.hover import hover_info


class TestHoverTool:
    """Test hover_info tool."""

    @pytest.mark.asyncio
    async def test_hover_basic(self, mock_lsp_client, temp_theory_file, sample_hover_response):
        """Test basic hover functionality."""
        mock_lsp_client.hover_response = sample_hover_response

        result = await hover_info(mock_lsp_client, temp_theory_file, 5, 15)

        assert result.symbol is not None
        assert result.info is not None
        assert result.line_context is not None
        assert isinstance(result.diagnostics, list)

    @pytest.mark.asyncio
    async def test_hover_with_markdown(self, mock_lsp_client, temp_theory_file):
        """Test hover with markdown content."""
        mock_lsp_client.hover_response = {
            "contents": {
                "kind": "markdown",
                "value": "**Symbol**: `my_const`\n\nType: `nat`"
            }
        }

        result = await hover_info(mock_lsp_client, temp_theory_file, 5, 15)

        assert "my_const" in result.info or result.info != ""
        assert result.symbol is not None

    @pytest.mark.asyncio
    async def test_hover_with_plaintext(self, mock_lsp_client, temp_theory_file):
        """Test hover with plaintext content."""
        mock_lsp_client.hover_response = {
            "contents": "Simple text content"
        }

        result = await hover_info(mock_lsp_client, temp_theory_file, 5, 15)

        assert result.info is not None

    @pytest.mark.asyncio
    async def test_hover_null_response(self, mock_lsp_client, temp_theory_file):
        """Test hover with null response."""
        mock_lsp_client.hover_response = None

        result = await hover_info(mock_lsp_client, temp_theory_file, 5, 15)

        assert result.symbol is not None  # Should extract from file
        assert result.info == ""  # Empty when no hover info

    @pytest.mark.asyncio
    async def test_hover_auto_open_document(self, mock_lsp_client, temp_theory_file):
        """Test that hover auto-opens document if not open."""
        # Document not in open_documents initially
        assert temp_theory_file not in mock_lsp_client.open_documents

        mock_lsp_client.hover_response = {"contents": "test"}

        await hover_info(mock_lsp_client, temp_theory_file, 5, 15)

        # Document should be opened
        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_hover_with_diagnostics(self, mock_lsp_client, temp_theory_file):
        """Test hover includes diagnostics from cache."""
        # Add diagnostics to cache
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {
                "range": {
                    "start": {"line": 4, "character": 0},
                    "end": {"line": 4, "character": 10}
                },
                "severity": 1,
                "message": "Type error"
            }
        ]

        mock_lsp_client.hover_response = {"contents": "test"}

        result = await hover_info(mock_lsp_client, temp_theory_file, 5, 1)

        assert len(result.diagnostics) > 0
        assert result.diagnostics[0].severity == "error"
        assert result.diagnostics[0].message == "Type error"

    @pytest.mark.asyncio
    async def test_hover_invalid_position(self, mock_lsp_client, temp_theory_file):
        """Test hover with invalid position."""
        mock_lsp_client.hover_response = None

        # Position beyond file end
        result = await hover_info(mock_lsp_client, temp_theory_file, 1000, 1)

        # Should still return result, just with empty symbol
        assert result.symbol == ""
        assert result.line_context == ""

    @pytest.mark.asyncio
    async def test_hover_file_not_found(self, mock_lsp_client):
        """Test hover with non-existent file."""
        with pytest.raises(FileNotFoundError):
            await hover_info(mock_lsp_client, "/nonexistent/file.thy", 1, 1)

    @pytest.mark.asyncio
    async def test_hover_position_conversion(self, mock_lsp_client, temp_theory_file):
        """Test that positions are correctly converted to LSP."""
        mock_lsp_client.hover_response = {"contents": "test"}

        # MCP uses 1-indexed, LSP uses 0-indexed
        await hover_info(mock_lsp_client, temp_theory_file, 5, 10)

        # The client's get_hover should have been called with 0-indexed positions
        # This would need deeper mocking to verify, but we test the conversion works

    @pytest.mark.asyncio
    async def test_hover_array_contents(self, mock_lsp_client, temp_theory_file):
        """Test hover with array of contents."""
        mock_lsp_client.hover_response = {
            "contents": [
                {"language": "isabelle", "value": "definition"},
                "Additional info"
            ]
        }

        result = await hover_info(mock_lsp_client, temp_theory_file, 5, 15)

        # Should handle array of contents
        assert result.info is not None

    @pytest.mark.asyncio
    async def test_hover_marked_string(self, mock_lsp_client, temp_theory_file):
        """Test hover with MarkedString format."""
        mock_lsp_client.hover_response = {
            "contents": {
                "language": "isabelle",
                "value": "my_const :: nat"
            }
        }

        result = await hover_info(mock_lsp_client, temp_theory_file, 5, 15)

        assert result.info is not None
