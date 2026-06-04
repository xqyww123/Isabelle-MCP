"""Self-contained Isabelle symbol handling.

Vendored from ``Isabelle_RPC_Host`` (``symbol_explode`` and the ASCII/Unicode
conversion) so the MCP has no runtime dependency on a separate package nor on
shelling out to ``isabelle getenv``.

The symbol table (needed only by :func:`ascii_of_unicode`) is normally seeded
once from the patched ``vscode_server`` via :func:`set_symbols_text` — the MCP
fetches it over the ``PIDE/symbols`` request. If it is never seeded (e.g. a
stock, unpatched server), the first conversion falls back to reading the files
named by ``isabelle getenv ISABELLE_SYMBOLS``.

``symbol_explode`` is purely static (no table) and always available.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


# ── symbol_explode (static port of Pure/General/symbol_explode.ML) ─────────


def symbol_explode(text: str) -> list[str]:
    """Split a string into Isabelle symbols.

    Handles CR normalization (``\\r\\n`` / ``\\r`` -> ``\\n``), named symbols
    ``\\<name>`` / ``\\<^name>`` as single symbols, and every other character
    as its own symbol. Python strings are already decoded Unicode, so glyphs
    like ``α`` / ``⇒`` are single characters and need no special handling.
    """
    result: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        # CR normalization: \r\n -> \n, bare \r -> \n
        if ch == '\r':
            result.append('\n')
            if i + 1 < n and text[i + 1] == '\n':
                i += 2
            else:
                i += 1
        # Named symbol: \<...>
        elif ch == '\\' and i + 1 < n and text[i + 1] == '<':
            j = i + 2
            # optional ^ for control symbols
            if j < n and text[j] == '^':
                j += 1
            # ASCII identifier
            if j < n and text[j].isascii() and text[j].isalpha():
                j += 1
                while j < n and (text[j].isascii() and (text[j].isalnum() or text[j] in "_'")):
                    j += 1
            # optional closing >
            if j < n and text[j] == '>':
                j += 1
            result.append(text[i:j])
            i = j
        # Single character (includes decoded Unicode like α, ⇒, etc.)
        else:
            result.append(ch)
            i += 1
    return result


# ── Symbol table (ASCII name -> Unicode char), parsed from etc/symbols ─────


def _parse_symbols(text: str) -> dict[str, str]:
    """Parse Isabelle ``etc/symbols`` text into {ASCII symbol -> unicode char}.

    Each declaration looks like::

        \\<odiv>   code: 0x002A38   font: PhiSymbols   group: operator

    Lines without a ``code:`` entry (markup-only symbols) are skipped, since
    they have no unicode rendering and so play no part in the conversion.
    """
    symbols: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        symbol = parts[0]

        code_point = None
        for i, part in enumerate(parts[1:], 1):
            if part.startswith('code:'):
                if len(part) > 5:  # "code:0x..." with no space
                    code_point = part.split(':', 1)[1].strip()
                elif i < len(parts) - 1:  # "code:" then the hex in the next part
                    code_point = parts[i + 1].strip()
                break

        if symbol and code_point:
            try:
                symbols[symbol] = chr(int(code_point, 16))
            except ValueError:
                continue
    return symbols


# Cache: (symbols, reverse_symbols, translate_table). None until first use.
_CACHE: tuple[dict[str, str], dict[str, str], dict[int, str]] | None = None


def _build_cache(symbols: dict[str, str]) -> tuple[dict[str, str], dict[str, str], dict[int, str]]:
    reverse = {uni: sym for sym, uni in symbols.items()}
    return (symbols, reverse, str.maketrans(reverse))


def set_symbols_text(text: str) -> None:
    """Seed the symbol table from ``etc/symbols`` text (e.g. from PIDE/symbols).

    Replaces any previously loaded table and disables the ``isabelle getenv``
    fallback for subsequent conversions.
    """
    global _CACHE
    _CACHE = _build_cache(_parse_symbols(text))
    logger.debug("Seeded symbol table: %d symbols", len(_CACHE[0]))


def _read_env_symbols_text() -> str:
    """Fallback: read the files named by ``isabelle getenv ISABELLE_SYMBOLS``."""
    home = os.popen("isabelle getenv -b ISABELLE_HOME").read().strip()
    home_user = os.popen("isabelle getenv -b ISABELLE_HOME_USER").read().strip()
    chunks: list[str] = []
    for path in (f"{home}/etc/symbols", f"{home_user}/etc/symbols"):
        if path and os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                chunks.append(f.read())
    return "\n".join(chunks)


def _ensure_cache() -> tuple[dict[str, str], dict[str, str], dict[int, str]]:
    global _CACHE
    if _CACHE is None:
        logger.warning(
            "Symbol table not seeded from PIDE/symbols; "
            "falling back to 'isabelle getenv ISABELLE_SYMBOLS'."
        )
        _CACHE = _build_cache(_parse_symbols(_read_env_symbols_text()))
    return _CACHE


def get_symbols() -> dict[str, str]:
    """Return the {ASCII symbol -> unicode char} table (seeding if needed)."""
    return _ensure_cache()[0]


# Sub-/superscript and bold control sequences map to standalone Unicode glyphs
# in the rendered text; the reverse table restores them to Isabelle's notation.
_SUBSUP_RESTORE_TABLE = {
    "₀": "⇩0", "₁": "⇩1", "₂": "⇩2", "₃": "⇩3", "₄": "⇩4",
    "₅": "⇩5", "₆": "⇩6", "₇": "⇩7", "₈": "⇩8", "₉": "⇩9",
    "ₐ": "⇩a", "ₑ": "⇩e", "ₕ": "⇩h", "ᵢ": "⇩i", "ⱼ": "⇩j", "ₖ": "⇩k", "ₗ": "⇩l",
    "ₘ": "⇩m", "ₙ": "⇩n", "ₒ": "⇩o", "ₚ": "⇩p", "ᵣ": "⇩r", "ₛ": "⇩s", "ₜ": "⇩t",
    "ᵤ": "⇩u", "ᵥ": "⇩v", "ₓ": "⇩x",
    "⁰": "⇧0", "¹": "⇧1", "²": "⇧2", "³": "⇧3", "⁴": "⇧4",
    "⁵": "⇧5", "⁶": "⇧6", "⁷": "⇧7", "⁸": "⇧8", "⁹": "⇧9",
    "ᴬ": "⇧A", "ᴮ": "⇧B", "ᴰ": "⇧D", "ᴱ": "⇧E", "ᴳ": "⇧G", "ᴴ": "⇧H", "ᴵ": "⇧I",
    "ᴶ": "⇧J", "ᴷ": "⇧K", "ᴸ": "⇧L", "ᴹ": "⇧M", "ᴺ": "⇧N", "ᴼ": "⇧O", "ᴾ": "⇧P",
    "ᴿ": "⇧R", "ᵀ": "⇧T", "ᵁ": "⇧U", "ⱽ": "⇧V", "ᵂ": "⇧W",
    "ᵃ": "⇧a", "ᵇ": "⇧b", "ᶜ": "⇧c", "ᵈ": "⇧d", "ᵉ": "⇧e", "ᶠ": "⇧f",
    "ᵍ": "⇧g", "ʰ": "⇧h", "ⁱ": "⇧i", "ʲ": "⇧j", "ᵏ": "⇧k", "ˡ": "⇧l",
    "ᵐ": "⇧m", "ⁿ": "⇧n", "ᵒ": "⇧o", "ᵖ": "⇧p", "ˢ": "⇧s", "ᵗ": "⇧t",
    "ᵘ": "⇧u", "ᵛ": "⇧v", "ʷ": "⇧w", "ˣ": "⇧x", "ʸ": "⇧y", "ᶻ": "⇧z",
    "₋": "⇩-", "⁻": "⇧-", "₊": "⇩+", "⁺": "⇧+", "₌": "⇩=", "⁼": "⇧=",
    "₍": "⇩(", "⁽": "⇧(", "₎": "⇩)", "⁾": "⇧)",
    "𝐚": "❙a", "𝐛": "❙b", "𝐜": "❙c", "𝐝": "❙d", "𝐞": "❙e", "𝐟": "❙f",
    "𝐠": "❙g", "𝐡": "❙h", "𝐢": "❙i", "𝐣": "❙j", "𝐤": "❙k", "𝐥": "❙l",
    "𝐦": "❙m", "𝐧": "❙n", "𝐨": "❙o", "𝐩": "❙p", "𝐪": "❙q", "𝐫": "❙r",
    "𝐬": "❙s", "𝐭": "❙t", "𝐮": "❙u", "𝐯": "❙v", "𝐰": "❙w", "𝐱": "❙x",
    "𝐲": "❙y", "𝐳": "❙z",
    "𝐀": "❙A", "𝐁": "❙B", "𝐂": "❙C", "𝐃": "❙D", "𝐄": "❙E", "𝐅": "❙F",
    "𝐆": "❙G", "𝐇": "❙H", "𝐈": "❙I", "𝐉": "❙J", "𝐊": "❙K", "𝐋": "❙L",
    "𝐌": "❙M", "𝐍": "❙N", "𝐎": "❙O", "𝐏": "❙P", "𝐐": "❙Q", "𝐑": "❙R",
    "𝐒": "❙S", "𝐓": "❙T", "𝐔": "❙U", "𝐕": "❙V", "𝐖": "❙W", "𝐗": "❙X",
    "𝐘": "❙Y", "𝐙": "❙Z",
}
_SUBSUP_RESTORE_TRANS = str.maketrans(_SUBSUP_RESTORE_TABLE)


def ascii_of_unicode(src: str) -> str:
    """Convert a Unicode string to Isabelle's ASCII notation (``\\<...>``).

    Inverse of Isabelle's symbol decoding: every recognised Unicode glyph is
    mapped back to its ``\\<name>`` form, and sub/superscript glyphs to their
    ``⇩``/``⇧`` control sequences.
    """
    trans_table = _ensure_cache()[2]
    return src.translate(_SUBSUP_RESTORE_TRANS).translate(trans_table)
