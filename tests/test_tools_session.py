from unittest.mock import AsyncMock, patch

import pytest

from isa_lsp.tools.session import build_session, session_info
from isa_lsp.utils import IsabelleToolError


class TestSessionTool:
    @pytest.mark.asyncio
    async def test_session_info(self, mock_lsp_client):
        result = await session_info(mock_lsp_client)
        assert result.current_session == "HOL"

    @pytest.mark.asyncio
    async def test_build_success(self, mock_lsp_client):
        with patch('asyncio.create_subprocess_exec') as mock_sub:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"Building HOL\nSuccess", b""))
            mock_proc.returncode = 0
            mock_sub.return_value = mock_proc
            result = await build_session(mock_lsp_client, "HOL")
        assert result.success is True
        assert result.session == "HOL"
        assert any("Success" in m for m in result.messages)

    @pytest.mark.asyncio
    async def test_build_failure(self, mock_lsp_client):
        with patch('asyncio.create_subprocess_exec') as mock_sub:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"Error: failed"))
            mock_proc.returncode = 1
            mock_sub.return_value = mock_proc
            result = await build_session(mock_lsp_client, "HOL")
        assert result.success is False
        assert any("Error" in m for m in result.messages)

    @pytest.mark.asyncio
    async def test_build_clean(self, mock_lsp_client):
        with patch('asyncio.create_subprocess_exec') as mock_sub:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            mock_sub.return_value = mock_proc
            await build_session(mock_lsp_client, "HOL", clean=True)
        assert "-c" in mock_sub.call_args[0]

    @pytest.mark.asyncio
    async def test_build_spawn_failure(self, mock_lsp_client):
        with patch('asyncio.create_subprocess_exec', side_effect=FileNotFoundError("isabelle")):
            with pytest.raises(IsabelleToolError, match="Failed to build session 'HOL'"):
                await build_session(mock_lsp_client, "HOL")
