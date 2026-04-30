"""Position conversion, URI handling, and error types."""

from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote


class IsabelleToolError(Exception):
    pass


def check_pide_response(response: Any, operation: str, *, allow_none: bool = False) -> Any:
    if response is None and not allow_none:
        raise IsabelleToolError(f"PIDE timeout during {operation}")

    if isinstance(response, dict) and "error" in response:
        error_data = response["error"]
        msg = error_data.get("message", "Unknown error")
        code = error_data.get("code", -1)
        raise IsabelleToolError(f"PIDE error during {operation}: {msg} (code {code})")

    return response


def validate_position(line: int, column: int) -> None:
    if line < 1:
        raise IsabelleToolError(f"line must be >= 1, got {line}")
    if column < 1:
        raise IsabelleToolError(f"column must be >= 1, got {column}")


# MCP uses 1-indexed positions, LSP uses 0-indexed.

def mcp_to_lsp_position(line: int, column: int) -> tuple[int, int]:
    return (line - 1, column - 1)


def lsp_to_mcp_position(line: int, character: int) -> tuple[int, int]:
    return (line + 1, character + 1)


def file_path_to_uri(file_path: str) -> str:
    path = Path(file_path).resolve()
    path_str = str(path).replace("\\", "/")
    encoded_path = quote(path_str, safe="/:")
    return f"file://{encoded_path}"


def uri_to_file_path(uri: str) -> str:
    if not uri.startswith("file://"):
        raise ValueError(f"Invalid file URI: {uri}")
    return unquote(uri[7:])
