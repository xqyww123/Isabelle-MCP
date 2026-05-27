import pytest

from isa_lsp.evaluation import evaluation_state
from isa_lsp.tools.definition import declaration_location
from isa_lsp.utils import IsabelleToolError, MCPLine


class TestDefinitionTool:
    @pytest.mark.asyncio
    async def test_basic(self, mock_lsp_client, temp_theory_file, sample_definition_response):
        mock_lsp_client.definition_response = sample_definition_response
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(8), "my_const")
        assert len(result.locations) == 1
        assert result.locations[0].line == 5
        assert result.locations[0].column == 12

    @pytest.mark.asyncio
    async def test_empty(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.definition_response = []
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
        assert result.locations == []

    @pytest.mark.asyncio
    async def test_null_response(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.definition_response = None
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
        assert result.locations == []

    @pytest.mark.asyncio
    async def test_location_link(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.definition_response = [{
            "targetUri": "file:///other/file.thy",
            "targetRange": {"start": {"line": 9, "character": 4}, "end": {"line": 9, "character": 10}},
        }]
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
        assert len(result.locations) == 1
        assert result.locations[0].file_path == "/other/file.thy"
        assert result.locations[0].line == 10
        assert result.locations[0].column == 5

    @pytest.mark.asyncio
    async def test_location_link_selection_range_fallback(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.definition_response = [{
            "targetUri": "file:///other/file.thy",
            "targetSelectionRange": {"start": {"line": 3, "character": 2}, "end": {"line": 3, "character": 8}},
        }]
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
        assert len(result.locations) == 1
        assert result.locations[0].line == 4
        assert result.locations[0].column == 3

    @pytest.mark.asyncio
    async def test_multiple_locations(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.definition_response = [
            {"uri": "file:///a.thy", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}},
            {"uri": "file:///b.thy", "range": {"start": {"line": 1, "character": 1}, "end": {"line": 1, "character": 6}}},
        ]
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
        assert len(result.locations) == 2

    @pytest.mark.asyncio
    async def test_symbol_not_found(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="not found on line"):
            await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(5), "nonexistent")

    @pytest.mark.asyncio
    async def test_multi_occurrence_dedup(self, mock_lsp_client, tmp_path):
        f = tmp_path / "Dedup.thy"
        f.write_text("x = x\n")
        mock_lsp_client.definition_response = [{
            "uri": "file:///a.thy",
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
        }]
        result = await declaration_location(mock_lsp_client, str(f), MCPLine(1), "x")
        assert len(result.locations) == 1

    @pytest.mark.asyncio
    async def test_beyond_file_end(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.definition_response = None
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(1000), "x")
        assert result.symbol == "x"
        assert result.locations == []

    @pytest.mark.asyncio
    async def test_file_not_found(self, mock_lsp_client):
        with pytest.raises(FileNotFoundError):
            await declaration_location(mock_lsp_client, "/nonexistent/file.thy", MCPLine(1), "x")

    @pytest.mark.asyncio
    async def test_negative_line(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="line must be >= 1"):
            await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(-1), "x")

    @pytest.mark.asyncio
    async def test_zero_line(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="line must be >= 1"):
            await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(0), "x")

    @pytest.mark.asyncio
    async def test_evaluation_guard_blocks(self, mock_lsp_client, temp_theory_file):
        evaluation_state.start(temp_theory_file, MCPLine(100))
        with pytest.raises(IsabelleToolError, match="Evaluation in progress"):
            await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")

    @pytest.mark.asyncio
    async def test_malformed_location_ignored(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.definition_response = [
            {"no_uri": True},
            {"uri": "file:///a.thy", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}},
        ]
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
        assert len(result.locations) == 1

    @pytest.mark.asyncio
    async def test_lsp_error_skips_occurrence(self, mock_lsp_client, tmp_path):
        f = tmp_path / "Err.thy"
        f.write_text("x = x\n")
        call_count = 0

        def error_on_first(fp, line, char):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient LSP error")
            return [{"uri": "file:///a.thy", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}}]

        mock_lsp_client.definition_response = error_on_first
        result = await declaration_location(mock_lsp_client, str(f), MCPLine(1), "x")
        assert len(result.locations) == 1

    @pytest.mark.asyncio
    async def test_isabelle_tool_error_propagates(self, mock_lsp_client, tmp_path):
        f = tmp_path / "Fatal.thy"
        f.write_text("x = x\n")

        def fatal_error(fp, line, char):
            raise IsabelleToolError("LSP process crashed")

        mock_lsp_client.definition_response = fatal_error
        with pytest.raises(IsabelleToolError, match="LSP process crashed"):
            await declaration_location(mock_lsp_client, str(f), MCPLine(1), "x")
