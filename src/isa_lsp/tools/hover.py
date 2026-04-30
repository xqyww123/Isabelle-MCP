"""
Hover information tool implementation.
"""

from typing import Annotated

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import DiagnosticMessage, HoverInfo
from isa_lsp.utils import (
    IsabelleToolError,
    check_pide_response,
    extract_symbol_from_lsp_range,
    get_line_from_file,
    lsp_to_mcp_position,
    mcp_to_lsp_position,
    severity_int_to_string,
    validate_position,
)


async def hover_info(
    client: IsabelleLSPClient,
    file_path: Annotated[str, Field(description="Absolute path to .thy file")],
    line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)],
    column: Annotated[int, Field(description="Column number (1-indexed)", ge=1)],
) -> HoverInfo:
    """Get type signature and documentation for symbol.

    Args:
        client: LSP client instance
        file_path: Absolute path to theory file
        line: Line number (1-indexed)
        column: Column number (1-indexed)

    Returns:
        HoverInfo with symbol, info, and context

    Raises:
        IsabelleToolError: If document not open or LSP error
    """
    validate_position(line, column)

    if file_path not in client.open_documents:
        await client.open_document(file_path)

    lsp_line, lsp_col = mcp_to_lsp_position(line, column)

    # Call LSP
    try:
        response = await client.get_hover(file_path, lsp_line, lsp_col)
        check_pide_response(response, "get_hover", allow_none=True)
    except Exception as exc:
        raise IsabelleToolError(f"Failed to get hover info: {exc}") from exc

    # Parse response
    symbol = ""
    info_text = ""

    if response and isinstance(response, dict):
        # Extract symbol from range
        if "range" in response:
            symbol = extract_symbol_from_lsp_range(file_path, response["range"])

        # Extract hover contents
        contents = response.get("contents", {})
        if isinstance(contents, dict):
            info_text = contents.get("value", "")
        elif isinstance(contents, str):
            info_text = contents
        elif isinstance(contents, list):
            # Array of marked strings
            info_text = "\n".join(
                item.get("value", str(item)) if isinstance(item, dict) else str(item)
                for item in contents
            )

    # Get line context
    line_context = get_line_from_file(file_path, line)

    # Get diagnostics at position (optional enhancement)
    diagnostics_at_position = []
    cached_diags = client.get_cached_diagnostics(file_path)

    for diag in cached_diags:
        diag_range = diag.get("range", {})
        diag_start = diag_range.get("start", {})
        diag_line = diag_start.get("line", -1)

        # Check if diagnostic is on the same line
        if diag_line == lsp_line:
            diag_mcp_line, diag_mcp_col = lsp_to_mcp_position(
                diag_start.get("line", 0),
                diag_start.get("character", 0)
            )
            diag_end = diag_range.get("end", {})
            diag_end_line, diag_end_col = lsp_to_mcp_position(
                diag_end.get("line", 0),
                diag_end.get("character", 0)
            )

            diagnostics_at_position.append(DiagnosticMessage(
                severity=severity_int_to_string(diag.get("severity", 1)),
                message=diag.get("message", ""),
                line=diag_mcp_line,
                column=diag_mcp_col,
                end_line=diag_end_line,
                end_column=diag_end_col,
            ))

    return HoverInfo(
        symbol=symbol,
        info=info_text,
        line_context=line_context,
        diagnostics=diagnostics_at_position,
    )

