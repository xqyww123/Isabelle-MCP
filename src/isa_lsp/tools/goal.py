from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import GoalState
from isa_lsp.utils import get_line_from_file, validate_position


async def goal(
    client: IsabelleLSPClient,
    file_path: str,
    line: int,
    column: int | None = None,
) -> GoalState:
    validate_position(line, column if column is not None else 1)

    if file_path not in client.open_documents:
        await client.open_document(file_path)

    line_context = get_line_from_file(file_path, line)

    if column is None:
        goals_before = await client.get_goals_at_position(file_path, line - 1, 0)
        goals_after = await client.get_goals_at_position(file_path, line - 1, len(line_context))
        return GoalState(
            line_context=line_context,
            goals_before=goals_before, goals_after=goals_after,
        )

    goals = await client.get_goals_at_position(file_path, line - 1, column - 1)
    return GoalState(line_context=line_context, goals=goals)
