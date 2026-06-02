import pytest

from isabelle_mcp.tools.local_occurrences import local_occurrences
from isabelle_mcp.utils import IsabelleToolError, MCPLine


class TestLocalOccurrencesTool:
    @pytest.mark.asyncio
    async def test_basic(self, mock_lsp_client, temp_theory_file, sample_highlights_response):
        mock_lsp_client.highlights_response = sample_highlights_response
        result = await local_occurrences(mock_lsp_client, temp_theory_file, MCPLine(8), "my_const")
        assert len(result.occurrences) == 2
        # sorted by (line, start_column): definition site first, then use
        assert (result.occurrences[0].line, result.occurrences[0].start_column) == (5, 12)
        assert (result.occurrences[1].line, result.occurrences[1].start_column) == (8, 21)

    @pytest.mark.asyncio
    async def test_empty(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.highlights_response = []
        result = await local_occurrences(mock_lsp_client, temp_theory_file, MCPLine(8), "my_const")
        assert result.occurrences == []

    @pytest.mark.asyncio
    async def test_null_response(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.highlights_response = None
        result = await local_occurrences(mock_lsp_client, temp_theory_file, MCPLine(8), "my_const")
        assert result.occurrences == []

    @pytest.mark.asyncio
    async def test_single_occurrence(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.highlights_response = [
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}, "kind": 1}
        ]
        result = await local_occurrences(mock_lsp_client, temp_theory_file, MCPLine(8), "my_const")
        assert len(result.occurrences) == 1
        assert result.occurrences[0].line == 1
        assert result.occurrences[0].start_column == 1
        assert result.occurrences[0].end_column == 6

    @pytest.mark.asyncio
    async def test_invalid_occurrence_skipped(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.highlights_response = [
            {"kind": 1},
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}, "kind": 1},
        ]
        result = await local_occurrences(mock_lsp_client, temp_theory_file, MCPLine(8), "my_const")
        assert len(result.occurrences) == 1

    @pytest.mark.asyncio
    async def test_symbol_not_found(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.highlights_response = []
        with pytest.raises(IsabelleToolError, match="not found on line"):
            await local_occurrences(mock_lsp_client, temp_theory_file, MCPLine(8), "nonexistent")

    @pytest.mark.asyncio
    async def test_dedup_across_line_occurrences(self, tmp_path, mock_lsp_client):
        # symbol appears twice on the line → queried twice → merged to one occurrence
        f = tmp_path / "Dup.thy"
        f.write_text(
            'theory Dup\n'
            'imports Main\n'
            'begin\n'
            'lemma l: "add_one (add_one n) = n"\n'
            'end\n'
        )
        mock_lsp_client.highlights_response = [
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 7}}, "kind": 2}
        ]
        result = await local_occurrences(mock_lsp_client, str(f), MCPLine(4), "add_one")
        assert len(result.occurrences) == 1
