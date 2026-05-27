import pytest

from isa_lsp.tools.session import session_info


class TestSessionTool:
    @pytest.mark.asyncio
    async def test_session_info(self, mock_lsp_client):
        result = await session_info(mock_lsp_client)
        assert result.current_session == "HOL"
