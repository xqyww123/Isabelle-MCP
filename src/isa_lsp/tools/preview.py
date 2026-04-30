"""
Preview tool implementation for document exports.

This tool uses PIDE preview requests to generate HTML previews of theory content.
"""

import logging
from typing import Annotated

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import PreviewResult
from isa_lsp.utils import (
    IsabelleToolError,
    file_path_to_uri,
    get_line_from_file,
)

logger = logging.getLogger(__name__)


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
        notifications. In MVP, we don't have full support for receiving these
        notifications. Full implementation would require extending lsp_client.py
        with a preview response handler.
    """
    # Ensure document is open
    if file_path not in client.open_documents:
        await client.open_document(file_path)

    # Get line context if provided
    line_context = None
    if line is not None:
        line_context = get_line_from_file(file_path, line)

    # Get URI
    uri = file_path_to_uri(file_path)

    # MVP stub: send preview request but cannot receive PIDE/preview_response yet
    logger.warning("PIDE preview not implemented; returning empty HTML")

    try:
        await client.notify("PIDE/preview_request", {"uri": uri})
    except Exception as e:
        raise IsabelleToolError(f"Failed to send preview request: {e}")

    return PreviewResult(
        html="",
        line_context=line_context,
    )
