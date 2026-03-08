"""
Preview tool implementation for document exports.

This tool uses PIDE preview requests to generate HTML previews of theory content.
"""

import asyncio
from typing import Annotated, Optional, Dict

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import PreviewResult
from isa_lsp.utils import (
    IsabelleToolError,
    file_path_to_uri,
    get_line_from_file,
)


# Global preview request manager
_preview_requests: Dict[int, asyncio.Future] = {}
_next_preview_id = 1


async def preview_document(
    client: IsabelleLSPClient,
    file_path: Annotated[str, Field(description="Absolute path to .thy file")],
    line: Annotated[Optional[int], Field(
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

    # In MVP: We don't have preview notification handling yet
    # TODO: Implement preview request/response mechanism
    #
    # Full implementation would:
    # 1. Send PIDE/preview_request with uri and optional snapshot_id
    # 2. Wait for PIDE/preview_response notification
    # 3. Return the HTML content
    #
    # For now, return empty HTML with a warning

    import logging
    logger = logging.getLogger(__name__)
    logger.warning(
        "PIDE preview support not fully implemented in MVP. "
        "Preview queries will not return actual HTML. "
        "Full implementation requires extending lsp_client.py with preview handlers."
    )

    # Try sending preview request (even though we can't receive response in MVP)
    try:
        await client.notify("PIDE/preview_request", {
            "uri": uri,
        })
    except Exception as e:
        raise IsabelleToolError(f"Failed to send preview request: {e}")

    # MVP limitation: return empty HTML
    return PreviewResult(
        html="",
        line_context=line_context,
    )


# ============================================================================
# NOTE: Full implementation of preview support
# ============================================================================
#
# To properly implement this tool, we need to extend lsp_client.py with:
#
# 1. Preview request manager:
#    class PreviewManager:
#        def __init__(self):
#            self.requests: Dict[int, asyncio.Future] = {}
#            self.next_id = 1
#
#        async def request_preview(self, client, uri):
#            preview_id = self.next_id
#            self.next_id += 1
#            future = asyncio.Future()
#            self.requests[preview_id] = future
#
#            await client.notify("PIDE/preview_request", {
#                "uri": uri,
#                "id": preview_id  # If PIDE supports request IDs
#            })
#
#            html = await asyncio.wait_for(future, timeout=10.0)
#            return html
#
#        def handle_preview_response(self, preview_id, html):
#            if preview_id in self.requests:
#                self.requests[preview_id].set_result(html)
#
# 2. In lsp_client._handle_notification:
#    elif method == "PIDE/preview_response":
#        preview_id = params.get("id")  # If PIDE includes ID
#        html = params.get("content", "")
#        if hasattr(self, 'preview_manager'):
#            self.preview_manager.handle_preview_response(preview_id, html)
#
# NOTE: The actual PIDE/preview_request and PIDE/preview_response message
# format needs to be verified from Isabelle VSCode extension source code.
# The preview mechanism may not include request IDs, in which case we would
# need a different approach (e.g., assume the next preview_response is for
# the most recent preview_request).
#
# This is left for future enhancement beyond MVP.
