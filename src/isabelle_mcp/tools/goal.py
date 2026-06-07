from isabelle_mcp.evaluation import check_evaluation_guard, format_evaluation_result
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import CommandSpan, EvaluationView, GoalState
from isabelle_mcp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPLine,
    resolve_caret,
)


async def goal(
    client: IsabelleLSPClient,
    file_path: str,
    line: MCPLine,
    after_text: str | None = None,
) -> GoalState:
    if line < 1:
        raise IsabelleToolError(f"line must be >= 1, got {line}")

    await client.open_document(file_path)

    guard = await check_evaluation_guard(client, file_path, line)
    if isinstance(guard, EvaluationView):
        raise IsabelleToolError(format_evaluation_result(guard, client.project_root))
    note = guard if isinstance(guard, str) else None

    doc = client.open_documents.get(file_path)
    if doc is None:
        raise IsabelleToolError(f"Document not open: {file_path}")
    lines = doc.content.split("\n")
    lsp_line_idx = int(line.to_lsp())
    caret_line, caret_char = resolve_caret(lines, lsp_line_idx, after_text, line)

    command = CommandSpan.from_lsp(
        await client.get_command_at_position(
            file_path, LSPLine(caret_line), LSPCharacter(caret_char),
        )
    )
    subgoals = await client.get_goals_at_position(
        file_path, LSPLine(caret_line), caret_char,
    )
    return GoalState(command=command, subgoals=subgoals, note=note)
