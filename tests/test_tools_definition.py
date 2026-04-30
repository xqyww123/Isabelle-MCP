"""
Unit tests for definition tool.
"""

import pytest

from isa_lsp.tools.definition import declaration_location


class TestDefinitionTool:
    """Test declaration_location tool."""

    @pytest.mark.asyncio
    async def test_definition_basic(self, mock_lsp_client, temp_theory_file, sample_definition_response):
        """Test basic definition functionality."""
        mock_lsp_client.definition_response = sample_definition_response

        result = await declaration_location(mock_lsp_client, temp_theory_file, 8, 20)

        assert result.symbol is not None
        assert len(result.locations) > 0
        assert result.locations[0].file_path is not None
        assert result.locations[0].line >= 1
        assert result.locations[0].column >= 1

    @pytest.mark.asyncio
    async def test_definition_null_response(self, mock_lsp_client, temp_theory_file):
        """Test definition with null response."""
        mock_lsp_client.definition_response = None

        result = await declaration_location(mock_lsp_client, temp_theory_file, 8, 20)

        assert result.symbol is not None  # Should extract from file
        assert result.locations == []  # No definitions found

    @pytest.mark.asyncio
    async def test_definition_empty_response(self, mock_lsp_client, temp_theory_file):
        """Test definition with empty response."""
        mock_lsp_client.definition_response = []

        result = await declaration_location(mock_lsp_client, temp_theory_file, 8, 20)

        assert result.locations == []

    @pytest.mark.asyncio
    async def test_definition_single_location(self, mock_lsp_client, temp_theory_file):
        """Test definition with single location (not array)."""
        mock_lsp_client.definition_response = {
            "uri": "file:///path/to/Test.thy",
            "range": {
                "start": {"line": 4, "character": 11},
                "end": {"line": 4, "character": 19}
            }
        }

        result = await declaration_location(mock_lsp_client, temp_theory_file, 8, 20)

        assert len(result.locations) == 1
        assert result.locations[0].line == 5  # Converted to 1-indexed

    @pytest.mark.asyncio
    async def test_definition_multiple_locations(self, mock_lsp_client, temp_theory_file):
        """Test definition with multiple locations."""
        mock_lsp_client.definition_response = [
            {
                "uri": "file:///path/to/Test1.thy",
                "range": {
                    "start": {"line": 4, "character": 11},
                    "end": {"line": 4, "character": 19}
                }
            },
            {
                "uri": "file:///path/to/Test2.thy",
                "range": {
                    "start": {"line": 10, "character": 5},
                    "end": {"line": 10, "character": 15}
                }
            }
        ]

        result = await declaration_location(mock_lsp_client, temp_theory_file, 8, 20)

        assert len(result.locations) == 2
        assert result.locations[0].file_path == "/path/to/Test1.thy"
        assert result.locations[1].file_path == "/path/to/Test2.thy"

    @pytest.mark.asyncio
    async def test_definition_location_link(self, mock_lsp_client, temp_theory_file):
        """Test definition with LocationLink format."""
        mock_lsp_client.definition_response = [
            {
                "targetUri": "file:///path/to/Test.thy",
                "targetRange": {
                    "start": {"line": 4, "character": 11},
                    "end": {"line": 4, "character": 19}
                },
                "targetSelectionRange": {
                    "start": {"line": 4, "character": 11},
                    "end": {"line": 4, "character": 19}
                }
            }
        ]

        result = await declaration_location(mock_lsp_client, temp_theory_file, 8, 20)

        assert len(result.locations) == 1
        assert result.locations[0].file_path == "/path/to/Test.thy"

    @pytest.mark.asyncio
    async def test_definition_auto_open(self, mock_lsp_client, temp_theory_file):
        """Test that definition auto-opens document."""
        assert temp_theory_file not in mock_lsp_client.open_documents

        mock_lsp_client.definition_response = []

        await declaration_location(mock_lsp_client, temp_theory_file, 8, 20)

        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_definition_symbol_extraction(self, mock_lsp_client, temp_theory_file):
        """Test symbol extraction at cursor position."""
        mock_lsp_client.definition_response = None

        # Position on "my_const" in the file
        result = await declaration_location(mock_lsp_client, temp_theory_file, 5, 15)

        # Should extract symbol from file
        assert result.symbol is not None
        # The exact symbol depends on file content and position

    @pytest.mark.asyncio
    async def test_definition_invalid_location(self, mock_lsp_client, temp_theory_file):
        """Test definition with malformed location."""
        mock_lsp_client.definition_response = [
            {
                # Missing required fields
                "invalid": "data"
            }
        ]

        result = await declaration_location(mock_lsp_client, temp_theory_file, 8, 20)

        # Should filter out invalid locations
        assert result.locations == []

    @pytest.mark.asyncio
    async def test_definition_position_conversion(self, mock_lsp_client, temp_theory_file):
        """Test position conversion LSP to MCP."""
        mock_lsp_client.definition_response = {
            "uri": "file:///test.thy",
            "range": {
                "start": {"line": 0, "character": 0},  # LSP 0-indexed
                "end": {"line": 0, "character": 10}
            }
        }

        result = await declaration_location(mock_lsp_client, temp_theory_file, 1, 1)

        # Should convert to MCP 1-indexed
        assert result.locations[0].line == 1
        assert result.locations[0].column == 1

    @pytest.mark.asyncio
    async def test_definition_file_not_found(self, mock_lsp_client):
        """Test definition with non-existent file."""
        with pytest.raises(FileNotFoundError):
            await declaration_location(mock_lsp_client, "/nonexistent/file.thy", 1, 1)

    @pytest.mark.asyncio
    async def test_definition_cross_file(self, mock_lsp_client, temp_theory_file):
        """Test definition pointing to different file."""
        mock_lsp_client.definition_response = {
            "uri": "file:///other/Theory.thy",
            "range": {
                "start": {"line": 100, "character": 50},
                "end": {"line": 100, "character": 60}
            }
        }

        result = await declaration_location(mock_lsp_client, temp_theory_file, 8, 20)

        assert len(result.locations) == 1
        # Should handle cross-file references
        assert "Theory.thy" in result.locations[0].file_path

    @pytest.mark.asyncio
    async def test_definition_symbol_with_special_chars(self, mock_lsp_client, temp_theory_file):
        """Test symbol extraction with Isabelle special characters."""
        mock_lsp_client.definition_response = None

        # The symbol extraction should handle Isabelle symbols
        result = await declaration_location(mock_lsp_client, temp_theory_file, 8, 20)

        # Should extract some symbol (may be empty if position is on whitespace)
        assert result.symbol is not None
