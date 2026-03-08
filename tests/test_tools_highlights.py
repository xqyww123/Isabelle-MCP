"""
Unit tests for highlights tool.
"""

import pytest
from isa_lsp.tools.highlights import document_highlights
from isa_lsp.utils import IsabelleToolError


class TestHighlightsTool:
    """Test document_highlights tool."""

    @pytest.mark.asyncio
    async def test_highlights_basic(self, mock_lsp_client, temp_theory_file, sample_highlights_response):
        """Test basic highlights functionality."""
        mock_lsp_client.highlights_response = sample_highlights_response

        result = await document_highlights(mock_lsp_client, temp_theory_file, 5, 15)

        assert result.symbol is not None
        assert len(result.highlights) > 0
        assert result.highlights[0].line >= 1
        assert result.highlights[0].start_column >= 1
        assert result.highlights[0].end_column >= 1

    @pytest.mark.asyncio
    async def test_highlights_null_response(self, mock_lsp_client, temp_theory_file):
        """Test highlights with null response."""
        mock_lsp_client.highlights_response = None

        result = await document_highlights(mock_lsp_client, temp_theory_file, 5, 15)

        assert result.symbol is not None  # Should extract from file
        assert result.highlights == []  # No highlights found

    @pytest.mark.asyncio
    async def test_highlights_empty_response(self, mock_lsp_client, temp_theory_file):
        """Test highlights with empty response."""
        mock_lsp_client.highlights_response = []

        result = await document_highlights(mock_lsp_client, temp_theory_file, 5, 15)

        assert result.highlights == []

    @pytest.mark.asyncio
    async def test_highlights_kind_mapping(self, mock_lsp_client, temp_theory_file):
        """Test highlight kind mapping."""
        mock_lsp_client.highlights_response = [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 10}
                },
                "kind": 1  # Text
            },
            {
                "range": {
                    "start": {"line": 1, "character": 0},
                    "end": {"line": 1, "character": 10}
                },
                "kind": 2  # Read
            },
            {
                "range": {
                    "start": {"line": 2, "character": 0},
                    "end": {"line": 2, "character": 10}
                },
                "kind": 3  # Write
            }
        ]

        result = await document_highlights(mock_lsp_client, temp_theory_file, 5, 15)

        assert len(result.highlights) == 3
        kinds = [h.kind for h in result.highlights]
        assert "text" in kinds
        assert "read" in kinds
        assert "write" in kinds

    @pytest.mark.asyncio
    async def test_highlights_unknown_kind(self, mock_lsp_client, temp_theory_file):
        """Test highlight with unknown kind."""
        mock_lsp_client.highlights_response = [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 10}
                },
                "kind": 999  # Unknown kind
            }
        ]

        result = await document_highlights(mock_lsp_client, temp_theory_file, 5, 15)

        # Should default to "text"
        assert len(result.highlights) == 1
        assert result.highlights[0].kind == "text"

    @pytest.mark.asyncio
    async def test_highlights_auto_open(self, mock_lsp_client, temp_theory_file):
        """Test that highlights auto-opens document."""
        assert temp_theory_file not in mock_lsp_client.open_documents

        mock_lsp_client.highlights_response = []

        await document_highlights(mock_lsp_client, temp_theory_file, 5, 15)

        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_highlights_position_conversion(self, mock_lsp_client, temp_theory_file):
        """Test position conversion LSP to MCP."""
        mock_lsp_client.highlights_response = [
            {
                "range": {
                    "start": {"line": 0, "character": 0},  # LSP 0-indexed
                    "end": {"line": 0, "character": 10}
                },
                "kind": 1
            }
        ]

        result = await document_highlights(mock_lsp_client, temp_theory_file, 1, 1)

        # Should convert to MCP 1-indexed
        assert result.highlights[0].line == 1
        assert result.highlights[0].start_column == 1
        assert result.highlights[0].end_column == 11

    @pytest.mark.asyncio
    async def test_highlights_multiple_occurrences(self, mock_lsp_client, temp_theory_file):
        """Test highlights with multiple occurrences."""
        # Simulate finding a symbol used multiple times
        mock_lsp_client.highlights_response = [
            {
                "range": {
                    "start": {"line": 4, "character": 11},
                    "end": {"line": 4, "character": 19}
                },
                "kind": 3  # Write (definition)
            },
            {
                "range": {
                    "start": {"line": 7, "character": 20},
                    "end": {"line": 7, "character": 28}
                },
                "kind": 2  # Read (usage)
            },
            {
                "range": {
                    "start": {"line": 8, "character": 15},
                    "end": {"line": 8, "character": 23}
                },
                "kind": 2  # Read (usage)
            }
        ]

        result = await document_highlights(mock_lsp_client, temp_theory_file, 5, 15)

        assert len(result.highlights) == 3
        # Check that definition is marked as write
        write_highlights = [h for h in result.highlights if h.kind == "write"]
        assert len(write_highlights) == 1

    @pytest.mark.asyncio
    async def test_highlights_invalid_highlight(self, mock_lsp_client, temp_theory_file):
        """Test highlights with malformed highlight."""
        mock_lsp_client.highlights_response = [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 10}
                },
                "kind": 1
            },
            {
                # Missing range
                "kind": 1
            }
        ]

        result = await document_highlights(mock_lsp_client, temp_theory_file, 5, 15)

        # Should filter out invalid highlights
        assert len(result.highlights) == 1

    @pytest.mark.asyncio
    async def test_highlights_multiline_range(self, mock_lsp_client, temp_theory_file):
        """Test highlights with multiline range."""
        mock_lsp_client.highlights_response = [
            {
                "range": {
                    "start": {"line": 5, "character": 10},
                    "end": {"line": 7, "character": 20}  # Spans multiple lines
                },
                "kind": 1
            }
        ]

        result = await document_highlights(mock_lsp_client, temp_theory_file, 6, 5)

        # Should still work with multiline ranges
        assert len(result.highlights) == 1

    @pytest.mark.asyncio
    async def test_highlights_file_not_found(self, mock_lsp_client):
        """Test highlights with non-existent file."""
        with pytest.raises(FileNotFoundError):
            await document_highlights(mock_lsp_client, "/nonexistent/file.thy", 1, 1)

    @pytest.mark.asyncio
    async def test_highlights_symbol_extraction(self, mock_lsp_client, temp_theory_file):
        """Test symbol extraction for highlights."""
        mock_lsp_client.highlights_response = []

        result = await document_highlights(mock_lsp_client, temp_theory_file, 5, 15)

        # Should extract symbol even when no highlights found
        assert result.symbol is not None
