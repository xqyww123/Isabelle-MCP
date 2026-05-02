import pytest

from isa_lsp.tools.definition import declaration_location
from isa_lsp.utils import MCPColumn, MCPLine


class TestDefinitionTool:
    @pytest.mark.asyncio
    async def test_basic(self, mock_lsp_client, temp_theory_file, sample_definition_response):
        mock_lsp_client.definition_response = sample_definition_response
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(8), MCPColumn(20))
        assert len(result.locations) == 1
        assert result.locations[0].line == 5
        assert result.locations[0].column == 12

    @pytest.mark.asyncio
    async def test_empty(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.definition_response = []
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert result.locations == []

    @pytest.mark.asyncio
    async def test_null_response(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.definition_response = None
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert result.locations == []

    @pytest.mark.asyncio
    async def test_location_link(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.definition_response = [{
            "targetUri": "file:///other/file.thy",
            "targetRange": {"start": {"line": 9, "character": 4}, "end": {"line": 9, "character": 10}},
        }]
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert len(result.locations) == 1
        assert result.locations[0].file_path == "/other/file.thy"
        assert result.locations[0].line == 10
        assert result.locations[0].column == 5

    @pytest.mark.asyncio
    async def test_multiple_locations(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.definition_response = [
            {"uri": "file:///a.thy", "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}}},
            {"uri": "file:///b.thy", "range": {"start": {"line": 1, "character": 1}, "end": {"line": 1, "character": 6}}},
        ]
        result = await declaration_location(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert len(result.locations) == 2
