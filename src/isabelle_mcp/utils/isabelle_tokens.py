"""Isabelle tokenizer and symbol occurrence finder.

Implements a simplified Isabelle lexer based on symbol_pos.ML and lexicon.ML,
operating in ASCII space via Isabelle_RPC_Host.
"""

import logging

from Isabelle_RPC_Host.position import symbol_explode
from Isabelle_RPC_Host.unicode import ascii_of_unicode

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
