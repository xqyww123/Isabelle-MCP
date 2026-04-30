"""
Document highlights tool implementation.
"""

from typing import Annotated

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import Highlight, HighlightsResult
from isa_lsp.utils import (
    IsabelleToolError,
    check_pide_response,
    lsp_to_mcp_position,
    mcp_to_lsp_position,
    validate_position,
)


async def document_highlights(
    client: IsabelleLSPClient,
    file_path: Annotated[str, Field(description="Absolute path to .thy file")],
    line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)],
    column: Annotated[int, Field(description="Column number (1-indexed)", ge=1)],
) -> HighlightsResult:
    """Find all occurrences of symbol in document.

    Args:
        client: LSP client instance
        file_path: Absolute path to theory file
        line: Line number (1-indexed)
        column: Column number (1-indexed)

    Returns:
        HighlightsResult with symbol and highlight locations

    Raises:
        IsabelleToolError: If document not open or LSP error
    """
    validate_position(line, column)

    if file_path not in client.open_documents:
        await client.open_document(file_path)

    lsp_line, lsp_col = mcp_to_lsp_position(line, column)

    # Call LSP
    try:
        response = await client.get_highlights(file_path, lsp_line, lsp_col)
        check_pide_response(response, "get_highlights", allow_none=True)
    except Exception as e:
        raise IsabelleToolError(f"Failed to get highlights: {e}")

    # Extract symbol at query position
    from isa_lsp.tools.definition import _extract_symbol_at_position
    symbol = _extract_symbol_at_position(file_path, line, column)

    # Parse response
    highlights = []

    if response and isinstance(response, list):
        for highlight in response:
            if isinstance(highlight, dict):
                parsed = _parse_highlight(highlight)
                if parsed:
                    highlights.append(parsed)

    return HighlightsResult(
        symbol=symbol,
        highlights=highlights,
    )


def _parse_highlight(highlight: dict) -> Highlight:
    """Parse LSP DocumentHighlight to our model.

    Args:
        highlight: LSP DocumentHighlight dictionary

    Returns:
        Highlight model or None if invalid
    """
    try:
        # Validate that range exists
        if "range" not in highlight:
            return None

        range_dict = highlight.get("range", {})
        start = range_dict.get("start", {})
        end = range_dict.get("end", {})

        # Validate that start and end exist
        if not start or not end:
            return None

        # Convert to 1-indexed
        start_line, start_col = lsp_to_mcp_position(
            start.get("line", 0),
            start.get("character", 0)
        )
        end_line, end_col = lsp_to_mcp_position(
            end.get("line", 0),
            end.get("character", 0)
        )

        # Map kind enum to string
        kind_mapping = {
            1: "text",
            2: "read",
            3: "write",
        }
        kind = kind_mapping.get(highlight.get("kind", 1), "text")

        return Highlight(
            line=start_line,
            start_column=start_col,
            end_column=end_col,
            kind=kind,
        )

    except Exception:
        return None
