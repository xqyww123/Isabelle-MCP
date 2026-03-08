"""
Proof goal state tool implementation - MOST IMPORTANT TOOL.

This tool uses PIDE state panels to query proof goals at a position.
"""

import asyncio
from typing import Annotated, Optional, Dict

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import GoalState
from isa_lsp.utils import (
    IsabelleToolError,
    file_path_to_uri,
    parse_goals_from_html,
    get_line_from_file,
)


# Global state panel manager
_state_panels: Dict[int, asyncio.Future] = {}
_next_panel_id = 1


async def goal(
    client: IsabelleLSPClient,
    file_path: Annotated[str, Field(description="Absolute path to .thy file")],
    line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)],
    column: Annotated[Optional[int], Field(
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

    Note:
        This is a simplified implementation. A full implementation would:
        1. Send PIDE/state_init to create panel
        2. Send PIDE/caret_update to set position
        3. Wait for PIDE/state_output notification
        4. Parse HTML to extract goals
        5. Send PIDE/state_exit to close panel

        For MVP, we use a simplified approach that relies on the LSP client
        handling PIDE notifications in the background.
    """
    global _next_panel_id

    # For MVP: We'll use a simplified approach
    # In a full implementation, we would:
    # - Register a handler for PIDE/state_output in the LSP client
    # - Send state_init, caret_update, wait for output, state_exit

    # Simplified approach: Send caret update and hope for the best
    # This is a limitation of the MVP - proper implementation requires
    # extending lsp_client.py with PIDE state panel support

    # TODO: Implement proper state panel mechanism
    # For now, return a placeholder indicating this needs full implementation

    # Send caret update (this might trigger state updates, but we can't receive them yet)
    try:
        await client.notify("PIDE/caret_update", {
            "uri": uri,
            "line": line,
            "character": character,
        })
    except Exception as e:
        raise IsabelleToolError(f"Failed to update caret: {e}")

    # In MVP, we can't get the actual goals without proper notification handling
    # Return empty list for now
    # NOTE: This tool will not work until we implement proper PIDE state panel support
    # in lsp_client.py

    # Placeholder warning
    import logging
    logger = logging.getLogger(__name__)
    logger.warning(
        "PIDE state panel support not fully implemented in MVP. "
        "Goal queries will not return actual goals. "
        "Full implementation requires extending lsp_client.py with state panel handlers."
    )

    return []  # TODO: Return actual goals when state panel support is added


# ============================================================================
# NOTE: Full implementation of state panel support
# ============================================================================
#
# To properly implement this tool, we need to extend lsp_client.py with:
#
# 1. State panel management:
#    class StatePanelManager:
#        def __init__(self):
#            self.panels: Dict[int, asyncio.Future] = {}
#            self.next_id = 1
#
#        async def create_panel(self, client, uri, line, char):
#            panel_id = self.next_id
#            self.next_id += 1
#            future = asyncio.Future()
#            self.panels[panel_id] = future
#
#            await client.notify("PIDE/state_init", {})
#            await client.notify("PIDE/caret_update", {
#                "uri": uri, "line": line, "character": char
#            })
#
#            html = await asyncio.wait_for(future, timeout=5.0)
#            await client.notify("PIDE/state_exit", {"id": panel_id})
#
#            return parse_goals_from_html(html)
#
#        def handle_state_output(self, panel_id, html):
#            if panel_id in self.panels:
#                self.panels[panel_id].set_result(html)
#
# 2. In lsp_client._handle_notification:
#    elif method == "PIDE/state_output":
#        panel_id = params.get("id")
#        html = params.get("content", "")
#        if hasattr(self, 'state_panel_manager'):
#            self.state_panel_manager.handle_state_output(panel_id, html)
#
# This is left for future enhancement beyond MVP.
