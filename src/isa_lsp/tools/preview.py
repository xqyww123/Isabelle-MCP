"""
Preview tool implementation for document exports.

This tool uses PIDE preview requests to generate HTML previews of theory content.
"""

from typing import Annotated

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import PreviewResult
from isa_lsp.utils import (
    get_line_from_file,
)


async def preview_document(
    client: IsabelleLSPClient,
    file_path: Annotated[str, Field(description="Absolute path to .thy file")],
    line: Annotated[int | None, Field(
        description="Line number (1-indexed) for context. If omitted, previews entire document.",
        ge=1
    )] = None,
) -> PreviewResult:
    """Generate HTML preview of theory content.

    Args:
        client: LSP client instance
        file_path: Absolute path to theory file
        line: Line number for context (1-indexed), optional

    Returns:
        PreviewResult with HTML content

    Raises:
        IsabelleToolError: If document not open or PIDE error

    Note:
        This tool relies on PIDE/preview_request and PIDE/preview_response
        notifications, which are matched by IsabelleLSPClient.
    """
    # Ensure document is open
    if file_path not in client.open_documents:
        await client.open_document(file_path)

    # Get line context if provided
    line_context = None
    if line is not None:
        line_context = get_line_from_file(file_path, line)

    response = await client.request_preview(file_path)
    html = str(response.get("content", ""))

    return PreviewResult(
        html=html,
        line_context=line_context,
    )
