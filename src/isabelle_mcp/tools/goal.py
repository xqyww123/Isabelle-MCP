from isabelle_mcp.evaluation import check_evaluation_guard
from isabelle_mcp.lsp_client import IsabelleLSPClient, JsonDict
from isabelle_mcp.models import CommandSpan, EvaluationResult, GoalState
from isabelle_mcp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPLine,
    find_after_text_caret,
)


def _to_command_span(result: tuple[str, JsonDict] | None) -> CommandSpan | None:
    if result is None:
        return None
    source, rng = result
    start, end = rng.get("start", {}), rng.get("end", {})
    return CommandSpan(
        text=source,
        start_line=int(start.get("line", 0)) + 1,
        start_column=int(start.get("character", 0)) + 1,
        end_line=int(end.get("line", 0)) + 1,
        end_column=int(end.get("character", 0)) + 1,
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
    if isinstance(guard, EvaluationResult):
        raise IsabelleToolError(guard.message)
    note = guard if isinstance(guard, str) else None

    doc = client.open_documents.get(file_path)
    if doc is None:
        raise IsabelleToolError(f"Document not open: {file_path}")
    lines = doc.content.split("\n")
    lsp_line_idx = int(line.to_lsp())
    if lsp_line_idx >= len(lines):
        raise IsabelleToolError(f"line {line} is beyond the end of the file")
    line_text = lines[lsp_line_idx]

    # Resolve the caret position whose enclosing command we report.
    if after_text is None:
        # The command the line ends in: anchor on the last non-blank character so
        # the position falls INSIDE the command. The exact end of the line sits on
        # the command boundary and would resolve to the following command.
        stripped = len(line_text.rstrip())
        caret_line = lsp_line_idx
        caret_char = stripped - 1 if stripped > 0 else 0
    else:
        caret = find_after_text_caret(lines, lsp_line_idx, after_text)
        if caret is None:
            raise IsabelleToolError(f"Text '{after_text}' not found on line {line}")
        caret_line, caret_char = caret

    command = _to_command_span(
        await client.get_command_at_position(
            file_path, LSPLine(caret_line), LSPCharacter(caret_char),
        )
    )
    subgoals = await client.get_goals_at_position(
        file_path, LSPLine(caret_line), caret_char,
    )
    return GoalState(command=command, subgoals=subgoals, note=note)
