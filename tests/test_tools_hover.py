import pytest

from isa_lsp.tools.hover import hover_info
from isa_lsp.utils import MCPColumn, MCPLine


class TestHoverTool:
    @pytest.mark.asyncio
    async def test_basic(self, mock_lsp_client, temp_theory_file, sample_hover_response):
        mock_lsp_client.hover_response = sample_hover_response
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert isinstance(result.symbol, str)
        assert isinstance(result.info, str)
        assert result.line_context != ""

    @pytest.mark.asyncio
    async def test_markdown_contents(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = {
            "contents": {"kind": "markdown", "value": "**Symbol**: `my_const`\n\nType: `nat`"}
        }
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert "my_const" in result.info

    @pytest.mark.asyncio
    async def test_plaintext_contents(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = {"contents": "Simple text content"}
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert result.info == "Simple text content"

    @pytest.mark.asyncio
    async def test_null_response(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = None
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert result.info == ""

    @pytest.mark.asyncio
    async def test_auto_opens_document(self, mock_lsp_client, temp_theory_file):
        assert temp_theory_file not in mock_lsp_client.open_documents
        mock_lsp_client.hover_response = {"contents": "test"}
        await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_with_diagnostics(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [{
            "range": {"start": {"line": 4, "character": 0}, "end": {"line": 4, "character": 10}},
            "severity": 1, "message": "Type error",
        }]
        mock_lsp_client.hover_response = {"contents": "test"}
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(1))
        assert len(result.diagnostics) == 1
        assert result.diagnostics[0].severity == "error"
        assert result.diagnostics[0].message == "Type error"

    @pytest.mark.asyncio
    async def test_beyond_file_end(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = None
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(1000), MCPColumn(1))
        assert result.symbol == ""
        assert result.line_context == ""

    @pytest.mark.asyncio
    async def test_file_not_found(self, mock_lsp_client):
        with pytest.raises(FileNotFoundError):
            await hover_info(mock_lsp_client, "/nonexistent/file.thy", MCPLine(1), MCPColumn(1))

    @pytest.mark.asyncio
    async def test_array_contents(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = {
            "contents": [{"language": "isabelle", "value": "definition"}, "Additional info"]
        }
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), MCPColumn(15))
        assert "definition" in result.info
        assert "Additional info" in result.info
