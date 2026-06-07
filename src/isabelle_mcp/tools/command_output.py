import io

from isabelle_mcp.evaluation import check_evaluation_guard, format_evaluation_result
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import (
    CommandOutputResult,
    CommandSpan,
    EvaluationView,
    OutputMessage,
)
from isabelle_mcp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPLine,
    parse_command_output_html,
    resolve_caret,
)


async def command_output(
    client: IsabelleLSPClient,
    file_path: str,
    line: MCPLine,
    after_text: str | None = None,
) -> CommandOutputResult:
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

    # One position-explicit request returns the enclosing command's source+range
    # AND its rendered output, in a single shot. It renders the whole command's
    # results (independent of the offset within the command) and does not move the
    # caret, so it is immune to the dynamic_output "same caret -> no push" hang.
    result = await client.get_output_at_position(
        file_path, LSPLine(caret_line), LSPCharacter(caret_char),
    )
    if result is None:
        # No command at the position (blank line, comment, or past the last command).
        return CommandOutputResult(command=None, messages=[], note=note)

    source, rng, content = result
    command = CommandSpan.from_lsp((source, rng))
    messages = [
        OutputMessage(kind=m.get("kind", "normal"), message=m.get("text", ""))
        for m in parse_command_output_html(content)
    ]
    return CommandOutputResult(command=command, messages=messages, note=note)


def format_command_output(result: CommandOutputResult, line: int) -> str:
    """Render a CommandOutputResult as the agent-facing plain-text block."""
    buf = io.StringIO()
    first = True

    def section(text: str) -> None:
        nonlocal first
        if not first:
            buf.write("\n\n")
        buf.write(text)
        first = False

    if result.note:
        section(f"[note] {result.note}")

    if result.command is None:
        section(f"No command at line {line}.")
        return buf.getvalue()

    cmd = result.command
    loc = (
        f"[line {cmd.start_line}]"
        if cmd.start_line == cmd.end_line
        else f"[line {cmd.start_line}-{cmd.end_line}]"
    )
    if result.messages:
        body = "\n".join(f"[{m.kind}] {m.message}" for m in result.messages)
    else:
        body = "(no output)"
    section(f"{loc}\n{cmd.text}\n\n{body}")
    return buf.getvalue()
