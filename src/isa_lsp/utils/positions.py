"""
Position conversion utilities.

MCP tools use 1-indexed positions (line 1, column 1 = first character)
LSP uses 0-indexed positions (line 0, character 0 = first character)
"""

from typing import Tuple


def mcp_to_lsp_position(line: int, column: int) -> Tuple[int, int]:
    """Convert MCP position (1-indexed) to LSP position (0-indexed).

    Args:
        line: Line number (1-indexed)
        column: Column number (1-indexed)

    Returns:
        Tuple of (line, character) in 0-indexed LSP format

    Examples:
        >>> mcp_to_lsp_position(1, 1)
        (0, 0)

        >>> mcp_to_lsp_position(42, 15)
        (41, 14)
    """
    return (line - 1, column - 1)


def lsp_to_mcp_position(line: int, character: int) -> Tuple[int, int]:
    """Convert LSP position (0-indexed) to MCP position (1-indexed).

    Args:
        line: Line number (0-indexed)
        character: Character number (0-indexed)

    Returns:
        Tuple of (line, column) in 1-indexed MCP format

    Examples:
        >>> lsp_to_mcp_position(0, 0)
        (1, 1)

        >>> lsp_to_mcp_position(41, 14)
        (42, 15)
    """
    return (line + 1, character + 1)
