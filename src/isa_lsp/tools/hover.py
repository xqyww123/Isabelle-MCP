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
    client: IsabelleLSPClient, file_path: str, line: int, column: int,
) -> HoverInfo:
    validate_position(line, column)

    await client.open_document(file_path)
    await client.set_caret(file_path, line - 1)

    lsp_line, lsp_col = mcp_to_lsp_position(line, column)

    try:
        response = await client.get_hover(file_path, lsp_line, lsp_col)
        check_pide_response(response, "get_hover", allow_none=True)
    except Exception as exc:
        raise IsabelleToolError(f"Failed to get hover info: {exc}") from exc

    symbol = ""
    info_text = ""

    if response and isinstance(response, dict):
        if "range" in response:
            symbol = extract_symbol_from_lsp_range(file_path, response["range"])

        contents = response.get("contents", {})
        if isinstance(contents, dict):
            info_text = contents.get("value", "")
        elif isinstance(contents, str):
            info_text = contents
        elif isinstance(contents, list):
            info_text = "\n".join(
                item.get("value", str(item)) if isinstance(item, dict) else str(item)
                for item in contents
            )

    line_context = get_line_from_file(file_path, line)

    diagnostics_at_position = []
    for diag in client.get_cached_diagnostics(file_path):
        diag_range = diag.get("range", {})
        diag_start = diag_range.get("start", {})
        if diag_start.get("line", -1) == lsp_line:
            diag_mcp_line, diag_mcp_col = lsp_to_mcp_position(
                diag_start.get("line", 0), diag_start.get("character", 0)
            )
            diag_end = diag_range.get("end", {})
            diag_end_line, diag_end_col = lsp_to_mcp_position(
                diag_end.get("line", 0), diag_end.get("character", 0)
            )
            diagnostics_at_position.append(DiagnosticMessage(
                severity=severity_int_to_string(diag.get("severity", 1)),
                message=diag.get("message", ""),
                line=diag_mcp_line, column=diag_mcp_col,
                end_line=diag_end_line, end_column=diag_end_col,
            ))

    return HoverInfo(
        symbol=symbol, info=info_text,
        line_context=line_context, diagnostics=diagnostics_at_position,
    )
