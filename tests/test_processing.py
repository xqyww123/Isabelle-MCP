"""Freshness semantics of ProcessingTracker (global post-edit grace window).

Regression tests for the "Evaluation in progress" latch: completion used to
require a decoration push strictly newer than the evaluation start, but the
server never re-sends unchanged decorations, so an already-finished file
latched "in progress" forever. Freshness now recovers by clock
(DECORATION_GRACE after the last edit-send, recorded globally by
note_edit_sent), never by waiting for a push that may legitimately never come.
"""

import asyncio
import time

import pytest

from isabelle_mcp import processing
from isabelle_mcp.processing import ProcessingTracker, note_edit_sent
from isabelle_mcp.utils import LSPLine

# 0.4s grace: wide enough that the "inside the window" asserts cannot be
# outrun by a loaded CI runner, small enough to keep the suite fast.
_GRACE = 0.4


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch):
    monkeypatch.setattr(processing, "DECORATION_GRACE", _GRACE)
    monkeypatch.setattr(processing, "_last_edit_sent", float("-inf"))


def _noop_health_check() -> None:
    pass


async def test_not_initialized_blocks_line_reached():
    tracker = ProcessingTracker()
    assert not tracker.line_reached(5)
    assert not tracker.range_processed(LSPLine(0), LSPLine(10))


async def test_first_push_makes_fresh_without_any_edit():
    tracker = ProcessingTracker()
    await tracker.update({"background_unprocessed1": [], "background_running1": []})
    assert tracker.line_reached(5)
    assert tracker.all_processed


async def test_unprocessed_range_blocks_line_reached_when_fresh():
    tracker = ProcessingTracker()
    await tracker.update({"background_unprocessed1": [(3, 0, 7, 0)]})
    assert not tracker.line_reached(5)
    assert tracker.line_reached(10)


async def test_grace_recovers_without_a_push():
    """The latch regression: an edit whose decorations do not change produces
    no push; freshness must come back by clock alone."""
    tracker = ProcessingTracker()
    await tracker.update({"background_unprocessed1": [], "background_running1": []})

    note_edit_sent()
    assert not tracker.line_reached(5)  # inside the grace window: cache distrusted

    await asyncio.sleep(_GRACE + 0.05)
    assert tracker.line_reached(5)      # no push arrived — fresh again anyway


async def test_edit_grace_is_global_across_trackers():
    """One edit anywhere distrusts EVERY tracker: PIDE invalidation propagates
    across imports, so editing A must also gate B's cached decorations."""
    a, b = ProcessingTracker(), ProcessingTracker()
    await a.update({"background_unprocessed1": []})
    await b.update({"background_unprocessed1": []})

    note_edit_sent()
    assert not a.line_reached(5)
    assert not b.line_reached(5)

    await asyncio.sleep(_GRACE + 0.05)
    assert a.line_reached(5)
    assert b.line_reached(5)


async def test_push_inside_grace_does_not_unlock_early_but_is_honored():
    """A push landing right after an edit may still describe the pre-edit
    document (in flight when we sent), so only the clock ends the grace window
    — but its CONTENT must be merged and honored once the window elapses."""
    tracker = ProcessingTracker()
    await tracker.update({"background_unprocessed1": []})

    note_edit_sent()
    await tracker.update({"background_unprocessed1": [(3, 0, 7, 0)]})
    assert not tracker.line_reached(5)
    assert not tracker.line_reached(10)  # not because of ranges — window still open

    await asyncio.sleep(_GRACE + 0.05)
    assert not tracker.line_reached(5)   # in-grace push content survived
    assert tracker.line_reached(10)


async def test_bounded_wait_wakes_when_grace_elapses():
    """Without the grace-aware wait slice the condition loop would sleep a full
    check_interval past the recovery point (no push ever notifies it)."""
    tracker = ProcessingTracker()
    await tracker.update({"background_unprocessed1": [], "background_running1": []})
    # start BEFORE the stamp: the wake cannot precede stamp+grace >= start+grace,
    # so the lower-bound assert below is deterministic (no scheduling margin).
    start = time.monotonic()
    note_edit_sent()

    ok = await tracker.wait_until_processed_bounded(
        LSPLine(0), LSPLine(10),
        timeout=5.0, health_check=_noop_health_check, check_interval=5.0,
    )
    elapsed = time.monotonic() - start

    assert ok
    # Lower bound: it actually waited out the grace window (a no-op stamp
    # would return instantly and silently void this test).
    assert elapsed >= _GRACE
    # Upper bound: it woke on grace expiry, not the 5s check_interval.
    assert elapsed < _GRACE + 2.0


async def test_line_reached_wait_returns_despite_trailing_run():
    """wait_until_line_reached_bounded keys on the FRONTIER, not prefix-quiet: it
    returns as soon as the dest line leaves unprocessed, even with an earlier
    command still running (so the eval loop can decide complete vs in_progress)."""
    tracker = ProcessingTracker()
    await tracker.update({
        "background_unprocessed1": [],
        "background_running1": [(7, 0, 7, 0)],
    })
    ok = await tracker.wait_until_line_reached_bounded(
        LSPLine(9), timeout=5.0, health_check=_noop_health_check,
    )
    assert ok
    # The prefix is NOT quiet — line 7 is still running — yet the wait returned.
    assert not tracker.range_processed(LSPLine(0), LSPLine(9))


async def test_line_reached_wait_times_out_when_unreached():
    tracker = ProcessingTracker()
    await tracker.update({"background_unprocessed1": [(3, 0, 9, 0)]})
    ok = await tracker.wait_until_line_reached_bounded(
        LSPLine(5), timeout=0.1, health_check=_noop_health_check,
    )
    assert not ok


async def test_reset_keeps_global_grace():
    """reset() clears per-file decoration state; the global edit clock is not
    per-file state and must survive (the edit still happened)."""
    tracker = ProcessingTracker()
    await tracker.update({"background_unprocessed1": []})
    note_edit_sent()
    await tracker.reset()
    assert not tracker.line_reached(5)  # uninitialized again
    await tracker.update({"background_unprocessed1": []})
    assert not tracker.line_reached(5)  # still inside the global grace window
    await asyncio.sleep(_GRACE + 0.05)
    assert tracker.line_reached(5)


def test_read_grace_env_parsing(monkeypatch):
    """Invalid env falls back to the default with a warning (no import crash)."""
    monkeypatch.setenv("ISABELLE_MCP_DECORATION_GRACE", "not-a-number")
    assert processing._read_grace() == 2.0
    monkeypatch.setenv("ISABELLE_MCP_DECORATION_GRACE", "0.7")
    assert processing._read_grace() == 0.7
    monkeypatch.delenv("ISABELLE_MCP_DECORATION_GRACE")
    assert processing._read_grace() == 2.0


# --------------------------------------------------------------------------
# position_state — the definite answer line_reached cannot give
# --------------------------------------------------------------------------

async def _tracker(**ranges) -> ProcessingTracker:
    tracker = ProcessingTracker()
    await tracker.update({
        "background_unprocessed1": ranges.get("unprocessed", []),
        "background_running1": ranges.get("running", []),
        "background_canceled": ranges.get("canceled", []),
    })
    return tracker


async def test_position_state_never_evaluated_tracker():
    # No decoration has ever arrived: nothing is processed, which is what the
    # caller must act on — not "unknown".
    assert ProcessingTracker().position_state(5) == processing.NOT_EVALUATED


async def test_position_state_reports_each_decoration():
    t = await _tracker(unprocessed=[(3, 0, 7, 0)])
    assert t.position_state(5) == processing.NOT_EVALUATED
    assert t.position_state(9) == processing.PROCESSED

    t = await _tracker(running=[(3, 0, 7, 0)])
    assert t.position_state(5) == processing.RUNNING

    t = await _tracker(canceled=[(3, 0, 7, 0)])
    assert t.position_state(5) == processing.CANCELLED


async def test_position_state_follows_isabelle_precedence():
    # One line covering several commands: the least-finished one names the line,
    # which is the order rendering.scala:515-518 itself uses.
    t = await _tracker(
        unprocessed=[(5, 0, 5, 0)], running=[(5, 0, 5, 0)], canceled=[(5, 0, 5, 0)],
    )
    assert t.position_state(5) == processing.NOT_EVALUATED
    t = await _tracker(running=[(5, 0, 5, 0)], canceled=[(5, 0, 5, 0)])
    assert t.position_state(5) == processing.RUNNING


async def test_position_state_is_unknown_inside_the_grace_window():
    t = await _tracker()
    assert t.position_state(5) == processing.PROCESSED
    note_edit_sent()
    # The cache may still describe the pre-edit document, and nothing covers the
    # line to prove otherwise.
    assert t.position_state(5) == processing.UNKNOWN
    await asyncio.sleep(_GRACE + 0.05)
    assert t.position_state(5) == processing.PROCESSED


async def test_position_state_stays_definite_inside_the_grace_window():
    # A stale cache can only over-report work as unfinished, so a range covering
    # the line is still trustworthy — only its ABSENCE is not.
    t = await _tracker(unprocessed=[(5, 0, 5, 0)])
    note_edit_sent()
    assert t.position_state(5) == processing.NOT_EVALUATED


async def test_canceled_decoration_is_no_longer_discarded():
    # background_canceled used to be absent from _TRACKED_TYPES, so an
    # interrupted command answered "processed".
    parsed = processing.parse_decoration_ranges([
        {"type": "background_canceled", "content": [{"range": [4, 0, 4, 9]}]},
    ])
    assert parsed == {"background_canceled": [(4, 0, 4, 9)]}


# --------------------------------------------------------------------------
# range_state — the same answer for a whole command, plus how long it has run
# --------------------------------------------------------------------------

async def test_range_state_is_the_span_generalisation_of_position_state():
    t = await _tracker(unprocessed=[(10, 0, 12, 0)])
    # A command spanning 8-11 overlaps the unprocessed span, so it is not
    # evaluated, even though its first line is clear.
    assert t.range_state(8, 11)[0] == processing.NOT_EVALUATED
    assert t.position_state(8) == processing.PROCESSED
    assert t.range_state(0, 3)[0] == processing.PROCESSED


async def test_range_state_reports_how_long_a_command_has_been_running():
    t = await _tracker(running=[(5, 0, 5, 0)])
    state, elapsed = t.range_state(5, 5)
    assert state == processing.RUNNING
    # The onset is stamped when the decoration arrives, so this is small but real.
    assert elapsed >= 0.0
    # Every other state carries no duration.
    assert t.range_state(9, 9) == (processing.PROCESSED, 0.0)
