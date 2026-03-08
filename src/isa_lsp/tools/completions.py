"""
Code completion tool implementation.
"""

from typing import Annotated

from pydantic import Field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import CompletionsResult, CompletionItem
from isa_lsp.utils import (
    IsabelleToolError,
    check_pide_response,
    mcp_to_lsp_position,
    get_line_from_file,
)


async def completions(
    client: IsabelleLSPClient,
    file_path: Annotated[str, Field(description="Absolute path to .thy file")],
    line: Annotated[int, Field(description="Line number (1-indexed)", ge=1)],
    column: Annotated[int, Field(description="Column number (1-indexed)", ge=1)],
    max_completions: Annotated[int, Field(
        description="Maximum number of completions to return", ge=1
    )] = 32,
) -> CompletionsResult:
    """Get code completion suggestions.

    Args:
        client: LSP client instance
        file_path: Absolute path to theory file
        line: Line number (1-indexed)
        column: Column number (1-indexed)
        max_completions: Maximum results to return

    Returns:
        CompletionsResult with completion items

    Raises:
        IsabelleToolError: If document not open or LSP error
    """
    # Ensure document is open
    if file_path not in client.open_documents:
        await client.open_document(file_path)

    # Convert to 0-indexed for LSP
    lsp_line, lsp_col = mcp_to_lsp_position(line, column)

    # Call LSP
    try:
        response = await client.get_completions(file_path, lsp_line, lsp_col)
        check_pide_response(response, "get_completions", allow_none=True)
    except Exception as e:
        raise IsabelleToolError(f"Failed to get completions: {e}")

    # Parse response
    items = []

    if response and isinstance(response, dict):
        # Extract completion items
        completion_items = response.get("items", [])

        # Get line context for prefix matching
        line_content = get_line_from_file(file_path, line)
        prefix = _extract_prefix(line_content, column)

        # Parse and filter items
        parsed_items = []
        for item in completion_items:
            parsed_item = _parse_completion_item(item)
            # Filter out invalid items (missing or empty label)
            if parsed_item.label and parsed_item.label.strip():
                parsed_items.append(parsed_item)

        # Sort by relevance
        sorted_items = _sort_by_relevance(parsed_items, prefix)

        # Limit results
        items = sorted_items[:max_completions]

    # Get line context
    line_context = get_line_from_file(file_path, line)

    return CompletionsResult(
        items=items,
        line_context=line_context,
    )


def _parse_completion_item(item: dict) -> CompletionItem:
    """Parse LSP completion item to our model.

    Args:
        item: LSP CompletionItem dictionary

    Returns:
        CompletionItem model
    """
    # Map LSP kind enum to string
    kind_mapping = {
        1: "text",
        2: "method",
        3: "function",
        4: "constructor",
        5: "field",
        6: "variable",
        7: "class",
        9: "module",
        14: "keyword",
        15: "file",
        21: "constant",
    }

    kind = kind_mapping.get(item.get("kind", 1), "text")

    # Extract insert text
    insert_text = item.get("insertText", item.get("label", ""))

    # Check for textEdit
    if "textEdit" in item:
        text_edit = item["textEdit"]
        if "newText" in text_edit:
            insert_text = text_edit["newText"]

    return CompletionItem(
        label=item.get("label", ""),
        kind=kind,
        detail=item.get("detail"),
        documentation=_extract_documentation(item.get("documentation")),
        insert_text=insert_text,
    )


def _extract_documentation(doc: any) -> str:
    """Extract documentation string from various formats.

    Args:
        doc: Documentation in various LSP formats

    Returns:
        Documentation string
    """
    if doc is None:
        return None
    elif isinstance(doc, str):
        return doc
    elif isinstance(doc, dict):
        # MarkupContent
        return doc.get("value", "")
    else:
        return str(doc)


def _extract_prefix(line: str, column: int) -> str:
    """Extract word prefix before cursor position.

    Args:
        line: Line content
        column: Column (1-indexed)

    Returns:
        Prefix string (lowercase)
    """
    # Get text before cursor
    text_before = line[:column - 1] if column <= len(line) + 1 else line

    # Extract last word
    import re
    words = re.split(r'[\s()\[\]{},:;.]+', text_before)
    prefix = words[-1] if words else ""

    return prefix.lower()


def _sort_by_relevance(items: list[CompletionItem], prefix: str) -> list[CompletionItem]:
    """Sort completions by relevance to prefix.

    Args:
        items: List of completion items
        prefix: Prefix string (lowercase)

    Returns:
        Sorted list
    """
    def sort_key(item: CompletionItem):
        label_lower = item.label.lower()

        if label_lower.startswith(prefix):
            return (0, label_lower)  # Exact prefix match (highest priority)
        elif prefix in label_lower:
            return (1, label_lower)  # Contains prefix (medium priority)
        else:
            return (2, label_lower)  # Alphabetical (lowest priority)

    items.sort(key=sort_key)
    return items
