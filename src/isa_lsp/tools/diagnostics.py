"""
Diagnostics tool implementation.
"""

from typing import Annotated, Optional

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import DiagnosticsResult, DiagnosticMessage
from isa_lsp.utils import (
    IsabelleToolError,
    lsp_to_mcp_position,
)


async def diagnostic_messages(
    client: IsabelleLSPClient,
    file_path: Annotated[str, Field(description="Absolute path to .thy file")],
    start_line: Annotated[Optional[int], Field(
        description="Filter diagnostics from this line (1-indexed)", ge=1
    )] = None,
    end_line: Annotated[Optional[int], Field(
        description="Filter diagnostics to this line (1-indexed)", ge=1
    )] = None,
    interactive: Annotated[bool, Field(
        description="Returns verbose nested markup with embedded PIDE information. "
                    "Only use when plain text is insufficient."
    )] = False,
) -> DiagnosticsResult:
    """Get compiler diagnostics (errors, warnings, info) for file.

    Args:
        client: LSP client instance
        file_path: Absolute path to theory file
        start_line: Filter from line (1-indexed), optional
        end_line: Filter to line (1-indexed), optional
        interactive: Return verbose PIDE markup (not implemented in MVP)

    Returns:
        DiagnosticsResult with diagnostics and status

    Raises:
        IsabelleToolError: If document not open
    """
    # Ensure document is open
    if file_path not in client.open_documents:
        await client.open_document(file_path)

    # Get cached diagnostics
    cached_diags = client.get_cached_diagnostics(file_path)

    # Filter by line range
    filtered_diags = []

    for diag in cached_diags:
        range_dict = diag.get("range", {})
        start = range_dict.get("start", {})
        diag_line_lsp = start.get("line", 0)

        # Convert to 1-indexed
        diag_line, _ = lsp_to_mcp_position(diag_line_lsp, 0)

        # Apply filters
        if start_line is not None and diag_line < start_line:
            continue
        if end_line is not None and diag_line > end_line:
            continue

        filtered_diags.append(diag)

    # Convert to DiagnosticMessage models
    items = []
    for diag in filtered_diags:
        items.append(_parse_diagnostic(diag))

    # Compute success flag (no errors in range)
    success = all(item.severity != "error" for item in items)

    # Check if processing is complete
    processing_complete = client.is_processing_complete(file_path)

    # Check for failed dependencies (not implemented in MVP - would need to parse special diagnostics)
    failed_dependencies = []

    return DiagnosticsResult(
        success=success,
        items=items,
        processing_complete=processing_complete,
        failed_dependencies=failed_dependencies,
    )


def _parse_diagnostic(diag: dict) -> DiagnosticMessage:
    """Parse LSP Diagnostic to our model.

    Args:
        diag: LSP Diagnostic dictionary

    Returns:
        DiagnosticMessage model
    """
    range_dict = diag.get("range", {})
    start = range_dict.get("start", {})
    end = range_dict.get("end", {})

    # Convert to 1-indexed
    start_line, start_col = lsp_to_mcp_position(
        start.get("line", 0),
        start.get("character", 0)
    )
    end_line, end_col = lsp_to_mcp_position(
        end.get("line", 0),
        end.get("character", 0)
    )

    # Map severity enum to string
    severity_mapping = {
        1: "error",
        2: "warning",
        3: "information",
        4: "hint",
    }
    severity = severity_mapping.get(diag.get("severity", 1), "error")

    return DiagnosticMessage(
        severity=severity,
        message=diag.get("message", ""),
        line=start_line,
        column=start_col,
        end_line=end_line,
        end_column=end_col,
    )
