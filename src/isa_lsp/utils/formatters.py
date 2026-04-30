"""
Response formatting utilities for parsing PIDE output.
"""

import html as html_module
import re
from typing import Any


def strip_html_tags(html: str) -> str:
    """Strip HTML tags from text.

    Args:
        html: HTML string

    Returns:
        Plain text with HTML tags removed

    Examples:
        >>> strip_html_tags("<div>Hello</div>")
        'Hello'

        >>> strip_html_tags("<html><body><p>Test</p></body></html>")
        'Test'
    """
    # Remove all HTML tags
    text = re.sub(r'<[^>]+>', '', html)

    # Decode HTML entities
    text = html_module.unescape(text)

    # Clean up whitespace
    text = re.sub(r'\s+', ' ', text)

    return text.strip()


def parse_goals_from_html(html: str) -> list[str]:
    """Extract goal text from PIDE HTML output.

    Args:
        html: HTML output from PIDE state panel

    Returns:
        List of goal strings

    Examples:
        >>> html = '<pre>goal (2 subgoals):\\n 1. P x\\n 2. Q y</pre>'
        >>> parse_goals_from_html(html)
        ['P x', 'Q y']

        >>> parse_goals_from_html('<html>no goals</html>')
        []
    """
    # Remove HTML tags but preserve newlines
    text = re.sub(r'<[^>]+>', '\n', html)

    # Decode HTML entities
    text = html_module.unescape(text)

    # Handle "no goals" case
    if "no goals" in text.lower():
        return []

    # Extract goals
    goals = []

    # Split into lines
    lines = text.split('\n')

    for line in lines:
        line = line.strip()

        # Skip empty lines
        if not line:
            continue

        # Match goal patterns:
        # - "1. goal_text"
        # - "⋀x. goal_text"
        # - Goal text after "goal (N subgoals):"

        # Remove goal numbering
        if re.match(r'^\d+\.', line):
            # Remove leading number
            goal_text = re.sub(r'^\d+\.\s*', '', line)
            goals.append(goal_text)
        elif re.match(r'^⋀', line):
            # Universal quantifier goal
            goals.append(line)

    return goals


def parse_command_output_html(html: str) -> list[dict]:
    """Parse PIDE dynamic output HTML into structured messages.

    Args:
        html: HTML from PIDE/dynamic_output

    Returns:
        List of message dictionaries with 'kind' and 'text' keys

    Examples:
        >>> html = '<div class="writeln">Success</div><div class="warning">Unused</div>'
        >>> parse_command_output_html(html)
        [{'kind': 'writeln', 'text': 'Success'}, {'kind': 'warning', 'text': 'Unused'}]
    """
    messages = []

    # Extract message divs with class attributes
    pattern = r'<div class=[\'"]([^\'"]+)[\'"]>(.*?)</div>'

    for match in re.finditer(pattern, html, re.DOTALL):
        css_class = match.group(1)
        content = match.group(2)

        # Strip HTML from content
        text = strip_html_tags(content)

        # Map CSS class to message kind
        kind_map = {
            'writeln': 'writeln',
            'warning': 'warning',
            'error': 'error',
            'information': 'information',
            'tracing': 'writeln',
        }

        kind = kind_map.get(css_class, 'writeln')

        messages.append({
            'kind': kind,
            'text': text
        })

    return messages


def get_line_from_file(file_path: str, line: int) -> str:
    """Get a specific line from a file.

    Args:
        file_path: Absolute path to file
        line: Line number (1-indexed)

    Returns:
        The line content (without newline)

    Examples:
        >>> # Assuming file has content
        >>> get_line_from_file("/path/to/file.thy", 1)
        'theory Example imports Main begin'
    """
    try:
        with open(file_path, encoding='utf-8') as f:
            lines = f.readlines()
            if 1 <= line <= len(lines):
                return lines[line - 1].rstrip('\n')
            else:
                return ""
    except (OSError, FileNotFoundError):
        return ""


def extract_symbol_from_range(text: str, start: int, end: int) -> str:
    """Extract substring from text.

    Args:
        text: The text string
        start: Start position (0-indexed)
        end: End position (0-indexed)

    Returns:
        The extracted substring

    Examples:
        >>> extract_symbol_from_range("lemma test: P", 6, 10)
        'test'
    """
    try:
        if start < 0 or end > len(text):
            return ""
        return text[start:end]
    except (IndexError, TypeError):
        return ""


def extract_symbol_from_lsp_range(
    file_path: str,
    lsp_range: dict[str, Any],
) -> str:
    """Extract symbol text from a file using LSP range.

    Args:
        file_path: Absolute path to file
        lsp_range: LSP range dict with 'start' and 'end' positions

    Returns:
        The symbol text

    Examples:
        >>> lsp_range = {
        ...     'start': {'line': 0, 'character': 5},
        ...     'end': {'line': 0, 'character': 8}
        ... }
        >>> # Assuming file has "lemma Suc n = ..."
        >>> extract_symbol_from_range("/path/to/file.thy", lsp_range)
        'Suc'
    """
    try:
        start = lsp_range['start']
        end = lsp_range['end']

        start_line = int(start["line"])
        start_char = int(start["character"])
        end_line = int(end["line"])
        end_char = int(end["character"])

        with open(file_path, encoding='utf-8') as f:
            lines = f.readlines()

        # Single line range
        if start_line == end_line:
            if start_line < len(lines):
                line = lines[start_line]
                return line[start_char:end_char]

        # Multi-line range (rare)
        result: list[str] = []
        for line_idx in range(start_line, end_line + 1):
            if line_idx >= len(lines):
                break

            line = lines[line_idx]

            if line_idx == start_line:
                result.append(line[start_char:])
            elif line_idx == end_line:
                result.append(line[:end_char])
            else:
                result.append(line)

        return ''.join(result).strip()

    except (OSError, KeyError, FileNotFoundError, IndexError, TypeError, ValueError):
        return ""


_SEVERITY_MAP = {1: "error", 2: "warning", 3: "information", 4: "hint"}


def severity_int_to_string(severity: int) -> str:
    """Convert LSP severity enum (1=Error, 2=Warning, 3=Information, 4=Hint) to string."""
    return _SEVERITY_MAP.get(severity, "error")


def format_hover_content(contents: Any) -> str:
    """Format LSP hover contents to plain text.

    Args:
        contents: LSP hover contents (can be string, dict, or list)

    Returns:
        Formatted text string

    Examples:
        >>> format_hover_content("Simple text")
        'Simple text'
        >>> format_hover_content({"kind": "markdown", "value": "**Bold**"})
        '**Bold**'
    """
    if isinstance(contents, str):
        return contents

    if isinstance(contents, dict):
        # MarkupContent format
        if "value" in contents:
            return str(contents["value"])
        # MarkedString format
        if "language" in contents and "value" in contents:
            return str(contents["value"])

    if isinstance(contents, list):
        # Array of MarkedString or strings
        result: list[str] = []
        for item in contents:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                if "value" in item:
                    result.append(str(item["value"]))
        return "\n".join(result)

    return str(contents)
