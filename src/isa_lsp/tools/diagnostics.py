from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import DiagnosticMessage, DiagnosticsResult
from isa_lsp.utils import IsabelleToolError, lsp_to_mcp_position, severity_int_to_string


async def diagnostic_messages(
    client: IsabelleLSPClient,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> DiagnosticsResult:
    if start_line is not None and start_line < 1:
        raise IsabelleToolError(f"start_line must be >= 1, got {start_line}")
    if end_line is not None and end_line < 1:
        raise IsabelleToolError(f"end_line must be >= 1, got {end_line}")
    if start_line is not None and end_line is not None and start_line > end_line:
        raise IsabelleToolError(
            f"start_line must be <= end_line, got {start_line} > {end_line}"
        )

    await client.open_document(file_path)

    # Set caret to end_line or end of file so Isabelle processes the needed region
    doc = client.open_documents.get(file_path)
    if doc is not None:
        if end_line is not None:
            caret_line = end_line - 1  # 0-indexed
        else:
            caret_line = doc.content.count("\n")
        await client.set_caret(file_path, caret_line)

    items: list[DiagnosticMessage] = []
    for diag in client.get_cached_diagnostics(file_path):
        diag_line, _ = lsp_to_mcp_position(
            diag.get("range", {}).get("start", {}).get("line", 0), 0
        )
        if start_line is not None and diag_line < start_line:
            continue
        if end_line is not None and diag_line > end_line:
            continue
        items.append(_parse_diagnostic(diag))

    processing_complete = client.diagnostics_settled(file_path)

    return DiagnosticsResult(
        success=processing_complete and all(it.severity != "error" for it in items),
        items=items,
        processing_complete=processing_complete,
        failed_dependencies=[],
    )


def _parse_diagnostic(diag: dict) -> DiagnosticMessage:
    start = diag.get("range", {}).get("start", {})
    end = diag.get("range", {}).get("end", {})
    start_line, start_col = lsp_to_mcp_position(start.get("line", 0), start.get("character", 0))
    end_line, end_col = lsp_to_mcp_position(end.get("line", 0), end.get("character", 0))
    return DiagnosticMessage(
        severity=severity_int_to_string(diag.get("severity", 1)),
        message=diag.get("message", ""),
        line=start_line, column=start_col,
        end_line=end_line, end_column=end_col,
    )
