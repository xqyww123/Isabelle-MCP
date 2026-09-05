"""Freshness semantics of ProcessingTracker: the picture stamp against the
newest document version and the unflushed-content counters
(ISABELLE_MCP_DECORATION_VERSION_STAMP_PLAN.md, section 3.2 (c)).

A picture is trusted iff it is a FULL picture stamped at least as new as the
newest version the client has seen, read while no content the client wrote is
still uncovered by a flush reply. No clock: freshness is regained by a push
(an acknowledgement push included) or a flush reply, never by waiting.
"""

import asyncio

import pytest

from isabelle_mcp import processing
from isabelle_mcp.processing import (
    FreshnessState,
    ProcessingTracker,
    at_least_as_new,
    is_full_decoration_push,
    newer_of,
)
from isabelle_mcp.utils import LSPLine
from tests.conftest import full_decoration_entries

# Two real document versions: ids tick DOWNWARD, so -40 is newer than -19.
OLDER = -19
NEWER = -40


def _noop_health_check() -> None:
    pass


def _full(**content) -> dict:
    return processing.parse_decoration_ranges(full_decoration_entries(**content))


def _partial(**content) -> dict:
    return processing.parse_decoration_ranges([
        {"type": typ, "content": [{"range": list(r)} for r in ranges]}
        for typ, ranges in content.items()
    ])


async def _tracker(state: FreshnessState | None = None, **ranges) -> ProcessingTracker:
    """A tracker holding a full picture at the state's newest version."""
    state = state or FreshnessState()
    tracker = ProcessingTracker(state)
    await tracker.update(_full(
        background_unprocessed1=ranges.get("unprocessed", []),
        background_running1=ranges.get("running", []),
        background_canceled=ranges.get("canceled", []),
    ), state.newest_document_version)
    return tracker


# --------------------------------------------------------------------------
# The predicate pair (I-7): the only place the direction of "newer" is written
# --------------------------------------------------------------------------

def test_two_real_ids_compare_in_the_right_direction():
    # Mutation control M-7: flip the comparison and both lines red.
    assert at_least_as_new(NEWER, OLDER)
    assert not at_least_as_new(OLDER, NEWER)
    assert at_least_as_new(OLDER, OLDER)
    assert newer_of(OLDER, NEWER) == NEWER
    # 0 is Version.init: older than everything, at least as new as itself only.
    assert at_least_as_new(OLDER, 0) and not at_least_as_new(0, OLDER)


def test_the_freshness_state_folds_stamps_into_the_newest_version():
    state = FreshnessState()
    assert state.newest_document_version == 0
    state.advance(OLDER)
    state.advance(NEWER)
    state.advance(OLDER)          # an older stamp never moves it back
    assert state.newest_document_version == NEWER
    state.reset()
    assert state.newest_document_version == 0


# --------------------------------------------------------------------------
# The trust rule (I-4): fresh = full picture, stamp at least as new, no unflushed content
# --------------------------------------------------------------------------

async def test_not_initialized_blocks_line_reached():
    tracker = ProcessingTracker(FreshnessState())
    assert not tracker.line_reached(5)
    assert not tracker.range_processed(LSPLine(0), LSPLine(10))


async def test_first_full_push_makes_fresh():
    tracker = await _tracker()
    assert tracker.line_reached(5)
    assert tracker.all_processed
    assert tracker.fresh


async def test_unprocessed_range_blocks_line_reached_when_fresh():
    tracker = await _tracker(unprocessed=[(3, 0, 7, 0)])
    assert not tracker.line_reached(5)
    assert tracker.line_reached(10)


async def test_a_push_stamped_older_than_the_newest_version_never_makes_fresh():
    # The client saw NEWER (a flush reply, say); a picture at OLDER describes
    # an older document state and is not trusted, however complete it is.
    state = FreshnessState()
    state.advance(NEWER)
    tracker = ProcessingTracker(state)
    await tracker.update(_full(), OLDER)
    assert tracker.initialized and not tracker.fresh
    assert not tracker.line_reached(5)
    assert tracker.position_state(5) == processing.UNKNOWN
    # The push at the newest version (or newer) makes it fresh.
    await tracker.update(_full(), NEWER)
    assert tracker.fresh and tracker.line_reached(5)


async def test_a_newer_version_seen_elsewhere_unfreshens_every_tracker():
    # The newest version is client-wide: a flush reply naming NEWER makes
    # every picture stamped OLDER unfresh, whatever file it belongs to.
    state = FreshnessState()
    state.advance(OLDER)
    a, b = await _tracker(state), await _tracker(state)
    assert a.fresh and b.fresh
    state.advance(NEWER)
    assert not a.fresh and not b.fresh


async def test_an_acknowledgement_push_freshens_without_content():
    state = FreshnessState()
    state.advance(OLDER)
    tracker = await _tracker(state, unprocessed=[(3, 0, 7, 0)])
    state.advance(NEWER)
    assert not tracker.fresh
    await tracker.update({}, NEWER)          # empty entries, a stamp
    assert tracker.fresh
    # The content survived: an ack says "nothing changed", not "nothing".
    assert not tracker.line_reached(5) and tracker.line_reached(10)


async def test_unflushed_content_blocks_freshness_until_the_reply():
    state = FreshnessState()
    tracker = await _tracker(state)
    assert tracker.fresh
    state.content_sends += 1                 # a didChange went out
    assert state.unflushed_content and not tracker.fresh
    assert tracker.position_state(5) == processing.UNKNOWN
    state.content_sends_flushed = max(state.content_sends_flushed, 1)   # the reply
    assert tracker.fresh


async def test_a_tracker_without_freshness_state_is_never_fresh():
    # The null-object sites (debugger.py, tools/command_status.py): a tracker
    # that stands in for "no picture" cannot fold a push and answers
    # not_evaluated everywhere.
    tracker = ProcessingTracker()
    assert not tracker.initialized and not tracker.fresh
    assert tracker.position_state(5) == processing.NOT_EVALUATED
    with pytest.raises(AssertionError):
        await tracker.update(_full(), OLDER)


# --------------------------------------------------------------------------
# I-5: only a FULL push initializes, whatever the call site does
# --------------------------------------------------------------------------

async def test_a_differential_push_never_initializes():
    # Mutation control M-8: drop the full-push rule from update and this reds.
    state = FreshnessState()
    tracker = ProcessingTracker(state)
    await tracker.update(_partial(background_sorry=[(4, 2, 4, 7)]), OLDER)
    assert not tracker.initialized and not tracker.fresh
    assert tracker.get_sorry_ranges() == [(4, 2, 4, 7)]     # folded, not served
    assert tracker.position_state(4) == processing.NOT_EVALUATED
    await tracker.update({}, OLDER)                          # an ack initializes nothing
    assert not tracker.initialized
    await tracker.update(_full(), OLDER)                     # the full push does
    assert tracker.initialized and tracker.fresh
    assert tracker.get_sorry_ranges() == []                  # overwritten whole


async def test_a_differential_push_updates_an_initialized_picture():
    tracker = await _tracker()
    await tracker.update(_partial(background_unprocessed1=[(3, 0, 7, 0)]), OLDER)
    assert tracker.initialized
    assert not tracker.line_reached(5)


async def test_the_stamp_is_the_last_folded_push():
    state = FreshnessState()
    tracker = await _tracker(state)
    assert tracker.document_version == 0
    await tracker.update({}, NEWER)
    assert tracker.document_version == NEWER


async def test_reset_forgets_the_picture_and_its_stamp():
    tracker = await _tracker()
    await tracker.reset()
    assert not tracker.initialized and tracker.document_version is None
    assert not tracker.line_reached(5)


# --------------------------------------------------------------------------
# The waits on the tracker's own condition
# --------------------------------------------------------------------------

async def test_line_reached_wait_returns_despite_trailing_run():
    """wait_until_line_reached_bounded keys on the FRONTIER, not prefix-quiet: it
    returns as soon as the dest line leaves unprocessed, even with an earlier
    command still running (so the eval loop can decide complete vs in_progress)."""
    tracker = await _tracker(running=[(7, 0, 7, 0)])
    ok = await tracker.wait_until_line_reached_bounded(
        LSPLine(9), timeout=5.0, health_check=_noop_health_check,
    )
    assert ok
    # The prefix is NOT quiet — line 7 is still running — yet the wait returned.
    assert not tracker.range_processed(LSPLine(0), LSPLine(9))


async def test_line_reached_wait_times_out_when_unreached():
    tracker = await _tracker(unprocessed=[(3, 0, 9, 0)])
    ok = await tracker.wait_until_line_reached_bounded(
        LSPLine(5), timeout=0.1, health_check=_noop_health_check,
    )
    assert not ok


async def test_a_wait_wakes_on_the_push_that_makes_the_picture_fresh():
    state = FreshnessState()
    state.advance(OLDER)
    tracker = await _tracker(state)
    state.advance(NEWER)                      # unfresh until the push at NEWER

    async def push():
        await asyncio.sleep(0.02)
        await tracker.update({}, NEWER)

    task = asyncio.ensure_future(push())
    ok = await tracker.wait_until_processed_bounded(
        LSPLine(0), LSPLine(10), timeout=5.0, health_check=_noop_health_check,
        check_interval=5.0,
    )
    await task
    assert ok


# --------------------------------------------------------------------------
# position_state — the definite answer line_reached cannot give
# --------------------------------------------------------------------------

async def test_position_state_never_evaluated_tracker():
    # No decoration has ever arrived: nothing is processed, which is what the
    # caller must act on — not "unknown".
    assert ProcessingTracker(FreshnessState()).position_state(5) == processing.NOT_EVALUATED


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


async def test_position_state_is_unknown_while_the_picture_is_not_fresh():
    state = FreshnessState()
    t = await _tracker(state)
    assert t.position_state(5) == processing.PROCESSED
    state.advance(NEWER)
    # The picture describes an older document state, and nothing covers the
    # line to prove otherwise.
    assert t.position_state(5) == processing.UNKNOWN
    await t.update({}, NEWER)
    assert t.position_state(5) == processing.PROCESSED


async def test_position_state_stays_definite_while_the_picture_is_not_fresh():
    # The unprocessed scan comes first: not_evaluated is the least-finished
    # verdict and the caller answers it by evaluating, so a range covering the
    # line is still answered — only its ABSENCE is not.
    state = FreshnessState()
    t = await _tracker(state, unprocessed=[(5, 0, 5, 0)])
    state.advance(NEWER)
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


# --------------------------------------------------------------------------
# The full-push invariant: what makes a picture of the whole document
# --------------------------------------------------------------------------

def test_a_full_push_names_every_tracked_type():
    from isabelle_mcp.processing import _TRACKED_TYPES
    full = {typ: [] for typ in _TRACKED_TYPES}
    assert is_full_decoration_push(full)
    assert is_full_decoration_push({**full, "text_keyword1": []})   # extras are fine
    for typ in _TRACKED_TYPES:
        partial = dict(full)
        del partial[typ]
        assert not is_full_decoration_push(partial), typ
    assert not is_full_decoration_push({})


@pytest.mark.asyncio
async def test_initialized_reads_whether_a_full_push_was_folded_in():
    tracker = ProcessingTracker(FreshnessState())
    assert not tracker.initialized
    await tracker.update(_full(), OLDER)
    assert tracker.initialized
    await tracker.reset()
    assert not tracker.initialized
