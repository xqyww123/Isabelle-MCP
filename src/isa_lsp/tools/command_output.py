"""
Command output tool implementation (PIDE dynamic output).
"""

from typing import Annotated

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import CommandOutputResult, OutputMessage
from isa_lsp.utils import (
    get_line_from_file,
    parse_command_output_html,
)


async def command_output(
    client: IsabelleLSPClient,
    file_path: Annotated[str, Field(description="Absolute path to .thy file")],
    line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)],
) -> CommandOutputResult:
    """Get prover output messages for command.

    Args:
        client: LSP client instance
        file_path: Absolute path to theory file
        line: Line number (1-indexed)

    Returns:
        CommandOutputResult with messages

    Raises:
        IsabelleToolError: If document not open

    Note:
        This tool relies on PIDE/dynamic_output notifications, which are
        triggered by caret movement and cached by IsabelleLSPClient.
    """
    # Ensure document is open
    if file_path not in client.open_documents:
        await client.open_document(file_path)

    # Get line context
    line_context = get_line_from_file(file_path, line)

    html = await client.get_dynamic_output(file_path, line - 1)
    parsed_messages = parse_command_output_html(html)

    return CommandOutputResult(
        line_context=line_context,
        messages=[
            OutputMessage(
                kind=str(item.get("kind", "writeln")),
                message=str(item.get("text", "")),
            )
            for item in parsed_messages
        ],
    )
