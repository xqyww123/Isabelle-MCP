import pytest

from isabelle_mcp.tools.diagnostics import diagnostic_messages
from isabelle_mcp.utils import IsabelleToolError


class TestDiagnosticsTool:
    @pytest.mark.asyncio
    async def test_empty(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []
        mock_lsp_client.processing_status[temp_theory_file] = True
        result = await diagnostic_messages(mock_lsp_client, temp_theory_file, 1, -1)
        assert result.success is True
        assert result.items == []
        assert result.processing_complete is True

    @pytest.mark.asyncio
    async def test_with_errors(self, mock_lsp_client, temp_theory_file, sample_diagnostics):
        mock_lsp_client.diagnostics_cache[temp_theory_file] = sample_diagnostics
        mock_lsp_client.processing_status[temp_theory_file] = True
        result = await diagnostic_messages(mock_lsp_client, temp_theory_file, 1, -1)
        assert result.success is False
        assert len(result.items) == 2
        assert result.items[0].severity == "error"
        assert result.items[1].severity == "warning"

    @pytest.mark.asyncio
    async def test_line_filter(self, mock_lsp_client, temp_theory_file, sample_diagnostics):
        mock_lsp_client.diagnostics_cache[temp_theory_file] = sample_diagnostics
        result = await diagnostic_messages(mock_lsp_client, temp_theory_file, start_line=5, end_line=5)
        assert len(result.items) == 1
        assert result.items[0].line == 5

    @pytest.mark.asyncio
    async def test_large_count(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {"range": {"start": {"line": i, "character": 0}, "end": {"line": i, "character": 10}},
             "severity": 1, "message": f"Error {i}"}
            for i in range(1000)
        ]
        result = await diagnostic_messages(mock_lsp_client, temp_theory_file, 1, 1000)
        assert len(result.items) == 1000

    @pytest.mark.asyncio
    async def test_auto_opens_document(self, mock_lsp_client, temp_theory_file):
        assert temp_theory_file not in mock_lsp_client.open_documents
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []
        await diagnostic_messages(mock_lsp_client, temp_theory_file, 1, -1)
        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_incomplete_empty_cache_is_not_success(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []
        mock_lsp_client.processing_status[temp_theory_file] = False
        result = await diagnostic_messages(mock_lsp_client, temp_theory_file, 1, -1)
        assert result.success is False
        assert result.items == []
        assert result.processing_complete is False

    @pytest.mark.asyncio
    async def test_incomplete_warning_only_cache_is_not_success(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {
                "range": {"start": {"line": 4, "character": 0}, "end": {"line": 4, "character": 10}},
                "severity": 2,
                "message": "Still processing",
            }
        ]
        mock_lsp_client.processing_status[temp_theory_file] = False
        result = await diagnostic_messages(mock_lsp_client, temp_theory_file, 1, -1)
        assert result.success is False
        assert result.processing_complete is False

    @pytest.mark.asyncio
    async def test_invalid_filter_lines(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="start_line must be >= 1"):
            await diagnostic_messages(mock_lsp_client, temp_theory_file, start_line=0, end_line=1)
        with pytest.raises(IsabelleToolError, match="end_line must be >= 1"):
            await diagnostic_messages(mock_lsp_client, temp_theory_file, start_line=1, end_line=0)
        with pytest.raises(IsabelleToolError, match="start_line must be <= end_line"):
            await diagnostic_messages(mock_lsp_client, temp_theory_file, start_line=10, end_line=5)
