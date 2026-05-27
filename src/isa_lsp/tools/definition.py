import logging

from isa_lsp.evaluation import check_evaluation_guard
from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import DeclarationLocation, EvaluationResult, Location
from isa_lsp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPLine,
    check_pide_response,
    find_symbol_occurrences,
    lsp_to_mcp_position,
    uri_to_file_path,
)

logger = logging.getLogger(__name__)


async def declaration_location(
    client: IsabelleLSPClient, file_path: str, line: MCPLine, symbol: str,
) -> DeclarationLocation:
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
        return DeclarationLocation(symbol=symbol, locations=[], note=note)

    doc_line = lines[lsp_line_idx]
    lsp_offsets = find_symbol_occurrences(doc_line, symbol)
    if not lsp_offsets:
        raise IsabelleToolError(f"Symbol '{symbol}' not found on line {line}")

    seen: set[tuple[str, int, int]] = set()
    locations: list[Location] = []
    for lsp_char_offset in lsp_offsets:
        try:
            response = await client.get_definition(
                file_path, LSPLine(lsp_line_idx), LSPCharacter(lsp_char_offset),
            )
            check_pide_response(response, "get_definition", allow_none=True)
        except IsabelleToolError:
            raise
        except Exception:
            logger.debug("Definition query failed at offset %d", lsp_char_offset, exc_info=True)
            continue

        if response is None:
            continue
        loc_list = response if isinstance(response, list) else [response]
        for loc in loc_list:
            if not isinstance(loc, dict):
                continue
            parsed = _parse_location(loc)
            if parsed is None:
                continue
            key = (parsed.file_path, parsed.line, parsed.column)
            if key not in seen:
                seen.add(key)
                locations.append(parsed)

    return DeclarationLocation(symbol=symbol, locations=locations, note=note)


def _parse_location(loc: dict) -> Location | None:
    try:
        if "targetUri" in loc:
            uri = loc["targetUri"]
            range_dict = loc.get("targetRange", loc.get("targetSelectionRange", {}))
        elif "uri" in loc:
            uri = loc["uri"]
            range_dict = loc.get("range", {})
        else:
            return None

        file_path = uri_to_file_path(uri)
        start = range_dict.get("start", {})
        mcp_line, mcp_col = lsp_to_mcp_position(
            LSPLine(start.get("line", 0)),
            LSPCharacter(start.get("character", 0)),
        )
        return Location(file_path=file_path, line=mcp_line, column=mcp_col)
    except Exception:
        return None
