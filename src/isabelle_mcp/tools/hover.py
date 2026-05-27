import logging

from isabelle_mcp.evaluation import check_evaluation_guard
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import DiagnosticMessage, EvaluationResult, HoverEntry, HoverInfo
from isabelle_mcp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPLine,
    check_pide_response,
    find_symbol_occurrences,
    lsp_to_mcp_position,
    severity_int_to_string,
)

logger = logging.getLogger(__name__)


def _extract_hover_text(response: dict | None) -> str:
    if not response or not isinstance(response, dict):
        return ""
    contents = response.get("contents", {})
    if isinstance(contents, dict):
        return contents.get("value", "")
    if isinstance(contents, str):
        return contents
    if isinstance(contents, list):
        return "\n".join(
            item.get("value", str(item)) if isinstance(item, dict) else str(item)
            for item in contents
        )
    return ""


async def hover_info(
    client: IsabelleLSPClient, file_path: str, line: MCPLine, symbol: str,
) -> HoverInfo:
    if line < 1:
        raise IsabelleToolError(f"line must be >= 1, got {line}")

    await client.open_document(file_path)

    guard = await check_evaluation_guard(client, file_path, line)
    if isinstance(guard, EvaluationResult):
        raise IsabelleToolError(guard.message)
    note = guard if isinstance(guard, str) else None

    doc = client.open_documents.get(file_path)
    if doc is None:
        raise IsabelleToolError(f"Document not open: {file_path}")

    lines = doc.content.split("\n")
    lsp_line_idx = int(line.to_lsp())
    if lsp_line_idx >= len(lines):
        return HoverInfo(symbol=symbol, results=[], line_context="", note=note)

    doc_line = lines[lsp_line_idx]
    lsp_offsets = find_symbol_occurrences(doc_line, symbol)
    if not lsp_offsets:
        raise IsabelleToolError(f"Symbol '{symbol}' not found on line {line}")

    info_map: dict[str, tuple[list[int], list[int]]] = {}
    for occ_idx, lsp_char_offset in enumerate(lsp_offsets, 1):
        try:
            response = await client.get_hover(
                file_path, LSPLine(lsp_line_idx), LSPCharacter(lsp_char_offset),
            )
            check_pide_response(response, "get_hover", allow_none=True)
        except IsabelleToolError:
            raise
        except Exception:
            logger.debug("Hover query failed for occurrence %d: %s", occ_idx, exc_info=True)
            continue

        info_text = _extract_hover_text(response)
        mcp_col = int(LSPCharacter(lsp_char_offset).to_mcp())
        if info_text not in info_map:
            info_map[info_text] = ([], [])
        info_map[info_text][0].append(occ_idx)
        info_map[info_text][1].append(mcp_col)

    results = [
        HoverEntry(info=info_text, occurrences=occs, columns=cols)
        for info_text, (occs, cols) in info_map.items()
    ]

    diagnostics_at_line: list[DiagnosticMessage] = []
    for diag in client.get_cached_diagnostics(file_path):
        diag_range = diag.get("range", {})
        diag_start = diag_range.get("start", {})
        if diag_start.get("line", -1) == lsp_line_idx:
            diag_end = diag_range.get("end", {})
            diag_mcp_line, diag_mcp_col = lsp_to_mcp_position(
                LSPLine(diag_start.get("line", 0)),
                LSPCharacter(diag_start.get("character", 0)),
            )
            diag_end_line, diag_end_col = lsp_to_mcp_position(
                LSPLine(diag_end.get("line", 0)),
                LSPCharacter(diag_end.get("character", 0)),
            )
            diagnostics_at_line.append(DiagnosticMessage(
                severity=severity_int_to_string(diag.get("severity", 1)),
                message=diag.get("message", ""),
                line=diag_mcp_line, column=diag_mcp_col,
                end_line=diag_end_line, end_column=diag_end_col,
            ))

    return HoverInfo(
        symbol=symbol, results=results,
        line_context=doc_line, diagnostics=diagnostics_at_line,
        note=note,
    )
