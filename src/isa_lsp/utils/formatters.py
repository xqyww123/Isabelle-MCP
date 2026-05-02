"""Parsing and formatting utilities for PIDE/LSP output."""

import html as html_module
import re
from html.parser import HTMLParser
from typing import Any

from bs4 import BeautifulSoup, NavigableString

from isa_lsp.utils.core import LSPCharacter, LSPLine, MCPColumn, MCPLine


def _pide_html_to_text(el: Any) -> str:
    """Convert a PIDE HTML element tree to plain text.

    Isabelle's state panel emits deeply nested <span class="block"> with
    <span class="break"> </span> for token separators.  We turn "break"
    spans into spaces and recurse into everything else.
    """
    parts: list[str] = []
    for child in el.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif child.name == "span" and "break" in (child.get("class") or []):
            parts.append(" ")
        else:
            parts.append(_pide_html_to_text(child))
    return "".join(parts)


def strip_html_tags(html: str) -> str:
    text = re.sub(r'<[^>]+>', '', html)
    text = html_module.unescape(text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def parse_goals_from_html(html: str) -> list[str]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    subgoals = soup.find_all("span", class_="subgoal")
    if subgoals:
        goals: list[str] = []
        for sg in subgoals:
            text = _pide_html_to_text(sg).strip()
            text = re.sub(r"^\d+\.\s*", "", text)
            if text:
                goals.append(text)
        return goals

    text = soup.get_text()
    if "no goals" in text.lower():
        return []
    return _parse_numbered_goals(text)


def _parse_numbered_goals(text: str) -> list[str]:
    goals: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\.\s+(.*)", line)
        if m:
            if current:
                goals.append("\n".join(current))
            current = [m.group(2)]
        elif current and line.strip():
            current.append(line.strip())
    if current:
        goals.append("\n".join(current))
    return goals


_COMMAND_OUTPUT_KIND_BY_CSS_CLASS = {
    'writeln': 'writeln',
    'writeln_message': 'writeln',
    'warning': 'warning',
    'warning_message': 'warning',
    'error': 'error',
    'error_message': 'error',
    'information': 'information',
    'information_message': 'information',
    'state_message': 'information',
    'tracing': 'writeln',
    'tracing_message': 'writeln',
}


def _normalize_command_output_text(text: str) -> str:
    text = text.replace("⌂", "")
    text = html_module.unescape(text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


class _CommandOutputHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[dict[str, str]] = []
        self._current_kind: str | None = None
        self._current_text: list[str] = []
        self._current_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._current_kind is not None:
            self._current_depth += 1
            return

        attr_map = dict(attrs)
        css_classes = (attr_map.get("class") or "").split()
        kind = next(
            (
                _COMMAND_OUTPUT_KIND_BY_CSS_CLASS[css_class]
                for css_class in css_classes
                if css_class in _COMMAND_OUTPUT_KIND_BY_CSS_CLASS
            ),
            None,
        )
        if kind is None:
            return

        self._current_kind = kind
        self._current_text = []
        self._current_depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self._current_kind is None:
            return

        self._current_depth -= 1
        if self._current_depth > 0:
            return

        text = _normalize_command_output_text("".join(self._current_text))
        if text:
            self.messages.append({"kind": self._current_kind, "text": text})
        self._current_kind = None
        self._current_text = []
        self._current_depth = 0

    def handle_data(self, data: str) -> None:
        if self._current_kind is not None:
            self._current_text.append(data)


def parse_command_output_html(html: str) -> list[dict[str, str]]:
    parser = _CommandOutputHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.messages


def get_line_from_file(file_path: str, line: MCPLine) -> str:
    try:
        with open(file_path, encoding='utf-8') as f:
            lines = f.readlines()
            if 1 <= line <= len(lines):
                return lines[line - 1].rstrip('\n')
            return ""
    except (OSError, FileNotFoundError):
        return ""


def extract_symbol_from_lsp_range(file_path: str, lsp_range: dict[str, Any]) -> str:
    try:
        start = lsp_range['start']
        end = lsp_range['end']
        start_line, start_char = int(start["line"]), int(start["character"])
        end_line, end_char = int(end["line"]), int(end["character"])

        with open(file_path, encoding='utf-8') as f:
            lines = f.readlines()

        if start_line == end_line:
            if start_line < len(lines):
                return lines[start_line][start_char:end_char]
            return ""

        result: list[str] = []
        for idx in range(start_line, end_line + 1):
            if idx >= len(lines):
                break
            line = lines[idx]
            if idx == start_line:
                result.append(line[start_char:])
            elif idx == end_line:
                result.append(line[:end_char])
            else:
                result.append(line)
        return ''.join(result).strip()
    except (OSError, KeyError, FileNotFoundError, IndexError, TypeError, ValueError):
        return ""


def extract_symbol_at_position(file_path: str, line: MCPLine, column: MCPColumn) -> str:
    """Extract the identifier at a 1-indexed position in a file."""
    try:
        with open(file_path, encoding='utf-8') as f:
            lines = f.readlines()
        if line < 1 or line > len(lines):
            return ""
        line_content = lines[line - 1]
        # Isabelle identifiers: letters, digits, _, ., '  plus Unicode operators
        for match in re.finditer(r"[a-zA-Z0-9_.']+|[⟹⟶∧∨¬∀∃]", line_content):
            start, end = match.span()
            if start < column <= end:
                return match.group()
        return ""
    except Exception:
        return ""


_SEVERITY_MAP = {1: "error", 2: "warning", 3: "information", 4: "hint"}


def severity_int_to_string(severity: int) -> str:
    return _SEVERITY_MAP.get(severity, "error")
