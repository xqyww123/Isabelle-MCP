import logging

from isabelle_mcp.evaluation import check_evaluation_guard, format_evaluation_result
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import EvaluationView, LocalOccurrencesResult, Occurrence
from isabelle_mcp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPLine,
    check_pide_response,
    find_symbol_occurrences,
    lsp_to_mcp_position,
)

logger = logging.getLogger(__name__)


async def local_occurrences(
    client: IsabelleLSPClient, file_path: str, line: MCPLine, symbol: str,
) -> LocalOccurrencesResult:
    if line < 1:
        raise IsabelleToolError(f"line must be >= 1, got {line}")

    await client.open_document(file_path)

    guard = await check_evaluation_guard(client, file_path, line)
    if isinstance(guard, EvaluationView):
        raise IsabelleToolError(format_evaluation_result(guard, client.project_root))
    note = guard if isinstance(guard, str) else None

    doc = client.open_documents.get(file_path)
    if doc is None:
        raise IsabelleToolError(f"Document not open: {file_path}")

    lines = doc.content.split("\n")
    lsp_line_idx = int(line.to_lsp())
    if lsp_line_idx >= len(lines):
        return LocalOccurrencesResult(symbol=symbol, occurrences=[], note=note)

    doc_line = lines[lsp_line_idx]
    lsp_offsets = find_symbol_occurrences(doc_line, symbol)
    if not lsp_offsets:
        raise IsabelleToolError(f"Symbol '{symbol}' not found on line {line}")

    # Querying any occurrence resolves the same entity, but distinct same-text
    # tokens on the line may be different entities, so query each and merge.
    seen: set[tuple[int, int, int, str]] = set()
    occurrences: list[Occurrence] = []
    for lsp_char_offset in lsp_offsets:
        try:
            response = await client.get_highlights(
                file_path, LSPLine(lsp_line_idx), LSPCharacter(lsp_char_offset),
            )
            check_pide_response(response, "get_highlights", allow_none=True)
        except IsabelleToolError:
            raise
        except Exception:
            logger.debug("Highlight query failed at offset %d", lsp_char_offset, exc_info=True)
            continue

        if not response or not isinstance(response, list):
            continue
        for h in response:
            if not isinstance(h, dict):
                continue
            parsed = _parse_occurrence(h)
            if parsed is None:
                continue
            key = (parsed.line, parsed.start_column, parsed.end_column)
            if key not in seen:
                seen.add(key)
                occurrences.append(parsed)

    occurrences.sort(key=lambda o: (o.line, o.start_column))
    return LocalOccurrencesResult(symbol=symbol, occurrences=occurrences, note=note)


def _parse_occurrence(h: dict) -> Occurrence | None:
    try:
        r = h.get("range")
        if not r:
            return None
        start, end = r.get("start", {}), r.get("end", {})
        if not start or not end:
            return None
        start_line, start_col = lsp_to_mcp_position(
            LSPLine(start.get("line", 0)),
            LSPCharacter(start.get("character", 0)),
        )
        _, end_col = lsp_to_mcp_position(
            LSPLine(end.get("line", 0)),
            LSPCharacter(end.get("character", 0)),
        )
        return Occurrence(line=start_line, start_column=start_col, end_column=end_col)
    except Exception:
        return None
