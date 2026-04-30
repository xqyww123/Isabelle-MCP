"""
Proof goal state tool implementation - MOST IMPORTANT TOOL.

This tool uses PIDE state panels to query proof goals at a position.
"""

import logging
from typing import Annotated

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import GoalState
from isa_lsp.utils import (
    IsabelleToolError,
    file_path_to_uri,
    get_line_from_file,
)

logger = logging.getLogger(__name__)


async def goal(
    client: IsabelleLSPClient,
    file_path: Annotated[str, Field(description="Absolute path to .thy file")],
    line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)],
    column: Annotated[int | None, Field(
        description="Column number (1-indexed). Omit to see before/after tactic transformation.",
        ge=1
    )] = None,
) -> GoalState:
    """Get proof goals at position. **MOST IMPORTANT tool - use often!**

    Omitting column shows how a tactic transforms the proof state:
    - goals_before: State at line start
    - goals_after: State at line end

    Args:
        client: LSP client instance
        file_path: Absolute path to theory file
        line: Line number (1-indexed)
        column: Column number (1-indexed), optional

    Returns:
        GoalState with goals and context

    Raises:
        IsabelleToolError: If document not open or PIDE error
    """
    # Ensure document is open
    if file_path not in client.open_documents:
        await client.open_document(file_path)

    # Get line context
    line_context = get_line_from_file(file_path, line)

    # Get URI
    uri = file_path_to_uri(file_path)

    if column is None:
        # Get before/after by querying line start and line end
        goals_before = await _query_goals_at_position(
            client, uri, line - 1, 0  # Line start (0-indexed)
        )

        # Get line length for end position
        line_length = len(line_context)
        goals_after = await _query_goals_at_position(
            client, uri, line - 1, line_length  # Line end (0-indexed)
        )

        return GoalState(
            line_context=line_context,
            goals=None,
            goals_before=goals_before,
            goals_after=goals_after,
            context=None,  # TODO: Extract context from HTML if available
        )
    else:
        # Get goals at specific column
        goals = await _query_goals_at_position(
            client, uri, line - 1, column - 1  # 0-indexed
        )

        return GoalState(
            line_context=line_context,
            goals=goals,
            goals_before=None,
            goals_after=None,
            context=None,  # TODO: Extract context from HTML if available
        )


async def _query_goals_at_position(
    client: IsabelleLSPClient,
    uri: str,
    line: int,
    character: int,
    timeout: float = 5.0
) -> list[str]:
    """Query proof goals at specific position using PIDE state panel.

    Args:
        client: LSP client instance
        uri: File URI
        line: Line number (0-indexed for LSP)
        character: Character position (0-indexed for LSP)
        timeout: Timeout in seconds

    Returns:
        List of goal strings

    Raises:
        IsabelleToolError: On timeout or PIDE error
    """
    # MVP stub: send caret update but cannot receive PIDE/state_output yet
    try:
        await client.notify("PIDE/caret_update", {
            "uri": uri,
            "line": line,
            "character": character,
        })
    except Exception as e:
        raise IsabelleToolError(f"Failed to update caret: {e}")

    logger.warning("PIDE state panel not implemented; goal query returns empty list")
    return []
