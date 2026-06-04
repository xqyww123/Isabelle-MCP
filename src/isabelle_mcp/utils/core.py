"""Position conversion, URI handling, and error types."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, unquote

from fastmcp.exceptions import ToolError


# MCP positions are 1-indexed; LSP positions are 0-indexed.
# Subclassing int gives: Pyright type safety, zero-cost comparisons,
# and OOP conversion methods.


class MCPLine(int):
    """1-indexed line number (MCP convention)."""

    def to_lsp(self) -> LSPLine:
        return LSPLine(self - 1)


class MCPColumn(int):
    """1-indexed column number (MCP convention)."""

    def to_lsp(self) -> LSPCharacter:
        return LSPCharacter(self - 1)


class LSPLine(int):
    """0-indexed line number (LSP convention)."""

    def to_mcp(self) -> MCPLine:
        return MCPLine(self + 1)


class LSPCharacter(int):
    """0-indexed character offset (LSP convention)."""

    def to_mcp(self) -> MCPColumn:
        return MCPColumn(self + 1)


class IsabelleToolError(ToolError):
    """An expected, actionable error meant for the calling agent.

    Inherits :class:`fastmcp.exceptions.ToolError` so its message is always
    delivered to the LLM (unaffected by ``mask_error_details``) and is kept
    semantically distinct from unexpected internal bugs.
    """


def check_pide_response(response: object, operation: str, *, allow_none: bool = False) -> object:
    if response is None and not allow_none:
        raise IsabelleToolError(f"PIDE timeout during {operation}")

    if isinstance(response, dict) and "error" in response:
        error_data = response["error"]
        msg = error_data.get("message", "Unknown error")
        code = error_data.get("code", -1)
        raise IsabelleToolError(f"PIDE error during {operation}: {msg} (code {code})")

    return response


def validate_position(line: MCPLine, column: MCPColumn) -> None:
    if line < 1:
        raise IsabelleToolError(f"line must be >= 1, got {line}")
    if column < 1:
        raise IsabelleToolError(f"column must be >= 1, got {column}")


def mcp_to_lsp_position(
    line: MCPLine, column: MCPColumn,
) -> tuple[LSPLine, LSPCharacter]:
    return line.to_lsp(), column.to_lsp()


def lsp_to_mcp_position(
    line: LSPLine, character: LSPCharacter,
) -> tuple[MCPLine, MCPColumn]:
    return line.to_mcp(), character.to_mcp()


def file_path_to_uri(file_path: str) -> str:
    path = Path(file_path).resolve()
    path_str = str(path).replace("\\", "/")
    encoded_path = quote(path_str, safe="/:")
    return f"file://{encoded_path}"


def uri_to_file_path(uri: str) -> str:
    if not uri.startswith("file://"):
        raise ValueError(f"Invalid file URI: {uri}")
    return unquote(uri[7:])
