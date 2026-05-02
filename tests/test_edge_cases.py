"""Edge cases and error handling tests."""

import asyncio

import pytest
from pydantic import ValidationError

from isa_lsp.models import HoverInfo, Location
from isa_lsp.tools.completions import completions
from isa_lsp.tools.diagnostics import diagnostic_messages
from isa_lsp.tools.highlights import document_highlights
from isa_lsp.tools.hover import hover_info
from isa_lsp.utils import IsabelleToolError, MCPColumn, MCPLine


class TestInvalidInput:
    @pytest.mark.asyncio
    async def test_negative_line(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="line must be >= 1"):
            await hover_info(mock_lsp_client, temp_theory_file, MCPLine(-1), MCPColumn(1))

    @pytest.mark.asyncio
    async def test_zero_line(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="line must be >= 1"):
            await hover_info(mock_lsp_client, temp_theory_file, MCPLine(0), MCPColumn(1))

    @pytest.mark.asyncio
    async def test_negative_column(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="column must be >= 1"):
            await hover_info(mock_lsp_client, temp_theory_file, MCPLine(1), MCPColumn(-1))

    @pytest.mark.asyncio
    async def test_nonexistent_file(self, mock_lsp_client):
        with pytest.raises(FileNotFoundError):
            await hover_info(mock_lsp_client, "/nonexistent/file.thy", MCPLine(1), MCPColumn(1))

    @pytest.mark.asyncio
    async def test_empty_file_path(self, mock_lsp_client):
        with pytest.raises((FileNotFoundError, IsabelleToolError)):
            await hover_info(mock_lsp_client, "", MCPLine(1), MCPColumn(1))


class TestModelValidation:
    def test_hover_missing_required_fields(self):
        with pytest.raises(ValidationError):
            HoverInfo(symbol="x", info="y", line_context=123)  # type: ignore[arg-type]

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
            hover_info(mock_lsp_client, temp_theory_file, MCPLine(i), MCPColumn(1)) for i in range(1, 6)
        ])
        assert len(results) == 5
        assert all(r.info == "test" for r in results)

    @pytest.mark.asyncio
    async def test_concurrent_different_tools(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = {"contents": "test"}
        mock_lsp_client.completion_response = {"items": []}
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []
        results = await asyncio.gather(
            hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15)),
            completions(mock_lsp_client, temp_theory_file, MCPLine(8), MCPColumn(1)),
            diagnostic_messages(mock_lsp_client, temp_theory_file, 1, -1),
        )
        assert len(results) == 3


class TestUnicodeHandling:
    @pytest.mark.asyncio
    async def test_hover_unicode(self, tmp_path, mock_lsp_client):
        f = tmp_path / "unicode.thy"
        f.write_text('lemma "∀x. P x ⟹ Q x"\n', encoding='utf-8')
        mock_lsp_client.hover_response = {"contents": "Universal quantifier: ∀"}
        result = await hover_info(mock_lsp_client, str(f), MCPLine(1), MCPColumn(8))
        assert isinstance(result.info, str)

    @pytest.mark.asyncio
    async def test_completion_unicode(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.completion_response = {
            "items": [
                {"label": "∀", "kind": 1, "detail": "Universal"},
                {"label": "∃", "kind": 1, "detail": "Existential"},
            ]
        }
        result = await completions(mock_lsp_client, temp_theory_file, MCPLine(8), MCPColumn(1))
        assert len(result.items) == 2


class TestEmptyResponses:
    @pytest.mark.asyncio
    async def test_hover_empty_contents(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = {"contents": ""}
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert result.info == ""

    @pytest.mark.asyncio
    async def test_highlights_single(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.highlights_response = [
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}, "kind": 1}
        ]
        result = await document_highlights(mock_lsp_client, temp_theory_file, MCPLine(1), MCPColumn(1))
        assert len(result.highlights) == 1


class TestLargeData:
    @pytest.mark.asyncio
    async def test_large_completion_list(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.completion_response = {
            "items": [{"label": f"item_{i}", "kind": 1, "detail": "x" * 1000} for i in range(1000)]
        }
        result = await completions(mock_lsp_client, temp_theory_file, MCPLine(8), MCPColumn(1), max_completions=100)
        assert len(result.items) == 100

    @pytest.mark.asyncio
    async def test_very_long_line(self, tmp_path, mock_lsp_client):
        f = tmp_path / "long.thy"
        f.write_text("x" * 100000 + "\n")
        mock_lsp_client.hover_response = {"contents": "test"}
        result = await hover_info(mock_lsp_client, str(f), MCPLine(1), MCPColumn(50000))
        assert result.info == "test"
