"""
Command output tool implementation (PIDE dynamic output).
"""

from typing import Annotated

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import CommandOutputResult, OutputMessage
from isa_lsp.utils import (
    IsabelleToolError,
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

    # In MVP: We don't have dynamic output caching yet
    # TODO: Implement dynamic output cache in lsp_client.py
    #
    # Full implementation would:
    # 1. Cache PIDE/dynamic_output notifications by file/line
    # 2. Return cached messages for the requested line
    #
    # For now, return empty messages with a warning

    import logging
    logger = logging.getLogger(__name__)
    logger.warning(
        "PIDE dynamic output support not fully implemented in MVP. "
        "Command output queries will not return actual messages. "
        "Full implementation requires caching PIDE/dynamic_output notifications."
    )

    messages = []

    return CommandOutputResult(
        line_context=line_context,
        messages=messages,
    )


# ============================================================================
# NOTE: Full implementation of dynamic output support
# ============================================================================
#
# To properly implement this tool, we need to extend lsp_client.py with:
#
# 1. Dynamic output cache:
#    @dataclass
#    class DynamicOutputCache:
#        output_by_position: Dict[Tuple[str, int], List[OutputMessage]] = field(default_factory=dict)
#
#        def cache_output(self, file_path: str, line: int, html: str):
#            messages = parse_command_output_html(html)
#            self.output_by_position[(file_path, line)] = messages
#
#        def get_output(self, file_path: str, line: int) -> List[OutputMessage]:
#            return self.output_by_position.get((file_path, line), [])
#
# 2. In lsp_client.__init__:
#    self.dynamic_output_cache = DynamicOutputCache()
#
# 3. In lsp_client._handle_notification:
#    elif method == "PIDE/dynamic_output":
#        html = params.get("content", "")
#        # Need to track current caret position to know which file/line this applies to
#        if hasattr(self, 'current_caret_position'):
#            file_path, line = self.current_caret_position
#            self.dynamic_output_cache.cache_output(file_path, line, html)
#
# This is left for future enhancement beyond MVP.
