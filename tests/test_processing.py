"""Freshness semantics of ProcessingTracker (post-edit grace window).

Regression tests for the "Evaluation in progress" latch: completion used to
require a decoration push strictly newer than the evaluation start, but the
server never re-sends unchanged decorations, so an already-finished file
latched "in progress" forever. Freshness now recovers by clock
(DECORATION_GRACE after the last didChange we sent), never by waiting for a
push that may legitimately never come.
"""

import asyncio
import time

import pytest

from isabelle_mcp import processing
from isabelle_mcp.processing import ProcessingTracker
from isabelle_mcp.utils import LSPLine


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch):
    monkeypatch.setattr(processing, "DECORATION_GRACE", 0.1)


def _noop_health_check() -> None:
    pass


async def test_not_initialized_blocks_line_reached():
    tracker = ProcessingTracker()
    assert not tracker.line_reached(5)
    assert not tracker.range_processed(LSPLine(0), LSPLine(10))


async def test_first_push_makes_fresh_without_any_send():
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

    tracker.note_doc_update_sent()
    assert not tracker.line_reached(5)  # inside the grace window: cache distrusted

    await asyncio.sleep(processing.DECORATION_GRACE + 0.02)
    assert tracker.line_reached(5)      # no push arrived — fresh again anyway


async def test_push_inside_grace_does_not_unlock_early():
    """A push landing right after our didChange may still describe the pre-edit
    document (in flight when we sent); only the clock ends the grace window."""
    tracker = ProcessingTracker()
    await tracker.update({"background_unprocessed1": []})

    tracker.note_doc_update_sent()
    await tracker.update({"background_unprocessed1": []})
    assert not tracker.line_reached(5)

    await asyncio.sleep(processing.DECORATION_GRACE + 0.02)
    assert tracker.line_reached(5)


async def test_bounded_wait_wakes_when_grace_elapses():
    """Without the grace-aware wait slice the condition loop would sleep a full
    check_interval past the recovery point (no push ever notifies it)."""
    tracker = ProcessingTracker()
    await tracker.update({"background_unprocessed1": [], "background_running1": []})
    tracker.note_doc_update_sent()

    start = time.monotonic()
    ok = await tracker.wait_until_processed_bounded(
        LSPLine(0), LSPLine(10),
        timeout=5.0, health_check=_noop_health_check, check_interval=5.0,
    )
    elapsed = time.monotonic() - start

    assert ok
    assert elapsed < 1.0  # woke on grace expiry, not the 5s check_interval


async def test_reset_clears_grace_stamp():
    tracker = ProcessingTracker()
    await tracker.update({"background_unprocessed1": []})
    tracker.note_doc_update_sent()
    await tracker.reset()
    assert not tracker.line_reached(5)  # uninitialized again
    await tracker.update({"background_unprocessed1": []})
    assert tracker.line_reached(5)      # fresh immediately: stamp was cleared
