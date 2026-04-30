import pytest

from isa_lsp.tools.preview import preview_document
from isa_lsp.utils import IsabelleToolError


class TestPreviewTool:
    @pytest.mark.asyncio
    async def test_basic(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.preview_response = {"content": "<html>Preview</html>"}
        result = await preview_document(mock_lsp_client, temp_theory_file)
        assert result.html == "<html>Preview</html>"
        assert result.line_context is None

    @pytest.mark.asyncio
    async def test_with_line(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.preview_response = {"content": "<html></html>"}
        result = await preview_document(mock_lsp_client, temp_theory_file, line=5)
        assert result.line_context != ""

    @pytest.mark.asyncio
    async def test_invalid_line(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="line must be >= 1"):
            await preview_document(mock_lsp_client, temp_theory_file, line=0)
