"""Guard: files pushed to Isabelle must be written in Isabelle ASCII.

Called at the disk-read points of the MCP push paths (``open_document`` /
``sync_dirty_files`` — the event-driven watcher sink and the tool-call stat
backstop both funnel into the latter). Policy is ASCII-or-nothing:

- Pure-ASCII content passes through untouched.
- When converting every recognised glyph (``α`` → ``\\<alpha>``, plus a leading
  UTF-8 BOM strip) yields a **fully ASCII** result, the file is rewritten on
  disk via compare-and-replace, so disk, document model, and prover stay
  byte-identical (column positions included).
- When non-ASCII remains after conversion (no symbol-table entry — e.g. CJK
  comments — or a defective symbol table), the file is NOT rewritten and the
  original on-disk content is pushed (the vscode_server encodes glyphs for the
  prover itself); only a warning is queued. Never writing a non-ASCII result
  also makes a rewrite feedback loop impossible by construction.

Each event queues a warning bullet; the server middleware drains the queue and
appends it to the next tool response, instructing the agent to emit Isabelle
ASCII directly. Warn-only (not-rewritten) bullets are deduplicated per file
until its set of non-ASCII characters changes.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections import Counter

from isabelle_mcp.utils.isabelle_symbols import ascii_of_unicode

logger = logging.getLogger(__name__)

# path -> warning bullet for the next tool response (latest event wins per path).
_pending: dict[str, str] = {}

# path -> the set of non-ASCII chars last reported for a NOT-rewritten file;
# warn-only bullets are suppressed until this set changes (anti-nag).
_last_nonascii_sig: dict[str, frozenset[str]] = {}

_AGENT_INSTRUCTION = (
    "You MUST write Isabelle ASCII notation in .thy/.ML files — e.g. "
    "\\<alpha>, \\<Rightarrow>, \\<forall>, \\<^sub>1 — NEVER the Unicode "
    "glyphs (α, ⇒, ∀, ₁). Any file marked REWRITTEN above changed on disk: "
    "re-read it before further edits so your edits match the new content."
)

# Caps so a glyph-heavy file cannot flood the response.
_MAX_REPLACEMENTS_SHOWN = 10
_MAX_LEFTOVER_LINES_SHOWN = 5
_MAX_GLYPHS_PER_LINE = 5

# Re-reads when an external write lands between our read and the replace.
_RACE_RETRIES = 3


def sanitize_read(path: str) -> tuple[str, str | None]:
    """Read *path* and return ``(text_to_push, warning_bullet | None)``.

    Blocking (file I/O, possibly an atomic rewrite) — call via
    ``asyncio.to_thread``. The returned text always matches what is on disk
    after the call, so stat signatures taken afterwards stay coherent. Queue
    the bullet with :func:`record_warning` from the event loop. Raises OSError
    when the initial read fails (caller handles it as before).
    """
    content = ""
    for _ in range(_RACE_RETRIES):
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if content.isascii():
            _last_nonascii_sig.pop(path, None)
            return content, None

        stripped = content.removeprefix("\ufeff")
        bom_stripped = len(stripped) != len(content)
        converted = ascii_of_unicode(stripped)
        replaced = _replaced_glyphs(stripped)

        if not converted.isascii():
            # ASCII-or-nothing: never write a partially converted file. Push
            # the original so disk and document model stay identical.
            return content, _warn_only_bullet(path, content, converted, replaced, bom_stripped)

        try:
            if _replace_if_unchanged(path, converted, expected=content):
                _last_nonascii_sig.pop(path, None)
                logger.info(
                    "Rewrote %s in Isabelle ASCII (%d glyph(s) converted)",
                    path, sum(replaced.values()),
                )
                return converted, _rewritten_bullet(path, replaced, bom_stripped)
        except OSError as e:
            logger.warning("Unicode->ASCII write-back failed for %s: %s", path, e)
            return content, _write_failed_bullet(path, replaced, e)
        # Raced with an external write: loop re-reads the fresh content.

    logger.info("%s kept changing during ASCII conversion; skipped this round", path)
    return content, None


def record_warning(path: str, bullet: str) -> None:
    """Queue *bullet* for the next tool response (call from the event loop)."""
    _pending[path] = bullet


def drain_warnings() -> str | None:
    """Return the queued warning text (and clear the queue), or None if empty."""
    if not _pending:
        return None
    bullets = list(_pending.values())
    _pending.clear()
    return (
        "⚠️ NON-ASCII DETECTED IN ISABELLE FILES\n"
        + "\n".join(bullets)
        + "\n" + _AGENT_INSTRUCTION
    )


def _replace_if_unchanged(path: str, new_text: str, expected: str) -> bool:
    """Atomically replace *path* with *new_text* unless its content changed.

    Same-directory tempfile + ``os.replace`` (readers never see partial
    content; the ``.tmp`` suffix keeps the tempfile outside the watcher's
    extension filter, and the final rename surfaces as on_moved whose re-sync
    no-ops). Before the rename the file is re-read and compared to *expected*
    — a concurrent external write aborts the replace (returns False) instead
    of being clobbered, the same modified-since-read fence editors use. The
    residual window between the compare and the rename is microseconds.
    """
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".isabelle-mcp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        with open(path, encoding="utf-8") as f:
            if f.read() != expected:
                os.unlink(tmp)
                return False
        try:
            os.chmod(tmp, os.stat(path).st_mode)
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replaced_glyphs(content: str) -> Counter[str]:
    """Count the non-ASCII glyphs in *content* that the symbol table converts.

    Both translation tables in :func:`ascii_of_unicode` work character by
    character, so per-glyph conversion agrees with full-text conversion.
    """
    counts: Counter[str] = Counter(ch for ch in content if not ch.isascii())
    return Counter({
        ch: n for ch, n in counts.items() if ascii_of_unicode(ch).isascii()
    })


def _non_ascii_lines(converted: str) -> list[tuple[int, str]]:
    """(1-indexed line, capped glyph sample) for lines still non-ASCII after
    conversion. The glyph sample is neutrally framed and capped by the caller's
    formatting so echoed file content cannot masquerade as server text.
    """
    result: list[tuple[int, str]] = []
    for lineno, line in enumerate(converted.split("\n"), 1):
        if not line.isascii():
            glyphs = list(dict.fromkeys(ch for ch in line if not ch.isascii()))
            sample = "".join(glyphs[:_MAX_GLYPHS_PER_LINE])
            if len(glyphs) > _MAX_GLYPHS_PER_LINE:
                sample += "…"
            result.append((lineno, sample))
    return result


def _format_replacements(replaced: Counter[str]) -> str:
    shown = replaced.most_common(_MAX_REPLACEMENTS_SHOWN)
    items = ", ".join(f"{ch}→{ascii_of_unicode(ch)} (×{n})" for ch, n in shown)
    if len(replaced) > _MAX_REPLACEMENTS_SHOWN:
        items += f", … {len(replaced) - _MAX_REPLACEMENTS_SHOWN} more"
    return items


def _rewritten_bullet(path: str, replaced: Counter[str], bom_stripped: bool) -> str:
    parts = [f"converted {_format_replacements(replaced)}"]
    if bom_stripped:
        parts.append("stripped a UTF-8 BOM")
    return f"- {path}: " + "; ".join(parts) + " — file REWRITTEN on disk"


def _warn_only_bullet(
    path: str,
    original: str,
    converted: str,
    replaced: Counter[str],
    bom_stripped: bool,
) -> str | None:
    """Bullet for a file left untouched; deduplicated until its glyph set changes."""
    sig = frozenset(ch for ch in original if not ch.isascii())
    if _last_nonascii_sig.get(path) == sig:
        return None
    _last_nonascii_sig[path] = sig

    leftover_lines = _non_ascii_lines(converted)
    shown = leftover_lines[:_MAX_LEFTOVER_LINES_SHOWN]
    locs = ", ".join(f"line {ln} (characters: {sample})" for ln, sample in shown)
    if len(leftover_lines) > _MAX_LEFTOVER_LINES_SHOWN:
        locs += f", … {len(leftover_lines) - _MAX_LEFTOVER_LINES_SHOWN} more lines"
    parts = [f"NOT rewritten — non-ASCII with no Isabelle ASCII form at {locs}"]
    if replaced:
        parts.append(
            f"convertible glyphs also left in place: {_format_replacements(replaced)}"
            " — replace these with their ASCII forms yourself"
        )
    if bom_stripped:
        parts.append("file starts with a UTF-8 BOM")
    return f"- {path}: " + "; ".join(parts)


def _write_failed_bullet(path: str, replaced: Counter[str], error: OSError) -> str:
    return (
        f"- {path}: conversion needed ({_format_replacements(replaced)}) but the "
        f"disk write-back FAILED ({error}); file unchanged on disk — rewrite it "
        "in Isabelle ASCII yourself"
    )
