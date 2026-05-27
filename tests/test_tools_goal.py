import pytest

from isabelle_mcp.tools.goal import goal
from isabelle_mcp.utils import IsabelleToolError, MCPColumn, MCPLine


class TestGoalTool:
    @pytest.mark.asyncio
    async def test_without_column(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.goal_response = ["P ⟹ Q"]
        result = await goal(mock_lsp_client, temp_theory_file, MCPLine(8))
        assert result.goals is None
        assert result.goals_before == ["P ⟹ Q"]
        assert result.goals_after == ["P ⟹ Q"]
        assert result.line_context != ""

    @pytest.mark.asyncio
    async def test_with_column(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.goal_response = ["Q ⟹ R"]
        result = await goal(mock_lsp_client, temp_theory_file, MCPLine(8), column=MCPColumn(5))
        assert result.goals == ["Q ⟹ R"]
        assert result.goals_before is None
        assert result.goals_after is None

    @pytest.mark.asyncio
    async def test_empty_goals(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.goal_response = []
        result = await goal(mock_lsp_client, temp_theory_file, MCPLine(8))
        assert result.goals_before == []
        assert result.goals_after == []

    @pytest.mark.asyncio
    async def test_invalid_line(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="line must be >= 1"):
            await goal(mock_lsp_client, temp_theory_file, MCPLine(0))

    @pytest.mark.asyncio
    async def test_invalid_column(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="column must be >= 1"):
            await goal(mock_lsp_client, temp_theory_file, MCPLine(8), column=MCPColumn(0))
