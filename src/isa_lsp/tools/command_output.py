from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import CommandOutputResult, OutputMessage
from isa_lsp.utils import MCPLine, get_line_from_file, parse_command_output_html, validate_position
from isa_lsp.utils.core import MCPColumn


def _is_non_command_line(line_context: str) -> bool:
    stripped = line_context.strip()
    return not stripped or (stripped.startswith("(*") and stripped.endswith("*)"))


def _candidate_characters(line_context: str) -> list[int]:
    candidates: list[int] = []

    def add(character: int) -> None:
        if character >= 0 and character not in candidates:
            candidates.append(character)

    if line_context.strip():
        first_non_space = len(line_context) - len(line_context.lstrip())
        add(first_non_space)

        token_end = first_non_space
        while token_end < len(line_context) and not line_context[token_end].isspace():
            token_end += 1
        add(token_end)
        if token_end < len(line_context):
            add(token_end + 1)

    add(0)
    return candidates


async def command_output(
    client: IsabelleLSPClient, file_path: str, line: MCPLine,
) -> CommandOutputResult:
    validate_position(line, MCPColumn(1))
    line_context = get_line_from_file(file_path, line)

    if _is_non_command_line(line_context):
        return CommandOutputResult(line_context=line_context)

    await client.open_document(file_path)
    await client.set_caret(file_path, line.to_lsp())

    messages: list[OutputMessage] = []
    for character in _candidate_characters(line_context):
        html = await client.get_dynamic_output(file_path, line.to_lsp(), character)
        messages = [
            OutputMessage(kind=m.get("kind", "writeln"), message=m.get("text", ""))
            for m in parse_command_output_html(html)
        ]
        if messages:
            break

    return CommandOutputResult(
        line_context=line_context,
        messages=messages,
    )
