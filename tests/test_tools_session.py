import pytest

from isabelle_mcp.tools.session import session_info


class TestSessionTool:
    @pytest.mark.asyncio
    async def test_session_info(self, mock_lsp_client):
        result = await session_info(mock_lsp_client)
        assert result.current_session == "HOL"
        assert result.debug is False

    @pytest.mark.asyncio
    async def test_session_info_reports_debug(self, mock_lsp_client):
        mock_lsp_client.debug = True
        result = await session_info(mock_lsp_client)
        assert result.debug is True
