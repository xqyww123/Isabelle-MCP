from isabelle_mcp import query
from isabelle_mcp.evaluation import (
    check_evaluation_guard,
    format_evaluation_result,
    relativize,
)
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import CommandSpan, EvaluationView, GoalState
from isabelle_mcp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPLine,
    parse_goals_from_html,
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

    # NOT opened here: the guard decides whether opening is allowed. A didOpen
    # globally invalidates decoration freshness, so it must not happen while an
    # evaluation is outstanding; on the paths that may open, evaluate_to does it.
    guard = await check_evaluation_guard(client, file_path, line)
    if isinstance(guard, EvaluationView):
        raise IsabelleToolError(format_evaluation_result(guard, client.project_root))
    notes = [guard] if isinstance(guard, str) else []

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

    # Read the command's state out of the prover's document state. No caret
    # movement, so this is safe while an evaluation is running, and "there is no
    # proof state here" comes back as an answer instead of as a silence waited
    # out by a timeout.
    reply = await client.get_proof_state_at_position(
        file_path, LSPLine(caret_line), caret_char,
    )
    where = f"{relativize(file_path, client.project_root)}:{int(line)}"

    if reply.status == query.NO_COMMAND:
        return GoalState(command=None, subgoals=[], note=_note(notes))
    if reply.status == query.NO_PROOF_STATE:
        notes.append(query.PROOF_STATE_MESSAGES[query.NO_PROOF_STATE].format(where=where))
        return GoalState(command=command, subgoals=[], note=_note(notes))
    if reply.status != query.OK:
        raise IsabelleToolError(
            query.message(
                reply, where, client.QUERY_BACKSTOP, query.PROOF_STATE_MESSAGES,
            )
        )

    notes.extend(query.notes(reply, where))
    return GoalState(
        command=command,
        subgoals=parse_goals_from_html(reply.content),
        note=_note(notes),
    )


def _note(notes: list[str]) -> str | None:
    return " ".join(notes) if notes else None
