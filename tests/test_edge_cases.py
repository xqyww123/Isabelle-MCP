"""Edge cases and error handling tests."""

import asyncio

import pytest
from pydantic import ValidationError

from isabelle_mcp.evaluation import evaluation_state
from isabelle_mcp.models import HoverEntry, HoverInfo, Location
from isabelle_mcp.tools.diagnostics import diagnostic_messages
from isabelle_mcp.tools.hover import hover_info
from isabelle_mcp.tools.local_occurrences import local_occurrences
from isabelle_mcp.utils import IsabelleToolError, MCPLine


class TestInvalidInput:
    @pytest.mark.asyncio
    async def test_negative_line(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="line must be >= 1"):
            await hover_info(mock_lsp_client, temp_theory_file, MCPLine(-1), "x")

    @pytest.mark.asyncio
    async def test_zero_line(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="line must be >= 1"):
            await hover_info(mock_lsp_client, temp_theory_file, MCPLine(0), "x")

    @pytest.mark.asyncio
    async def test_nonexistent_file(self, mock_lsp_client):
        with pytest.raises(FileNotFoundError):
            await hover_info(mock_lsp_client, "/nonexistent/file.thy", MCPLine(1), "x")

    @pytest.mark.asyncio
    async def test_empty_file_path(self, mock_lsp_client):
        with pytest.raises((FileNotFoundError, IsabelleToolError)):
            await hover_info(mock_lsp_client, "", MCPLine(1), "x")


class TestModelValidation:
    def test_hover_missing_required_fields(self):
        with pytest.raises(ValidationError):
            HoverInfo(symbol="x", results=[], line_context=123)  # type: ignore[arg-type]

    def test_hover_entry_mismatched_lengths(self):
        with pytest.raises(ValidationError):
            HoverEntry(info="test", occurrences=[1, 2], columns=[1])

    def test_hover_entry_valid(self):
        entry = HoverEntry(info="test", occurrences=[1, 2], columns=[3, 7])
        assert entry.occurrences == [1, 2]
        assert entry.columns == [3, 7]

    def test_location_zero_line(self):
        with pytest.raises(ValidationError):
            Location(file_path="/test.thy", line=0, column=1)

    def test_location_zero_column(self):
        with pytest.raises(ValidationError):
            Location(file_path="/test.thy", line=1, column=0)


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_hover(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = {"contents": "test"}
        results = await asyncio.gather(*[
            hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
            for _ in range(5)
        ])
        assert len(results) == 5
        assert all(len(r.results) >= 1 for r in results)

    @pytest.mark.asyncio
    async def test_concurrent_different_tools(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = {"contents": "test"}
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []
        results = await asyncio.gather(
            hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const"),
            diagnostic_messages(mock_lsp_client, temp_theory_file, 1, -1),
        )
        assert len(results) == 2


class TestEvaluationGuard:
    @pytest.mark.asyncio
    async def test_query_during_evaluation_fails(self, mock_lsp_client, temp_theory_file):
        evaluation_state.start(temp_theory_file, MCPLine(100))
        with pytest.raises(IsabelleToolError, match="Evaluation in progress"):
            await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")


class TestUnicodeHandling:
    @pytest.mark.asyncio
    async def test_hover_unicode(self, tmp_path, mock_lsp_client):
        f = tmp_path / "unicode.thy"
        f.write_text('lemma "∀x. P x ⟹ Q x"\n', encoding='utf-8')
        mock_lsp_client.hover_response = {"contents": "Universal quantifier: ∀"}
        result = await hover_info(mock_lsp_client, str(f), MCPLine(1), "P")
        assert len(result.results) >= 1

    @pytest.mark.asyncio
    async def test_hover_unicode_symbol(self, tmp_path, mock_lsp_client):
        f = tmp_path / "unicode2.thy"
        f.write_text('lemma "P ⟹ Q"\n', encoding='utf-8')
        mock_lsp_client.hover_response = {"contents": "implication"}
        result = await hover_info(mock_lsp_client, str(f), MCPLine(1), "⟹")
        assert len(result.results) >= 1
        assert result.results[0].info == "implication"


class TestEmptyResponses:
    @pytest.mark.asyncio
    async def test_hover_empty_contents(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = {"contents": ""}
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
        assert result.results[0].info == ""

    @pytest.mark.asyncio
    async def test_local_occurrences_single(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.highlights_response = [
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}, "kind": 1}
        ]
        result = await local_occurrences(mock_lsp_client, temp_theory_file, MCPLine(8), "my_const")
        assert len(result.occurrences) == 1


class TestLargeData:
    @pytest.mark.asyncio
    async def test_very_long_line(self, tmp_path, mock_lsp_client):
        f = tmp_path / "long.thy"
        f.write_text("x " * 50000 + "\n")
        mock_lsp_client.hover_response = {"contents": "test"}
        result = await hover_info(mock_lsp_client, str(f), MCPLine(1), "x")
        assert len(result.results) >= 1
        assert len(result.results[0].occurrences) == 10  # capped at 10
