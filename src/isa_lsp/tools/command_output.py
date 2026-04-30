"""
Command output tool implementation (PIDE dynamic output).
"""

import logging
from typing import Annotated

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import CommandOutputResult
from isa_lsp.utils import (
    get_line_from_file,
)

logger = logging.getLogger(__name__)


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
        This tool relies on PIDE/dynamic_output notifications which are
        sent when the caret moves. In MVP, we don't have full support for
        capturing these notifications. Full implementation would require
        extending lsp_client.py with a dynamic output cache.
    """
    # Ensure document is open
    if file_path not in client.open_documents:
        await client.open_document(file_path)

    # Get line context
    line_context = get_line_from_file(file_path, line)

    # MVP stub: PIDE/dynamic_output caching not implemented yet
    logger.warning("PIDE dynamic output not implemented; returning empty messages")

    return CommandOutputResult(
        line_context=line_context,
        messages=[],
    )
