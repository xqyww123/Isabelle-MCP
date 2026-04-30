"""
Proof goal state tool implementation - MOST IMPORTANT TOOL.

This tool uses PIDE state panels to query proof goals at a position.
"""

from typing import Annotated

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import GoalState
from isa_lsp.utils import get_line_from_file


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

    if column is None:
        # Get before/after by querying line start and line end
        goals_before = await client.get_goals_at_position(
            file_path, line - 1, 0  # Line start (0-indexed)
        )

        # Get line length for end position
        line_length = len(line_context)
        goals_after = await client.get_goals_at_position(
            file_path, line - 1, line_length  # Line end (0-indexed)
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
        goals = await client.get_goals_at_position(
            file_path, line - 1, column - 1  # 0-indexed
        )

        return GoalState(
            line_context=line_context,
            goals=goals,
            goals_before=None,
            goals_after=None,
            context=None,  # TODO: Extract context from HTML if available
        )
