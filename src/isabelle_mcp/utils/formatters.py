"""Parsing and formatting utilities for PIDE/LSP output."""

import html as html_module
import re
from html.parser import HTMLParser
from typing import Any

from bs4 import BeautifulSoup, NavigableString

from isabelle_mcp.utils.core import IsabelleToolError, LSPCharacter, LSPLine, MCPColumn, MCPLine


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
    'writeln': 'normal',
    'writeln_message': 'normal',
    'tracing': 'tracing',
    'tracing_message': 'tracing',
    'warning': 'warning',
    'warning_message': 'warning',
    'error': 'error',
    'error_message': 'error',
    'information': 'information',
    'information_message': 'information',
    'state_message': 'state',
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


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_find_theorems_tally(text: str) -> tuple[int | None, int | None]:
    """Parse find_theorems' tally line into (found, displayed).

    Three shapes are emitted (find_theorems.ML:476-491):
      - "found N theorem(s)"            with an OPTIONAL same-line "(M displayed)"
        suffix when the limit truncated the result (returned < found).
      - "displaying N theorem(s)"       when no limit cap was hit (total unknown).
      - "found nothing"                 when there were no matches.

    ``text`` must be the output text BEFORE the first theorem item (see
    parse_find_theorems_from_html): find_theorems first echoes the query criteria
    — including the user's quoted name/pattern strings — and only then prints the
    tally, so a criterion literally containing "found 5 theorem(s)" would otherwise
    be mistaken for the tally. The criteria echo precedes the tally, so we take the
    LAST "found N" / "displaying N" match in this pre-item region.
    """
    if "found nothing" in text:
        return 0, 0
    matches = list(re.finditer(r"found (\d+) theorem\(s\)(?:\s*\((\d+) displayed\))?", text))
    if matches:
        m = matches[-1]
        found = int(m.group(1))
        displayed = int(m.group(2)) if m.group(2) is not None else found
        return found, displayed
    matches = list(re.finditer(r"displaying (\d+) theorem\(s\)", text))
    if matches:
        return None, int(matches[-1].group(1))
    return None, None


def _split_name_and_statement(text: str) -> tuple[str, str]:
    """Split a single theorem entry "name: statement" at the first ':'.

    pretty_thm_head (find_theorems.ML:456-458) emits the fact name (with an
    optional "(i)" index suffix) followed by ":" then a break. Fact names never
    contain ':', so the first ':' is the boundary.
    """
    text = _collapse_ws(text)
    idx = text.find(":")
    if idx == -1:
        return "", text
    return text[:idx].strip(), text[idx + 1 :].strip()


def parse_find_theorems_from_html(html: str) -> tuple[int | None, int | None, list[tuple[str, str]]]:
    """Parse rendered find_theorems output into (found, displayed, [(name, stmt), ...]).

    The wire content is browser HTML (the Scala side routes find_theorems output
    through Browser_Info.make_html, mirroring PIDE/output_at_position), so the same
    span-keyed helpers used for goals/command output apply.

    Raises IsabelleToolError when the output carries an Isabelle error message —
    e.g. "Current goal required for intro search criterion" when a goal-requiring
    criterion (intro/elim/dest/solves) is used at a non-proof caret. Such an error
    is embedded in the output body (Query_Operation still reports status finished),
    so it must be surfaced here rather than returned as a bogus theorem.
    """
    if not html:
        return None, None, []
    soup = BeautifulSoup(html, "html.parser")

    # Surface embedded ML errors instead of treating them as results.
    error_nodes = soup.find_all(class_=["error_message", "error"])
    if error_nodes:
        msg = _collapse_ws(" ".join(_pide_html_to_text(e) for e in error_nodes))
        raise IsabelleToolError(msg or "find_theorems failed")

    # Each theorem is a Pretty.item (find_theorems.ML:494); make_html renders it as
    # <span class="item"> (verified against real wire output). Split on those, like
    # parse_goals_from_html splits on its markup-derived spans.
    item_nodes = soup.find_all("span", class_="item")

    # Parse the tally from the text BEFORE the first item only, so neither a
    # theorem statement nor (via _parse_find_theorems_tally) the criteria echo can
    # be mistaken for the tally. With no items (e.g. "found nothing"), use all text.
    if item_nodes:
        pre_item_strings = item_nodes[0].find_all_previous(string=True)
        tally_text = _collapse_ws("".join(str(s) for s in reversed(pre_item_strings)))
    else:
        tally_text = _collapse_ws(_pide_html_to_text(soup))
    found, displayed = _parse_find_theorems_tally(tally_text)

    theorems: list[tuple[str, str]] = []
    for node in item_nodes:
        entry = _pide_html_to_text(node)
        if entry.strip():
            theorems.append(_split_name_and_statement(entry))
    return found, displayed, theorems


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
