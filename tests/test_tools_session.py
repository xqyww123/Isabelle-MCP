"""
Unit tests for session management tools.
"""

from unittest.mock import AsyncMock, patch

import pytest

from isa_lsp.tools.session import build_session, session_info
from isa_lsp.utils import IsabelleToolError


class TestSessionInfoTool:
    """Test session_info tool."""

    @pytest.mark.asyncio
    async def test_session_info_basic(self, mock_lsp_client):
        """Test basic session info functionality."""
        result = await session_info(mock_lsp_client)

        assert result.current_session == "HOL"
        assert isinstance(result.available_sessions, list)
        assert len(result.available_sessions) > 0

    @pytest.mark.asyncio
    async def test_session_info_includes_common_sessions(self, mock_lsp_client):
        """Test that session info includes common sessions."""
        result = await session_info(mock_lsp_client)

        # Should include common sessions
        assert "HOL" in result.available_sessions
        assert "Pure" in result.available_sessions
        assert "Main" in result.available_sessions

    @pytest.mark.asyncio
    async def test_session_info_custom_logic(self):
        """Test session info with custom logic."""
        from tests.conftest import MockLSPClient

        client = MockLSPClient()
        client.logic = "Main"

        result = await session_info(client)

        assert result.current_session == "Main"


class TestBuildSessionTool:
    """Test build_session tool."""

    @pytest.mark.asyncio
    async def test_build_session_success(self, mock_lsp_client):
        """Test successful session build."""
        with patch('asyncio.create_subprocess_exec') as mock_subprocess:
            # Mock successful build
            mock_process = AsyncMock()
            mock_process.communicate = AsyncMock(return_value=(
                b"Building session HOL...\nFinished building HOL\n",
                b""
            ))
            mock_process.returncode = 0
            mock_subprocess.return_value = mock_process

            result = await build_session(mock_lsp_client, "HOL")

            assert result.success is True
            assert result.session == "HOL"
            assert len(result.messages) > 0

    @pytest.mark.asyncio
    async def test_build_session_failure(self, mock_lsp_client):
        """Test failed session build."""
        with patch('asyncio.create_subprocess_exec') as mock_subprocess:
            # Mock failed build
            mock_process = AsyncMock()
            mock_process.communicate = AsyncMock(return_value=(
                b"Building session HOL...\n",
                b"Error: Build failed\n"
            ))
            mock_process.returncode = 1
            mock_subprocess.return_value = mock_process

            result = await build_session(mock_lsp_client, "HOL")

            assert result.success is False
            assert result.session == "HOL"
            assert any("Error" in msg or "failed" in msg for msg in result.messages)

    @pytest.mark.asyncio
    async def test_build_session_with_clean(self, mock_lsp_client):
        """Test session build with clean flag."""
        with patch('asyncio.create_subprocess_exec') as mock_subprocess:
            mock_process = AsyncMock()
            mock_process.communicate = AsyncMock(return_value=(b"Success", b""))
            mock_process.returncode = 0
            mock_subprocess.return_value = mock_process

            result = await build_session(mock_lsp_client, "HOL", clean=True)

            # Verify clean flag was used
            call_args = mock_subprocess.call_args[0]
            assert "-c" in call_args

    @pytest.mark.asyncio
    async def test_build_session_command_construction(self, mock_lsp_client):
        """Test that build command is constructed correctly."""
        with patch('asyncio.create_subprocess_exec') as mock_subprocess:
            mock_process = AsyncMock()
            mock_process.communicate = AsyncMock(return_value=(b"", b""))
            mock_process.returncode = 0
            mock_subprocess.return_value = mock_process

            await build_session(mock_lsp_client, "Main")

            # Check command was correct
            call_args = mock_subprocess.call_args[0]
            assert "isabelle" in call_args
            assert "build" in call_args
            assert "-b" in call_args
            assert "Main" in call_args

    @pytest.mark.asyncio
    async def test_build_session_exception(self, mock_lsp_client):
        """Test build session with subprocess exception."""
        with patch('asyncio.create_subprocess_exec') as mock_subprocess:
            mock_subprocess.side_effect = FileNotFoundError("isabelle not found")

            with pytest.raises(IsabelleToolError, match="isabelle not found"):
                await build_session(mock_lsp_client, "HOL")

    @pytest.mark.asyncio
    async def test_build_session_output_parsing(self, mock_lsp_client):
        """Test parsing of build output messages."""
        with patch('asyncio.create_subprocess_exec') as mock_subprocess:
            mock_process = AsyncMock()
            mock_process.communicate = AsyncMock(return_value=(
                b"Line 1\nLine 2\nLine 3\n",
                b"Warning line\n"
            ))
            mock_process.returncode = 0
            mock_subprocess.return_value = mock_process

            result = await build_session(mock_lsp_client, "HOL")

            # Should include both stdout and stderr
            assert len(result.messages) >= 4  # At least 4 non-empty lines

    @pytest.mark.asyncio
    async def test_build_session_unicode_handling(self, mock_lsp_client):
        """Test handling of unicode in build output."""
        with patch('asyncio.create_subprocess_exec') as mock_subprocess:
            mock_process = AsyncMock()
            # Unicode output
            mock_process.communicate = AsyncMock(return_value=(
                "Building session: ∀ x. P x → Q x".encode(),
                b""
            ))
            mock_process.returncode = 0
            mock_subprocess.return_value = mock_process

            result = await build_session(mock_lsp_client, "HOL")

            # Should handle unicode correctly
            assert result.success is True
            assert any("∀" in msg or "Building" in msg for msg in result.messages)

    @pytest.mark.asyncio
    async def test_build_session_empty_output(self, mock_lsp_client):
        """Test build session with empty output."""
        with patch('asyncio.create_subprocess_exec') as mock_subprocess:
            mock_process = AsyncMock()
            mock_process.communicate = AsyncMock(return_value=(b"", b""))
            mock_process.returncode = 0
            mock_subprocess.return_value = mock_process

            result = await build_session(mock_lsp_client, "Pure")

            assert result.success is True
            # Messages might be empty or have minimal content
            assert isinstance(result.messages, list)
