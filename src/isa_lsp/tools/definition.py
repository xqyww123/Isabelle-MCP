from isa_lsp.evaluation import check_evaluation_guard
from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import DeclarationLocation, EvaluationResult, Location
from isa_lsp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPColumn,
    MCPLine,
    check_pide_response,
    extract_symbol_at_position,
    lsp_to_mcp_position,
    mcp_to_lsp_position,
    uri_to_file_path,
    validate_position,
)


async def declaration_location(
    client: IsabelleLSPClient, file_path: str, line: MCPLine, column: MCPColumn,
) -> DeclarationLocation:
    validate_position(line, column)

    await client.open_document(file_path)

    guard = await check_evaluation_guard(client, file_path, line)
    if isinstance(guard, EvaluationResult):
        raise IsabelleToolError(guard.message)
    note = guard if isinstance(guard, str) else None

    lsp_line, lsp_col = mcp_to_lsp_position(line, column)

    try:
        response = await client.get_definition(file_path, lsp_line, lsp_col)
        check_pide_response(response, "get_definition", allow_none=True)
    except Exception as exc:
        raise IsabelleToolError(f"Failed to get definition: {exc}") from exc

    symbol = extract_symbol_at_position(file_path, line, column)

    locations: list[Location] = []
    if response is not None:
        loc_list = response if isinstance(response, list) else [response]
        for loc in loc_list:
            if isinstance(loc, dict):
                parsed = _parse_location(loc)
                if parsed:
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
