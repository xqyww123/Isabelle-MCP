import pytest

from isabelle_mcp.tools.highlights import document_highlights
from isabelle_mcp.utils import MCPColumn, MCPLine


class TestHighlightsTool:
    @pytest.mark.asyncio
    async def test_basic(self, mock_lsp_client, temp_theory_file, sample_highlights_response):
        mock_lsp_client.highlights_response = sample_highlights_response
        result = await document_highlights(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert len(result.highlights) == 2
        assert result.highlights[0].kind == "text"
        assert result.highlights[1].kind == "read"

    @pytest.mark.asyncio
    async def test_empty(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.highlights_response = []
        result = await document_highlights(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert result.highlights == []

    @pytest.mark.asyncio
    async def test_null_response(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.highlights_response = None
        result = await document_highlights(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert result.highlights == []

    @pytest.mark.asyncio
    async def test_single_occurrence(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.highlights_response = [
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}, "kind": 1}
        ]
        result = await document_highlights(mock_lsp_client, temp_theory_file, MCPLine(1), MCPColumn(1))
        assert len(result.highlights) == 1
        assert result.highlights[0].line == 1
        assert result.highlights[0].start_column == 1
        assert result.highlights[0].end_column == 6

    @pytest.mark.asyncio
    async def test_invalid_highlight_skipped(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.highlights_response = [
            {"kind": 1},
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}, "kind": 1},
        ]
        result = await document_highlights(mock_lsp_client, temp_theory_file, MCPLine(1), MCPColumn(1))
        assert len(result.highlights) == 1
