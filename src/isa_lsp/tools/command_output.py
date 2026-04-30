from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import CommandOutputResult, OutputMessage
from isa_lsp.utils import get_line_from_file, parse_command_output_html, validate_position


async def command_output(
    client: IsabelleLSPClient, file_path: str, line: int,
) -> CommandOutputResult:
    validate_position(line, 1)

    if file_path not in client.open_documents:
        await client.open_document(file_path)

    html = await client.get_dynamic_output(file_path, line - 1)
    return CommandOutputResult(
        line_context=get_line_from_file(file_path, line),
        messages=[
            OutputMessage(kind=m.get("kind", "writeln"), message=m.get("text", ""))
            for m in parse_command_output_html(html)
        ],
    )
