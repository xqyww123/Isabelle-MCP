"""Isabelle tokenizer and symbol occurrence finder.

Implements a simplified Isabelle lexer based on symbol_pos.ML and lexicon.ML,
operating in ASCII space via the vendored isabelle_symbols module.
"""

import bisect
import logging

from isabelle_mcp.utils.core import IsabelleToolError
from isabelle_mcp.utils.isabelle_symbols import ascii_of_unicode, symbol_explode

logger = logging.getLogger(__name__)

# Explicit enumeration from symbol.ML lines 246-386.
# NOTE: \<lambda> is intentionally excluded (it is an operator, not a letter).
_LETTER_SYMBOLS: frozenset[str] = frozenset([
    # Latin variants \<A>..\<Z>, \<a>..\<z>
    *[f"\\<{c}>" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"],
    # Double-letter variants \<AA>..\<ZZ>, \<aa>..\<zz>
    *[f"\\<{c}{c}>" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"],
    # Greek lowercase (without lambda)
    "\\<alpha>", "\\<beta>", "\\<gamma>", "\\<delta>", "\\<epsilon>",
    "\\<zeta>", "\\<eta>", "\\<theta>", "\\<iota>", "\\<kappa>",
    "\\<mu>", "\\<nu>", "\\<xi>", "\\<pi>", "\\<rho>",
    "\\<sigma>", "\\<tau>", "\\<upsilon>", "\\<phi>", "\\<chi>",
    "\\<psi>", "\\<omega>",
    # Greek uppercase
    "\\<Gamma>", "\\<Delta>", "\\<Theta>", "\\<Lambda>", "\\<Xi>",
    "\\<Pi>", "\\<Sigma>", "\\<Upsilon>", "\\<Phi>", "\\<Psi>", "\\<Omega>",
])


def _is_letter(sym: str) -> bool:
    if len(sym) == 1 and sym.isascii():
        return sym.isalpha()
    return sym in _LETTER_SYMBOLS


def _is_digit(sym: str) -> bool:
    return len(sym) == 1 and sym.isascii() and sym.isdigit()


def _is_letdig(sym: str) -> bool:
    if _is_letter(sym):
        return True
    if len(sym) == 1 and sym.isascii():
        return sym.isdigit() or sym in "_'"
    return False


def _is_blank(sym: str) -> bool:
    return sym in (" ", "\t", "\n", "\x0b", "\f", "\r")


def _is_sub(sym: str) -> bool:
    return sym == "\\<^sub>"


def _scan_ident_tail(symbols: list[str], pos: int) -> int:
    """Consume letdigs and \\<^sub>-letdig groups after the initial letter."""
    n = len(symbols)
    while pos < n:
        if _is_letdig(symbols[pos]):
            pos += 1
        elif _is_sub(symbols[pos]) and pos + 1 < n and _is_letdig(symbols[pos + 1]):
            pos += 1  # consume \<^sub>
            while pos < n and _is_letdig(symbols[pos]):
                pos += 1
        else:
            break
    return pos


def tokenize_isabelle_line(ascii_text: str) -> list[tuple[str, int, int]]:
    """Tokenize Isabelle ASCII text into (token_text, ascii_offset, symbol_index) triples."""
    symbols = symbol_explode(ascii_text)

    offsets: list[int] = []
    off = 0
    for sym in symbols:
        offsets.append(off)
        off += len(sym)

    tokens: list[tuple[str, int, int]] = []
    pos = 0
    n = len(symbols)

    while pos < n:
        sym = symbols[pos]

        if _is_blank(sym):
            pos += 1
            continue

        if _is_letter(sym):
            start = pos
            pos = _scan_ident_tail(symbols, pos + 1)
            while (
                pos < n
                and symbols[pos] == "."
                and pos + 1 < n
                and _is_letter(symbols[pos + 1])
            ):
                pos = _scan_ident_tail(symbols, pos + 2)
            tokens.append(("".join(symbols[start:pos]), offsets[start], start))
            continue

        if sym == "'" and pos + 1 < n and _is_letter(symbols[pos + 1]):
            start = pos
            pos = _scan_ident_tail(symbols, pos + 2)
            tokens.append(("".join(symbols[start:pos]), offsets[start], start))
            continue

        if sym == "?" and pos + 1 < n:
            next_sym = symbols[pos + 1]
            if _is_letter(next_sym) or (
                next_sym == "'" and pos + 2 < n and _is_letter(symbols[pos + 2])
            ):
                start = pos
                pos += 1
                if symbols[pos] == "'":
                    pos += 1
                pos = _scan_ident_tail(symbols, pos + 1)
                if (
                    pos < n
                    and symbols[pos] == "."
                    and pos + 1 < n
                    and _is_digit(symbols[pos + 1])
                ):
                    pos += 1
                    while pos < n and _is_digit(symbols[pos]):
                        pos += 1
                tokens.append(("".join(symbols[start:pos]), offsets[start], start))
                continue

        if _is_digit(sym):
            start = pos
            while pos < n and _is_digit(symbols[pos]):
                pos += 1
            tokens.append(("".join(symbols[start:pos]), offsets[start], start))
            continue

        tokens.append((sym, offsets[pos], pos))
        pos += 1

    return tokens


def find_symbol_occurrences(doc_line: str, symbol: str) -> list[int]:
    """Find all token-level occurrences of symbol on a line.

    Returns 0-indexed character offsets in the original doc_line coordinate space
    (suitable as LSP character positions). Capped at 10 results.
    """
    ascii_line = ascii_of_unicode(doc_line)
    ascii_target = ascii_of_unicode(symbol)

    line_tokens = tokenize_isabelle_line(ascii_line)
    target_tokens = tokenize_isabelle_line(ascii_target)

    if not target_tokens or not line_tokens:
        return []

    doc_symbols = symbol_explode(doc_line)
    doc_offsets: list[int] = []
    off = 0
    for sym in doc_symbols:
        doc_offsets.append(off)
        off += len(sym)

    target_texts = [t[0] for t in target_tokens]
    target_len = len(target_texts)
    results: list[int] = []

    for i in range(len(line_tokens) - target_len + 1):
        if all(line_tokens[i + j][0] == target_texts[j] for j in range(target_len)):
            sym_idx = line_tokens[i][2]
            if sym_idx < len(doc_offsets):
                results.append(doc_offsets[sym_idx])
            if len(results) >= 10:
                break

    return results


def find_after_text_caret(
    lines: list[str], line_idx: int, after_text: str,
) -> tuple[int, int] | None:
    """Locate after_text and return the caret position immediately AFTER it.

    after_text is matched as a sequence of Isabelle tokens (like
    find_symbol_occurrences), so ASCII and Unicode forms are equivalent and the
    match lands on token boundaries. The match must BEGIN on line ``line_idx``
    (0-indexed) but may extend onto following lines — newlines are ordinary token
    separators, so a command split across several lines still matches.

    Returns the (line, character) position (both 0-indexed, LSP coordinates) just
    past the last matched token, or None if after_text does not occur as such a
    token run starting on ``line_idx``. When it occurs more than once, the first
    occurrence is used.
    """
    if line_idx < 0 or line_idx >= len(lines):
        return None

    sub_lines = lines[line_idx:]
    sub_text = "\n".join(sub_lines)
    ascii_sub = ascii_of_unicode(sub_text)
    ascii_target = ascii_of_unicode(after_text)

    sub_tokens = tokenize_isabelle_line(ascii_sub)
    target_tokens = tokenize_isabelle_line(ascii_target)
    if not target_tokens or not sub_tokens:
        return None

    # Char offset of each symbol within sub_text (original, non-ASCII coordinates).
    sub_symbols = symbol_explode(sub_text)
    sub_offsets: list[int] = []
    off = 0
    for sym in sub_symbols:
        sub_offsets.append(off)
        off += len(sym)
    total_len = off

    # Char offset at which each line begins within sub_text.
    line_starts: list[int] = [0]
    for ln in sub_lines:
        line_starts.append(line_starts[-1] + len(ln) + 1)  # +1 for the '\n'
    first_line_len = len(sub_lines[0])

    target_texts = [t[0] for t in target_tokens]
    target_len = len(target_texts)

    for i in range(len(sub_tokens) - target_len + 1):
        start_off = sub_offsets[sub_tokens[i][2]]
        # Token starts are monotonically increasing: once we pass the first line,
        # no later match can begin on line_idx.
        if start_off >= first_line_len:
            break
        if all(sub_tokens[i + j][0] == target_texts[j] for j in range(target_len)):
            last_token = sub_tokens[i + target_len - 1]
            end_sym_idx = last_token[2] + len(symbol_explode(last_token[0]))
            end_off = sub_offsets[end_sym_idx] if end_sym_idx < len(sub_offsets) else total_len
            k = bisect.bisect_right(line_starts, end_off) - 1
            return (line_idx + k, end_off - line_starts[k])

    return None


def resolve_caret(
    lines: list[str], lsp_line_idx: int, after_text: str | None, line_label: object,
) -> tuple[int, int]:
    """Resolve the caret position whose enclosing command a query reports.

    Without after_text, anchor on the last non-blank character of the line so the
    caret falls INSIDE the command (the exact end of the line sits on the command
    boundary and would resolve to the following command). With after_text, return
    the position just past that token run (see find_after_text_caret).

    Returns an (lsp_line, lsp_character) pair (both 0-indexed). ``line_label`` is
    the 1-indexed line as the user supplied it, used only in error messages.
    """
    if lsp_line_idx >= len(lines):
        raise IsabelleToolError(f"line {line_label} is beyond the end of the file")
    line_text = lines[lsp_line_idx]
    if after_text is None:
        stripped = len(line_text.rstrip())
        return (lsp_line_idx, stripped - 1 if stripped > 0 else 0)
    caret = find_after_text_caret(lines, lsp_line_idx, after_text)
    if caret is None:
        raise IsabelleToolError(f"Text '{after_text}' not found on line {line_label}")
    return caret
