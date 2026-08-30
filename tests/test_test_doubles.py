"""Guards on the test doubles in conftest.py.

Three things the suite silently lost before and must not lose again: the
tracker stub answers per line from its ranges (not a constant), the mock
client yields so a frontier can advance under a waiting evaluation, and the
long-evaluation re-stat path is reachable through the mock.
"""

import asyncio

import pytest

from isabelle_mcp import evaluation as ev
from isabelle_mcp.evaluation import evaluate_to
from isabelle_mcp.processing import NOT_EVALUATED, PROCESSED, RUNNING
from isabelle_mcp.utils import LSPLine
from tests.conftest import MockProcessingTracker


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(ev, "EVAL_POLL_INTERVAL", 0.2)


class TestTrackerStubContract:
    """The predicates derive from the ranges, with the real tracker's rules."""

    def test_predicates_follow_the_ranges(self):
        t = MockProcessingTracker(unprocessed=[(5, 0, 7, 3)], running=[(2, 0, 2, 9)])
        # line_reached: outside every unprocessed range; running ranges do not count.
        assert t.line_reached(4) and t.line_reached(2) and t.line_reached(8)
        assert not t.line_reached(5) and not t.line_reached(6) and not t.line_reached(7)
        # range_processed: no unprocessed OR running range overlaps [start, end].
        assert t.range_processed(LSPLine(0), LSPLine(1))
        assert not t.range_processed(LSPLine(0), LSPLine(2))
        assert not t.range_processed(LSPLine(7), LSPLine(9))
        assert t.range_processed(LSPLine(8), LSPLine(9))
        assert t.line_running(2) and not t.line_running(3)
        assert t.position_state(1) == PROCESSED
        assert t.position_state(2) == RUNNING
        assert t.position_state(6) == NOT_EVALUATED
        assert t.range_state(0, 1) == (PROCESSED, 0.0)
        assert t.range_state(1, 2) == (RUNNING, 0.0)
        assert t.range_state(2, 6) == (NOT_EVALUATED, 0.0)   # least-finished wins
        assert not t.all_processed

    def test_all_processed_false_means_nothing_evaluated(self):
        t = MockProcessingTracker(all_processed=False)
        assert not t.line_reached(0) and not t.range_processed(LSPLine(0), LSPLine(0))
        assert t.position_state(0) == NOT_EVALUATED
        assert MockProcessingTracker().line_reached(10**6)

    def test_forced_answers_override_the_ranges(self):
        t = MockProcessingTracker(frontier=True, quiet=False, unprocessed=[(7, 0, 7, 5)])
        assert t.line_reached(7) and not t.range_processed(LSPLine(0), LSPLine(3))

    async def test_wait_stubs_answer_from_live_ranges(self):
        t = MockProcessingTracker(unprocessed=[(5, 0, 7, 3)])
        assert not await t.wait_until_line_reached_bounded(LSPLine(6))
        t.unprocessed.clear()
        assert await t.wait_until_line_reached_bounded(LSPLine(6))
        assert await t.wait_until_processed_bounded(LSPLine(0), LSPLine(9))


class TestConcurrentFrontierAdvance:
    """An evaluation waiting on the mock sees a frontier advanced by another task."""

    async def test_evaluation_completes_when_another_task_advances_the_frontier(
        self, mock_lsp_client, temp_theory_file,
    ):
        tracker = MockProcessingTracker(unprocessed=[(6, 0, 9, 0)])   # 0-idx 6.. -> lines 7..10
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker

        async def advance():
            for _ in range(3):
                await asyncio.sleep(0)
            tracker.unprocessed[:] = [(9, 0, 9, 0)]      # frontier now past line 9
        advancing = asyncio.create_task(advance())

        result = await evaluate_to(mock_lsp_client, temp_theory_file, 9)
        await advancing
        assert result.status == "complete"
        assert result.destination_line == 9


class TestRestatPathReachable:
    """The wait loop's periodic re-stat (``_LONG_EVAL_RESTAT_INTERVAL``) goes
    through ``resync_changed_open_documents`` — the mock must have it."""

    async def test_long_wait_resyncs_through_the_mock(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        monkeypatch.setattr(ev, "_LONG_EVAL_RESTAT_INTERVAL", 0.0)
        resyncs = 0
        mock_resync = mock_lsp_client.resync_changed_open_documents   # must exist

        async def counting_resync():
            nonlocal resyncs
            resyncs += 1
            await mock_resync()
        mock_lsp_client.resync_changed_open_documents = counting_resync
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        result = await evaluate_to(mock_lsp_client, temp_theory_file, 9)
        assert result.status == "in_progress"
        assert resyncs >= 1
