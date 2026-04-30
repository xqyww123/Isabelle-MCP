"""
Go to definition tool implementation.
"""

import re
from typing import Annotated

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import DeclarationLocation, Location
from isa_lsp.utils import (
    IsabelleToolError,
    check_pide_response,
    lsp_to_mcp_position,
    mcp_to_lsp_position,
    uri_to_file_path,
    validate_position,
)


async def declaration_location(
    client: IsabelleLSPClient,
    file_path: Annotated[str, Field(description="Absolute path to .thy file")],
    line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)],
    column: Annotated[int, Field(description="Column number (1-indexed)", ge=1)],
) -> DeclarationLocation:
    """Find where a symbol is defined.

    Args:
        client: LSP client instance
        file_path: Absolute path to theory file
        line: Line number (1-indexed)
        column: Column number (1-indexed)

    Returns:
        DeclarationLocation with symbol and definition locations

    Raises:
        IsabelleToolError: If document not open or LSP error
    """
    validate_position(line, column)

    if file_path not in client.open_documents:
        await client.open_document(file_path)

    lsp_line, lsp_col = mcp_to_lsp_position(line, column)

    try:
        response = await client.get_definition(file_path, lsp_line, lsp_col)
        check_pide_response(response, "get_definition", allow_none=True)
    except Exception as e:
        raise IsabelleToolError(f"Failed to get definition: {e}")

    # Extract symbol at query position
    symbol = _extract_symbol_at_position(file_path, line, column)

    # Parse response - LSP can return Location, Location[], or LocationLink[]
    locations = []

    if response is not None:
        # Normalize to list
        if isinstance(response, list):
            location_list = response
        else:
            location_list = [response]

        # Parse each location
        for loc in location_list:
            if isinstance(loc, dict):
                parsed_loc = _parse_location(loc)
                if parsed_loc:
                    locations.append(parsed_loc)

    return DeclarationLocation(
        symbol=symbol,
        locations=locations,
    )


def _parse_location(loc: dict) -> Location:
    """Parse LSP Location or LocationLink to our model.

    Args:
        loc: LSP Location or LocationLink dictionary

    Returns:
        Location model or None if invalid
    """
    try:
        # Handle LocationLink (has targetUri and targetRange)
        if "targetUri" in loc:
            uri = loc["targetUri"]
            range_dict = loc.get("targetRange", loc.get("targetSelectionRange", {}))
        # Handle Location (has uri and range)
        elif "uri" in loc:
            uri = loc["uri"]
            range_dict = loc.get("range", {})
        else:
            return None

        # Convert URI to file path
        file_path = uri_to_file_path(uri)

        # Extract position (use start of range)
        start = range_dict.get("start", {})
        lsp_line = start.get("line", 0)
        lsp_char = start.get("character", 0)

        # Convert to 1-indexed
        mcp_line, mcp_col = lsp_to_mcp_position(lsp_line, lsp_char)

        return Location(
            file_path=file_path,
            line=mcp_line,
            column=mcp_col,
        )

    except Exception:
        return None


def _extract_symbol_at_position(file_path: str, line: int, column: int) -> str:
    """Extract symbol text at position (simple word extraction).

    Args:
        file_path: Absolute path to file
        line: Line number (1-indexed)
        column: Column number (1-indexed)

    Returns:
        Symbol text
    """
    try:
        with open(file_path, encoding='utf-8') as f:
            lines = f.readlines()

        if line < 1 or line > len(lines):
            return ""

        line_content = lines[line - 1]

        # Find word boundaries
        # Isabelle identifiers can include: letters, digits, _, ., '
        pattern = r"[a-zA-Z0-9_.']+|[⟹⟶∧∨¬∀∃]"

        # Find all matches
        for match in re.finditer(pattern, line_content):
            start, end = match.span()
            # Check if column is within this match (convert to 0-indexed)
            if start < column <= end:
                return match.group()

        return ""

    except Exception:
        return ""
