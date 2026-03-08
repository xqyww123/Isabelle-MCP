"""
Error handling utilities for Isa-LSP.

Following lean-lsp-mcp patterns for structured error handling.
"""

from typing import Any


class IsabelleToolError(Exception):
    """Exception raised when an Isabelle MCP tool operation fails.

    This is the main exception type for all tool errors. It should be caught
    by the MCP server and returned as an error response to the client.

    Examples:
        >>> raise IsabelleToolError("Session not initialized")
        >>> raise IsabelleToolError("PIDE timeout during get_hover")
    """
    pass


def check_pide_response(
    response: Any,
    operation: str,
    *,
    allow_none: bool = False
) -> Any:
    """Check a PIDE/LSP response for error patterns and raise if found.

    Args:
        response: The response from LSP/PIDE server
        operation: Description of the operation (e.g., "get_hover")
        allow_none: Whether None is a valid response

    Returns:
        The response if valid

    Raises:
        IsabelleToolError: If response indicates an error or timeout

    Examples:
        >>> check_pide_response({"result": "ok"}, "test")
        {'result': 'ok'}

        >>> check_pide_response(None, "test", allow_none=True)
        None

        >>> check_pide_response(None, "test", allow_none=False)
        Traceback (most recent call last):
        ...
        IsabelleToolError: PIDE timeout during test

        >>> check_pide_response({"error": {"message": "Failed"}}, "test")
        Traceback (most recent call last):
        ...
        IsabelleToolError: PIDE error during test: Failed (code -1)
    """
    # Check for timeout (None response)
    if response is None and not allow_none:
        raise IsabelleToolError(f"PIDE timeout during {operation}")

    # Check for LSP error response
    if isinstance(response, dict) and "error" in response:
        error_data = response["error"]
        error_msg = error_data.get("message", "Unknown error")
        error_code = error_data.get("code", -1)
        raise IsabelleToolError(
            f"PIDE error during {operation}: {error_msg} (code {error_code})"
        )

    return response
