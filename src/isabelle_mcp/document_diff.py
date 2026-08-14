"""Ranged didChange support: UTF-16 positions and the minimal line-level diff.

Why this exists (measured 2026-08-14, plan R9): a range-less didChange makes the
server's Text.Edit.replace remove ALL text and insert ALL text -- not a minimal
diff -- so every command in the file is re-created and the whole file re-executes
on ANY edit.  Sending ranged contentChanges restores the evaluated-prefix reuse
(measured: 0.2s instead of a full re-run; an armed breakpoint before the edit
survives).

Two hard facts shape the code:

* LSP character offsets are UTF-16 code units, and the server does Java String
  arithmetic on them.  Astral glyphs can reach ``doc.content`` through the
  unicode guard's warn-only paths, so EVERY position emitted on the wire goes
  through :func:`utf16_position`.

* A newline is a separator, not a terminator (the server's Line.Document model):
  ``text == "\\n".join(lines)``, and a file's trailing newline is the empty last
  element of ``text.split("\\n")``.  A hunk whose old side reaches end-of-text
  therefore anchors at the END of the last kept line -- consuming or supplying
  that line's separating newline -- instead of at the start of a line that may
  not exist.
"""

from __future__ import annotations

import difflib

__all__ = ["utf16_position", "ranged_content_changes"]


def utf16_position(text: str, offset: int) -> dict[str, int]:
    """Map a Python character offset in *text* to an LSP position dict.

    The line is the number of newlines before *offset*; the character is the
    UTF-16 code-unit count from the line start (an astral glyph counts as 2).
    *offset* is clamped into ``[0, len(text)]``.
    """
    offset = max(0, min(offset, len(text)))
    line_start = text.rfind("\n", 0, offset) + 1
    line = text.count("\n", 0, offset)
    character = sum(2 if ord(c) > 0xFFFF else 1 for c in text[line_start:offset])
    return {"line": line, "character": character}


def ranged_content_changes(old: str, new: str) -> list[dict] | None:
    """The minimal line-level diff from *old* to *new* as LSP contentChanges.

    One ranged change per non-equal difflib opcode, in DESCENDING position
    order: the server applies changes sequentially, each against the already-
    edited model, and with the later-in-file hunks applied first every start
    position computed against *old* stays valid.  Returns ``None`` when the
    texts are equal (nothing to send).  All ranges satisfy start <= end; a
    range with start > end would be rejected server-side, and a rejected
    didChange is DROPPED with only a log message.
    """
    if old == new:
        return None

    old_lines = old.split("\n")
    new_lines = new.split("\n")

    # Flat offset of each line start in *old*: line k begins after k separators.
    starts = [0]
    for line in old_lines:
        starts.append(starts[-1] + len(line) + 1)

    changes: list[dict] = []
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        replacement_lines = new_lines[j1:j2]
        if i2 < len(old_lines):
            # Whole-line hunk: [start of line i1, start of line i2), replacement
            # keeps the invariant by carrying one trailing separator per line.
            begin, end = starts[i1], starts[i2]
            text = "".join(line + "\n" for line in replacement_lines)
        elif i1 > 0:
            # The old side reaches end-of-text: anchor at the end of the last
            # kept line, consuming its separating newline; the replacement
            # carries the leading newline back iff it still has lines to add.
            begin, end = starts[i1] - 1, len(old)
            text = "\n" + "\n".join(replacement_lines) if replacement_lines else ""
        else:
            # The whole text is one hunk.
            begin, end = 0, len(old)
            text = "\n".join(replacement_lines)
        changes.append({
            "range": {
                "start": utf16_position(old, begin),
                "end": utf16_position(old, end),
            },
            "text": text,
        })

    changes.reverse()
    return changes
