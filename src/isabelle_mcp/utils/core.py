"""Position conversion, URI handling, and error types."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, unquote

from fastmcp.exceptions import FastMCPError, ToolError


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


# One sentence for every catastrophe, no cause: the cause goes to the server log.
CATASTROPHE_MESSAGE = (
    "The Isabelle session hit an internal failure and has been terminated; "
    "call isabelle_launch to start a new one. Details are in the server log."
)


class IsabelleCatastrophe(FastMCPError):
    """The system-wide catastrophe: the Isabelle session must be terminated and
    the agent must launch again.

    Raise it from anywhere -- a cancellation that ran out of its budget, a prover
    that stopped answering, an invariant found broken. Exactly one handler exists,
    at the tool boundary (server.py): it logs the reason, tears the prover down
    through IsabelleLSPClient.teardown, and answers with CATASTROPHE_MESSAGE.

    A FastMCPError, and this is load-bearing: FastMCP runs the tool body under
    ``except Exception: raise ToolError(...)`` BEFORE any middleware sees the
    result, and lets only FastMCPError through. A plain Exception here would be
    masked into a generic tool error and the handler would never run (measured).
    FastMCPError rather than ToolError so the reason is not delivered to the
    agent if the handler were ever missing.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def plural(n: int, noun: str) -> str:
    """``1 command`` / ``2 commands``.

    Agent-facing text never writes ``command(s)``: the count is known at the
    moment the message is built, so the parenthesis only makes the reader do
    work the server could have done.
    """
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


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
