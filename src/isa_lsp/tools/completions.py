import re

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import CompletionItem, CompletionsResult
from isa_lsp.utils import (
    IsabelleToolError,
    check_pide_response,
    get_line_from_file,
    mcp_to_lsp_position,
    validate_position,
)


async def completions(
    client: IsabelleLSPClient,
    file_path: str,
    line: int,
    column: int,
    max_completions: int = 32,
) -> CompletionsResult:
    validate_position(line, column)
    if max_completions < 1:
        raise IsabelleToolError(f"max_completions must be >= 1, got {max_completions}")

    await client.open_document(file_path)
    await client.set_caret(file_path, line - 1)

    lsp_line, lsp_col = mcp_to_lsp_position(line, column)

    try:
        response = await client.get_completions(file_path, lsp_line, lsp_col)
        check_pide_response(response, "get_completions", allow_none=True)
    except Exception as exc:
        raise IsabelleToolError(f"Failed to get completions: {exc}") from exc

    items: list[CompletionItem] = []
    raw_items: list[dict] = []
    if isinstance(response, dict):
        raw_response_items = response.get("items", [])
        if isinstance(raw_response_items, list):
            raw_items = [item for item in raw_response_items if isinstance(item, dict)]
    elif isinstance(response, list):
        raw_items = [item for item in response if isinstance(item, dict)]

    if raw_items:
        line_content = get_line_from_file(file_path, line)
        prefix = _extract_prefix(line_content, column)

        parsed = [_parse_item(it) for it in raw_items]
        parsed = [it for it in parsed if it.label.strip()]
        _sort_by_relevance(parsed, prefix)
        items = parsed[:max_completions]

    return CompletionsResult(items=items, line_context=get_line_from_file(file_path, line))


_KIND_MAP = {
    1: "text", 2: "method", 3: "function", 4: "constructor",
    5: "field", 6: "variable", 7: "class", 9: "module",
    14: "keyword", 15: "file", 21: "constant",
}


def _parse_item(item: dict) -> CompletionItem:
    kind = _KIND_MAP.get(item.get("kind", 1), "text")
    label = _string_or_empty(item.get("label"))
    insert_text = _string_or_empty(item.get("insertText", label))
    text_edit = item.get("textEdit")
    if isinstance(text_edit, dict) and "newText" in text_edit:
        insert_text = _string_or_empty(text_edit["newText"])

    doc = item.get("documentation")
    if isinstance(doc, dict):
        doc = _string_or_empty(doc.get("value"))
    elif doc is not None:
        doc = str(doc)

    return CompletionItem(
        label=label,
        kind=kind,
        detail=str(item.get("detail") or ""),
        documentation=doc,
        insert_text=insert_text,
    )


def _string_or_empty(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _extract_prefix(line: str, column: int) -> str:
    text_before = line[:column - 1] if column <= len(line) + 1 else line
    words = re.split(r'[\s()\[\]{},:;.]+', text_before)
    return (words[-1] if words else "").lower()


def _sort_by_relevance(items: list[CompletionItem], prefix: str) -> None:
    def key(item: CompletionItem) -> tuple[int, str]:
        label = item.label.lower()
        if label.startswith(prefix):
            return (0, label)
        if prefix in label:
            return (1, label)
        return (2, label)

    items.sort(key=key)
