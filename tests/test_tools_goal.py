import pytest

from isabelle_mcp.tools.goal import goal
from isabelle_mcp.utils import IsabelleToolError, MCPLine

# LSP 0-indexed range for a "by simp" command on the 9th line (lsp line 8).
CMD = ("by simp", {"start": {"line": 8, "character": 2}, "end": {"line": 8, "character": 9}})


class TestGoalTool:
    @pytest.mark.asyncio
    async def test_default_end_of_line(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.goal_response = ["P ⟹ Q"]
        mock_lsp_client.command_at_position_response = CMD
        result = await goal(mock_lsp_client, temp_theory_file, MCPLine(9))
        assert result.subgoals == ["P ⟹ Q"]
        assert result.command is not None
        assert result.command.text == "by simp"
        assert result.command.start_line == 9      # lsp 8 -> 1-indexed 9
        assert result.command.start_column == 3     # lsp char 2 -> 1-indexed 3
        assert result.command.end_column == 10      # lsp char 9 -> 1-indexed 10

    @pytest.mark.asyncio
    async def test_with_after_text(self, mock_lsp_client, temp_theory_file):
        # Line 9 is "  by (simp add: my_const_def)"
        mock_lsp_client.goal_response = ["Q ⟹ R"]
        mock_lsp_client.command_at_position_response = CMD
        result = await goal(
            mock_lsp_client, temp_theory_file, MCPLine(9), after_text="by",
        )
        assert result.subgoals == ["Q ⟹ R"]
        assert result.command is not None
        assert result.command.text == "by simp"

    @pytest.mark.asyncio
    async def test_command_none(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.goal_response = []
        mock_lsp_client.command_at_position_response = None
        result = await goal(mock_lsp_client, temp_theory_file, MCPLine(9))
        assert result.command is None
        assert result.subgoals == []

    @pytest.mark.asyncio
    async def test_after_text_not_found(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="not found on line"):
            await goal(
                mock_lsp_client, temp_theory_file, MCPLine(9), after_text="no_such_text",
            )

    @pytest.mark.asyncio
    async def test_invalid_line(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="line must be >= 1"):
            await goal(mock_lsp_client, temp_theory_file, MCPLine(0))
