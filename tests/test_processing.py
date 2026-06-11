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
