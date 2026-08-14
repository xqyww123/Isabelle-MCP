"""Unit tests for the ranged-didChange diff (document_diff.py, plan R9).

The property test applies the emitted changes to a Python model of the
server's Line.Document.change semantics: changes are applied SEQUENTIALLY in
the order sent, each against the already-edited text, positions are UTF-16
code units, and a range with start > end (or otherwise unresolvable) is a
REJECTION -- the real server drops the whole didChange with only a log
message, so the model raises instead.
"""

import random

from isabelle_mcp.document_diff import ranged_content_changes, utf16_position

ASTRAL = "\U0001d569"  # counts as 2 UTF-16 code units


# ── the Line.Document.change model ─────────────────────────────────────────

def _flat_offset(text: str, pos: dict) -> int:
    lines = text.split("\n")
    line, want = pos["line"], pos["character"]
    if line >= len(lines):
        raise AssertionError(f"position beyond last line: {pos}")
    offset = sum(len(lines[k]) + 1 for k in range(line))
    units = 0
    for ch in lines[line]:
        if units >= want:
            break
        units += 2 if ord(ch) > 0xFFFF else 1
        offset += 1
    if units != want:
        raise AssertionError(f"character {want} not reachable on line {line}")
    return offset


def _apply_changes(text: str, changes: list[dict]) -> str:
    for change in changes:
        start = _flat_offset(text, change["range"]["start"])
        end = _flat_offset(text, change["range"]["end"])
        if not 0 <= start <= end <= len(text):
            raise AssertionError(f"rejected range: {change['range']}")
        text = text[:start] + change["text"] + text[end:]
    return text


def _roundtrip(old: str, new: str) -> list[dict]:
    changes = ranged_content_changes(old, new)
    assert changes is not None
    assert _apply_changes(old, changes) == new
    return changes


# ── utf16_position ─────────────────────────────────────────────────────────

def test_utf16_position_ascii():
    text = "ab\ncd"
    assert utf16_position(text, 0) == {"line": 0, "character": 0}
    assert utf16_position(text, 2) == {"line": 0, "character": 2}
    assert utf16_position(text, 3) == {"line": 1, "character": 0}
    assert utf16_position(text, 5) == {"line": 1, "character": 2}


def test_utf16_position_astral_counts_two():
    text = f"a{ASTRAL}b\ncd"
    # a=1, astral=2, b=1 -> offset 3 (after b) is character 4
    assert utf16_position(text, 3) == {"line": 0, "character": 4}
    # the next line is unaffected
    assert utf16_position(text, 5) == {"line": 1, "character": 1}


def test_utf16_position_clamps():
    assert utf16_position("ab", 99) == {"line": 0, "character": 2}
    assert utf16_position("ab", -1) == {"line": 0, "character": 0}
    assert utf16_position("", 0) == {"line": 0, "character": 0}


# ── the wire-shape pin ─────────────────────────────────────────────────────

def test_exact_wire_shape_of_a_middle_line_edit():
    # A malformed range object silently decodes as the FULL-DOCUMENT form on
    # the server (lsp.scala), so the emitted JSON shape is pinned exactly.
    changes = ranged_content_changes("a\nb\nc", "a\nX\nc")
    assert changes == [{
        "range": {
            "start": {"line": 1, "character": 0},
            "end": {"line": 2, "character": 0},
        },
        "text": "X\n",
    }]


def test_equal_texts_yield_none():
    assert ranged_content_changes("same\ntext", "same\ntext") is None


# ── hunk shapes ────────────────────────────────────────────────────────────

def test_two_distant_hunks_descend():
    old = "a\nb\nc\nd\ne\nf\ng"
    new = "a\nB\nc\nd\ne\nF\ng"
    changes = _roundtrip(old, new)
    assert len(changes) == 2
    first, second = changes
    assert first["range"]["start"]["line"] == 5   # descending: f-hunk first
    assert second["range"]["start"]["line"] == 1
    assert first["text"] == "F\n" and second["text"] == "B\n"


def test_replace_last_line_anchors_at_previous_line_end():
    changes = _roundtrip("a\nb\nc", "a\nb\nC")
    assert changes == [{
        "range": {
            "start": {"line": 1, "character": 1},
            "end": {"line": 2, "character": 1},
        },
        "text": "\nC",
    }]


def test_delete_last_line_eats_the_separator():
    changes = _roundtrip("a\nb\nc", "a\nb")
    assert changes == [{
        "range": {
            "start": {"line": 1, "character": 1},
            "end": {"line": 2, "character": 1},
        },
        "text": "",
    }]


def test_append_line_without_trailing_newline():
    changes = _roundtrip("a\nb", "a\nb\nc")
    assert changes == [{
        "range": {
            "start": {"line": 1, "character": 1},
            "end": {"line": 1, "character": 1},
        },
        "text": "\nc",
    }]


def test_add_trailing_newline():
    changes = _roundtrip("a\nb", "a\nb\n")
    assert changes[0]["text"] == "\n"


def test_remove_trailing_newline():
    changes = _roundtrip("a\nb\n", "a\nb")
    assert changes == [{
        "range": {
            "start": {"line": 1, "character": 1},
            "end": {"line": 2, "character": 0},
        },
        "text": "",
    }]


def test_insert_and_delete_at_top():
    _roundtrip("b\nc", "a\nb\nc")
    _roundtrip("a\nb\nc", "b\nc")


def test_whole_text_replacement():
    _roundtrip("old", "completely\ndifferent")
    _roundtrip("", "something")
    _roundtrip("something", "")


def test_eof_anchor_counts_utf16_on_the_kept_line():
    old = f"a\nx{ASTRAL}\nzap"
    changes = _roundtrip(old, f"a\nx{ASTRAL}")
    # anchor at the end of the kept line "x<astral>": 1 + 2 = 3 UTF-16 units
    assert changes == [{
        "range": {
            "start": {"line": 1, "character": 3},
            "end": {"line": 2, "character": 3},
        },
        "text": "",
    }]


def test_crlf_is_ordinary_content():
    # CR is just a character of the line; only "\n" separates.
    _roundtrip("a\r\nb\r", "a\r\nB\r")


# ── the property test ──────────────────────────────────────────────────────

def test_property_random_pairs_roundtrip():
    rng = random.Random(20260814)
    alphabet = ["", "a", "bb", "ccc", f"x{ASTRAL}y", "d d", "\r"]

    def random_text() -> str:
        n_lines = rng.randrange(0, 9)
        lines = [rng.choice(alphabet) for _ in range(n_lines)]
        return "\n".join(lines)

    def mutate(text: str) -> str:
        lines = text.split("\n")
        for _ in range(rng.randrange(1, 4)):
            op = rng.randrange(3)
            if op == 0 and lines:
                lines[rng.randrange(len(lines))] = rng.choice(alphabet)
            elif op == 1:
                lines.insert(rng.randrange(len(lines) + 1), rng.choice(alphabet))
            elif op == 2 and lines:
                del lines[rng.randrange(len(lines))]
        return "\n".join(lines)

    for _ in range(500):
        old = random_text()
        new = mutate(old) if rng.random() < 0.8 else random_text()
        changes = ranged_content_changes(old, new)
        if old == new:
            assert changes is None
            continue
        assert changes, f"no changes for {old!r} -> {new!r}"
        assert _apply_changes(old, changes) == new, f"{old!r} -> {new!r}"
