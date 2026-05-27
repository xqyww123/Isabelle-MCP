from isabelle_mcp.evaluation import check_evaluation_guard
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import EvaluationResult, GoalState
from isabelle_mcp.utils import (
    IsabelleToolError,
    MCPColumn,
    MCPLine,
    get_line_from_file,
    validate_position,
)


async def goal(
    client: IsabelleLSPClient,
    file_path: str,
    line: MCPLine,
    column: MCPColumn | None = None,
) -> GoalState:
    validate_position(line, column if column is not None else MCPColumn(1))

    await client.open_document(file_path)

    guard = await check_evaluation_guard(client, file_path, line)
    if isinstance(guard, EvaluationResult):
        raise IsabelleToolError(guard.message)
    note = guard if isinstance(guard, str) else None

    line_context = get_line_from_file(file_path, line)

    if column is None:
        goals_before = await client.get_goals_at_position(file_path, line.to_lsp(), 0)
        goals_after = await client.get_goals_at_position(
            file_path, line.to_lsp(), len(line_context),
        )
        return GoalState(
            line_context=line_context,
            goals_before=goals_before, goals_after=goals_after,
            note=note,
        )

    goals = await client.get_goals_at_position(
        file_path, line.to_lsp(), column.to_lsp(),
    )
    return GoalState(line_context=line_context, goals=goals, note=note)
