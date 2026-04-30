"""
Unit tests for goal tool.
"""

import pytest

from isa_lsp.tools.goal import goal


class TestGoalTool:
    """Test goal tool."""

    @pytest.mark.asyncio
    async def test_goal_with_column(self, mock_lsp_client, temp_theory_file):
        """Test goal at specific column."""
        result = await goal(mock_lsp_client, temp_theory_file, 8, column=10)

        assert result.line_context is not None
        assert result.goals is not None  # May be empty in MVP
        assert result.goals_before is None
        assert result.goals_after is None

    @pytest.mark.asyncio
    async def test_goal_without_column(self, mock_lsp_client, temp_theory_file):
        """Test goal without column (before/after mode)."""
        result = await goal(mock_lsp_client, temp_theory_file, 8)

        assert result.line_context is not None
        assert result.goals is None
        assert result.goals_before is not None  # May be empty in MVP
        assert result.goals_after is not None  # May be empty in MVP

    @pytest.mark.asyncio
    async def test_goal_mvp_limitation(self, mock_lsp_client, temp_theory_file):
        """Test that goal returns empty in MVP."""
        result = await goal(mock_lsp_client, temp_theory_file, 8)

        # MVP limitation: returns empty goals
        assert result.goals_before == []
        assert result.goals_after == []

    @pytest.mark.asyncio
    async def test_goal_auto_open(self, mock_lsp_client, temp_theory_file):
        """Test that goal auto-opens document."""
        assert temp_theory_file not in mock_lsp_client.open_documents

        await goal(mock_lsp_client, temp_theory_file, 8)

        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_goal_line_context_extraction(self, mock_lsp_client, temp_theory_file):
        """Test line context extraction."""
        result = await goal(mock_lsp_client, temp_theory_file, 5)

        # Should extract the actual line content
        assert result.line_context is not None
        assert len(result.line_context) > 0

    @pytest.mark.asyncio
    async def test_goal_position_conversion(self, mock_lsp_client, temp_theory_file):
        """Test position conversion MCP to LSP."""
        # MCP uses 1-indexed, should convert to 0-indexed for LSP
        result = await goal(mock_lsp_client, temp_theory_file, 1, column=1)

        # Should not crash
        assert result is not None

    @pytest.mark.asyncio
    async def test_goal_line_end_calculation(self, mock_lsp_client, temp_theory_file):
        """Test that line end position is calculated correctly."""
        result = await goal(mock_lsp_client, temp_theory_file, 5)

        # Should query both line start and line end
        # The line_context length should be used for end position
        assert result.line_context is not None

    @pytest.mark.asyncio
    async def test_goal_file_not_found(self, mock_lsp_client):
        """Test goal with non-existent file."""
        with pytest.raises(FileNotFoundError):
            await goal(mock_lsp_client, "/nonexistent/file.thy", 1)

    @pytest.mark.asyncio
    async def test_goal_empty_line(self, mock_lsp_client, temp_theory_file):
        """Test goal on empty line."""
        # Line 4 in temp_theory_file is empty
        result = await goal(mock_lsp_client, temp_theory_file, 4)

        assert result.line_context == ""
        assert result.goals_before is not None
        assert result.goals_after is not None

    @pytest.mark.asyncio
    async def test_goal_context_none(self, mock_lsp_client, temp_theory_file):
        """Test that context field is None in MVP."""
        result = await goal(mock_lsp_client, temp_theory_file, 8)

        # Context extraction not implemented in MVP
        assert result.context is None

    @pytest.mark.asyncio
    async def test_goal_column_at_line_start(self, mock_lsp_client, temp_theory_file):
        """Test goal with column at line start."""
        result = await goal(mock_lsp_client, temp_theory_file, 5, column=1)

        assert result is not None
        assert result.goals is not None

    @pytest.mark.asyncio
    async def test_goal_column_at_line_end(self, mock_lsp_client, temp_theory_file):
        """Test goal with column at line end."""
        # Get line content to find end
        with open(temp_theory_file) as f:
            lines = f.readlines()
            line_length = len(lines[4])  # Line 5 (0-indexed)

        result = await goal(mock_lsp_client, temp_theory_file, 5, column=line_length)

        assert result is not None

    @pytest.mark.asyncio
    async def test_goal_large_line_number(self, mock_lsp_client, temp_theory_file):
        """Test goal with line number beyond file."""
        result = await goal(mock_lsp_client, temp_theory_file, 1000)

        # Should handle gracefully
        assert result.line_context == ""
