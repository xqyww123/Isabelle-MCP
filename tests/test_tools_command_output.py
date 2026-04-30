import pytest

from isa_lsp.tools.command_output import command_output
from isa_lsp.utils import IsabelleToolError


class TestCommandOutputTool:
    @pytest.mark.asyncio
    async def test_empty(self, mock_lsp_client, temp_theory_file):
        result = await command_output(mock_lsp_client, temp_theory_file, 8)
        assert result.messages == []
        assert result.line_context != ""

    @pytest.mark.asyncio
    async def test_with_output(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.dynamic_output_response = '<div class="writeln">Success</div>'
        result = await command_output(mock_lsp_client, temp_theory_file, 8)
        assert len(result.messages) == 1
        assert result.messages[0].kind == "writeln"
        assert result.messages[0].message == "Success"

    @pytest.mark.asyncio
    async def test_invalid_line(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="line must be >= 1"):
            await command_output(mock_lsp_client, temp_theory_file, 0)
