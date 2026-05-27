from isa_lsp.evaluation import check_evaluation_guard
from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import DiagnosticMessage, DiagnosticsResult, EvaluationResult
from isa_lsp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPLine,
    lsp_to_mcp_position,
    severity_int_to_string,
)


def _resolve_line(value: int, total_lines: int) -> MCPLine:
    """Resolve negative line indices: -1 = last line, -i = last i-th line."""
    if value < 0:
        return MCPLine(max(1, total_lines + 1 + value))
    return MCPLine(value)


async def diagnostic_messages(
    client: IsabelleLSPClient,
    file_path: str,
    start_line: int,
    end_line: int,
) -> DiagnosticsResult:
    await client.open_document(file_path)

    doc = client.open_documents.get(file_path)
    total_lines = (doc.content.count("\n") + 1) if doc else 1

    mcp_start = _resolve_line(start_line, total_lines)
    mcp_end = _resolve_line(end_line, total_lines)

    if mcp_start < 1:
        raise IsabelleToolError(f"start_line must be >= 1, got {mcp_start}")
    if mcp_end < 1:
        raise IsabelleToolError(f"end_line must be >= 1, got {mcp_end}")
    if mcp_start > mcp_end:
        raise IsabelleToolError(
            f"start_line must be <= end_line, got {mcp_start} > {mcp_end}"
        )

    guard = await check_evaluation_guard(client, file_path, mcp_end)
    if isinstance(guard, EvaluationResult):
        raise IsabelleToolError(guard.message)
    note = guard if isinstance(guard, str) else None

    items: list[DiagnosticMessage] = []
    for diag in client.get_cached_diagnostics(file_path):
        diag_lsp_line = diag.get("range", {}).get("start", {}).get("line", 0)
        diag_mcp_line = LSPLine(diag_lsp_line).to_mcp()
        if diag_mcp_line < mcp_start:
            continue
        if diag_mcp_line > mcp_end:
            continue
        items.append(_parse_diagnostic(diag))

    processing_complete = client.file_all_processed(file_path)

    return DiagnosticsResult(
        success=processing_complete and all(it.severity != "error" for it in items),
        items=items,
        processing_complete=processing_complete,
        failed_dependencies=[],
        note=note,
    )


def _parse_diagnostic(diag: dict) -> DiagnosticMessage:
    start = diag.get("range", {}).get("start", {})
    end = diag.get("range", {}).get("end", {})
    start_line, start_col = lsp_to_mcp_position(
        LSPLine(start.get("line", 0)), LSPCharacter(start.get("character", 0)),
    )
    end_line, end_col = lsp_to_mcp_position(
        LSPLine(end.get("line", 0)), LSPCharacter(end.get("character", 0)),
    )
    return DiagnosticMessage(
        severity=severity_int_to_string(diag.get("severity", 1)),
        message=diag.get("message", ""),
        line=start_line, column=start_col,
        end_line=end_line, end_column=end_col,
    )
