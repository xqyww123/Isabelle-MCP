from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import Highlight, HighlightsResult
from isa_lsp.utils import (
    IsabelleToolError,
    check_pide_response,
    extract_symbol_at_position,
    lsp_to_mcp_position,
    mcp_to_lsp_position,
    validate_position,
)

_KIND_MAP = {1: "text", 2: "read", 3: "write"}


async def document_highlights(
    client: IsabelleLSPClient, file_path: str, line: int, column: int,
) -> HighlightsResult:
    validate_position(line, column)

    await client.open_document(file_path)

    lsp_line, lsp_col = mcp_to_lsp_position(line, column)

    try:
        response = await client.get_highlights(file_path, lsp_line, lsp_col)
        check_pide_response(response, "get_highlights", allow_none=True)
    except Exception as exc:
        raise IsabelleToolError(f"Failed to get highlights: {exc}") from exc

    symbol = extract_symbol_at_position(file_path, line, column)

    highlights: list[Highlight] = []
    if response and isinstance(response, list):
        for h in response:
            if isinstance(h, dict):
                parsed = _parse_highlight(h)
                if parsed:
                    highlights.append(parsed)

    return HighlightsResult(symbol=symbol, highlights=highlights)


def _parse_highlight(h: dict) -> Highlight | None:
    try:
        r = h.get("range")
        if not r:
            return None
        start, end = r.get("start", {}), r.get("end", {})
        if not start or not end:
            return None
        start_line, start_col = lsp_to_mcp_position(
            start.get("line", 0), start.get("character", 0)
        )
        _, end_col = lsp_to_mcp_position(end.get("line", 0), end.get("character", 0))
        kind = _KIND_MAP.get(h.get("kind", 1), "text")
        return Highlight(line=start_line, start_column=start_col, end_column=end_col, kind=kind)
    except Exception:
        return None
