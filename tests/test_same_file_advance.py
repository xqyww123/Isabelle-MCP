"""A second evaluate_to on the file under evaluation joins the running
evaluation and can only move its target forward (plan section 3).

The run's target is shared state (``evaluation_state.destination_line``);
every completion decision reads it right before deciding, never a copy taken
at entry. Only the request whose caret is the run's target may end the run.
"""

import asyncio

import pytest

from isabelle_mcp import evaluation as ev
from isabelle_mcp.evaluation import (
    EVALUATE_TO_REFUSAL,
    check_evaluation_guard,
    evaluate_to,
    evaluation_footer,
    evaluation_state,
)
from isabelle_mcp.models import RunningCommand
from isabelle_mcp.utils import IsabelleToolError, MCPLine
from tests.conftest import MockProcessingTracker


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(ev, "EVAL_POLL_INTERVAL", 0.3)


def _running(elapsed: float) -> RunningCommand:
    return RunningCommand(
        file_path="/tmp/T.thy", start_line=3, end_line=3, text="by auto",
        elapsed_seconds=elapsed,
    )


async def _settle(n: int = 3) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


class TestActivityClause:
    def test_empty_below_threshold(self):
        assert ev._activity_clause([]) == ""
        assert ev._activity_clause([_running(9.9)]) == ""

    def test_one_slow_command(self):
        assert ev._activity_clause([_running(42.7), _running(1.0)]) == (
            ", where one command has been running for 42s"
        )

    def test_several_slow_commands_name_the_longest(self):
        assert ev._activity_clause([_running(12.0), _running(95.2), _running(30.0)]) == (
            ", where 3 commands have been running for over 10s, the longest for 95s"
        )


class TestRefusalCarriesActivity:
    """The two non-empty {activity} variants really reach the refusal text."""

    async def _refused(self, mock_lsp_client, temp_theory_file, temp_theory_with_errors, running):
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        mock_lsp_client.get_all_running_commands = lambda: running
        with pytest.raises(IsabelleToolError) as exc:
            await evaluate_to(mock_lsp_client, temp_theory_with_errors, 3)
        return str(exc.value)

    async def test_one_slow(self, mock_lsp_client, temp_theory_file, temp_theory_with_errors):
        text = await self._refused(
            mock_lsp_client, temp_theory_file, temp_theory_with_errors, [_running(15.0)])
        assert text == EVALUATE_TO_REFUSAL.format(
            target=temp_theory_file, target_line=5,
            activity=", where one command has been running for 15s")

    async def test_several_slow(self, mock_lsp_client, temp_theory_file, temp_theory_with_errors):
        text = await self._refused(
            mock_lsp_client, temp_theory_file, temp_theory_with_errors,
            [_running(15.0), _running(20.5)])
        assert ", where 2 commands have been running for over 10s, the longest for 20s." in text


class TestAdvance:
    def test_advance_only_moves_forward(self, temp_theory_file):
        evaluation_state.start(temp_theory_file, MCPLine(5))
        evaluation_state.advance(temp_theory_file, MCPLine(9))
        assert evaluation_state.destination_line == 9
        evaluation_state.advance(temp_theory_file, MCPLine(3))
        assert evaluation_state.destination_line == 9

    def test_advance_refuses_a_dead_or_foreign_run(self, temp_theory_file):
        with pytest.raises(AssertionError):
            evaluation_state.advance(temp_theory_file, MCPLine(9))     # nothing running
        evaluation_state.start(temp_theory_file, MCPLine(5))
        with pytest.raises(AssertionError):
            evaluation_state.advance("/tmp/Other.thy", MCPLine(9))    # another file
        evaluation_state.complete()
        with pytest.raises(AssertionError):
            evaluation_state.advance(temp_theory_file, MCPLine(9))     # finished run

    async def test_the_wait_loop_reads_the_advanced_target(
        self, mock_lsp_client, temp_theory_file,
    ):
        # Lines 1..8 done, line 9 not: a run started towards 5 would be
        # complete — unless its target was advanced to 9 meanwhile.
        await mock_lsp_client.open_document(temp_theory_file)
        tracker = MockProcessingTracker(unprocessed=[(8, 0, 8, 0)])
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        run = evaluation_state.start(temp_theory_file, MCPLine(5))
        evaluation_state.advance(temp_theory_file, MCPLine(9))
        status, _, _ = await ev._evaluation_wait_loop(
            mock_lsp_client, temp_theory_file, evaluation_state, run, 0.2)
        assert status == "in_progress"
        tracker.unprocessed.clear()
        status, _, _ = await ev._evaluation_wait_loop(
            mock_lsp_client, temp_theory_file, evaluation_state, run, 0.2)
        assert status == "complete"


class TestSameFileRequests:
    async def test_a_request_ahead_advances_the_shared_run(
        self, mock_lsp_client, temp_theory_file,
    ):
        """A waits towards 5, B asks for 9: one run, target 9, both report 9."""
        tracker = MockProcessingTracker(unprocessed=[(4, 0, 9, 0)])   # lines 5..10 pending
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        a = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 5))
        await _settle()
        run = evaluation_state.current
        b = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 9))
        await _settle()
        assert evaluation_state.current is run                 # no second run
        assert evaluation_state.destination_line == 9
        tracker.unprocessed.clear()
        ra, rb = await asyncio.gather(a, b)
        assert (ra.status, ra.destination_line) == ("complete", 9)
        assert (rb.status, rb.destination_line) == ("complete", 9)
        assert not evaluation_state.active

    async def test_a_request_behind_the_target_waits_for_the_real_target(
        self, mock_lsp_client, temp_theory_file,
    ):
        """Target 9, a request for 5: the target and the caret stay, and the
        reply is about the run's target (decision D-B1)."""
        tracker = MockProcessingTracker(unprocessed=[(6, 0, 9, 0)])   # lines 7..10 pending
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        carets: list[int] = []

        async def set_caret(file_path, line, character=0):
            carets.append(int(line))
        mock_lsp_client.set_caret = set_caret
        first = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 9))
        await _settle()
        behind = await evaluate_to(mock_lsp_client, temp_theory_file, 5)   # line 5 is done
        assert behind.status == "in_progress"
        assert behind.destination_line == 9
        assert behind.message.startswith(f"Evaluating towards {temp_theory_file}:9.")
        assert evaluation_state.destination_line == 9
        assert carets == [8]                                    # only the first request moved it
        # Both calls returned early (poll interval) with the run still going.
        assert (await first).destination_line == 9
        assert evaluation_state.active

    async def test_the_query_guard_lets_the_same_file_through(
        self, mock_lsp_client, temp_theory_file,
    ):
        """A query beyond the target advances it instead of being refused."""
        await mock_lsp_client.open_document(temp_theory_file)
        tracker = MockProcessingTracker(unprocessed=[(4, 0, 9, 0)])
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        first = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 5))
        await _settle()
        query = asyncio.create_task(
            check_evaluation_guard(mock_lsp_client, temp_theory_file, MCPLine(8)))
        await _settle()
        assert evaluation_state.destination_line == 8
        tracker.unprocessed.clear()
        assert await query is None                              # served after completion
        assert (await first).destination_line == 8


class TestWhoMayEndTheRun:
    """Only the request whose caret is the run's target ends it on abort."""

    async def _run_with_dep(self, mock_lsp_client, temp_theory_file, tracker):
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        driver = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 5))
        await _settle()
        evaluation_state.auto_opened_files.add("/tmp/Dep.thy")
        return driver

    async def test_an_aborted_waiter_behind_the_target_leaves_the_run(
        self, mock_lsp_client, temp_theory_file,
    ):
        tracker = MockProcessingTracker(unprocessed=[(4, 0, 9, 0)])
        driver = await self._run_with_dep(mock_lsp_client, temp_theory_file, tracker)
        waiter = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 3))
        await _settle()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert evaluation_state.active
        assert evaluation_state.auto_opened_files == {"/tmp/Dep.thy"}
        tracker.unprocessed.clear()
        assert (await driver).status == "complete"

    async def test_an_aborted_caret_contributor_still_cancels(
        self, mock_lsp_client, temp_theory_file,
    ):
        tracker = MockProcessingTracker(unprocessed=[(4, 0, 9, 0)])
        driver = await self._run_with_dep(mock_lsp_client, temp_theory_file, tracker)
        driver.cancel()
        with pytest.raises(asyncio.CancelledError):
            await driver
        assert not evaluation_state.active
        assert evaluation_state.auto_opened_files == set()

    async def test_an_overtaken_contributor_aborting_leaves_the_run(
        self, mock_lsp_client, temp_theory_file,
    ):
        """A drove to 5, B advanced to 9, A is aborted: the run survives and
        B gets its completion (blocking item 2 of the merged-tree review)."""
        tracker = MockProcessingTracker(unprocessed=[(4, 0, 9, 0)])
        a = await self._run_with_dep(mock_lsp_client, temp_theory_file, tracker)
        b = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 9))
        await _settle()
        a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await a
        assert evaluation_state.active
        assert evaluation_state.auto_opened_files == {"/tmp/Dep.thy"}
        tracker.unprocessed.clear()
        rb = await b
        assert (rb.status, rb.destination_line) == ("complete", 9)


class TestFooterGuard:
    """The footer stamps "complete" only if the target it judged is still the
    run's target after its theory_status round trip."""

    async def _footer_at_target(self, mock_lsp_client, temp_theory_file, on_status):
        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker()
        evaluation_state.start(temp_theory_file, MCPLine(5))
        real_status = mock_lsp_client.request_theory_status

        async def status():
            on_status()
            return await real_status()
        mock_lsp_client.request_theory_status = status
        return await evaluation_footer(mock_lsp_client)

    async def test_unchanged_target_completes(self, mock_lsp_client, temp_theory_file):
        text = await self._footer_at_target(mock_lsp_client, temp_theory_file, lambda: None)
        assert text == f"Evaluation has completed up to {temp_theory_file}:5."
        assert not evaluation_state.active

    async def test_a_target_advanced_meanwhile_is_only_arrived(
        self, mock_lsp_client, temp_theory_file,
    ):
        text = await self._footer_at_target(
            mock_lsp_client, temp_theory_file,
            lambda: evaluation_state.advance(temp_theory_file, MCPLine(9)))
        assert text.startswith(f"Evaluation has arrived at {temp_theory_file}:5.")
        assert evaluation_state.active                          # not stamped
        assert evaluation_state.destination_line == 9


class TestGuardToEvaluateToGap:
    """Section 10A, unverified item 3: the query guard releases the lock before
    calling evaluate_to, and there is no await between the two. So the gap is
    reachable only through the lock's fair queue: when a third holder (a file
    sync push, a cancel) had both the query and another file's evaluate_to
    waiting, the query passes its guard, releases, and the other request —
    queued behind it — gets the lock before the query's evaluate_to does. The
    query is then refused with EVALUATE_TO_REFUSAL (evaluate_to's sentence)
    rather than NOT_EVALUATED_REFUSAL (the guard's). True in substance, wrong
    voice; recorded, not fixed."""

    async def test_which_sentence_the_gap_yields(
        self, mock_lsp_client, temp_theory_file, temp_theory_with_errors, monkeypatch,
    ):
        from isabelle_mcp.processing import NOT_EVALUATED
        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        mock_lsp_client._processing_trackers[temp_theory_with_errors] = MockProcessingTracker(
            all_processed=False,
        )

        # The settled-state layer takes the same lock for its resync; skip it so
        # the guard's own acquisition is the query's first.
        async def not_evaluated(client, file_path, line):
            return NOT_EVALUATED
        monkeypatch.setattr(ev, "_settled_position_state", not_evaluated)
        lock = ev._evaluation_state_lock
        async with lock:
            # Queue on the lock in this order: the query first, the other
            # file's evaluate_to second.
            query = asyncio.create_task(
                check_evaluation_guard(mock_lsp_client, temp_theory_file, MCPLine(5)))
            while len(lock._waiters or ()) < 1:
                await asyncio.sleep(0)
            other = asyncio.create_task(
                evaluate_to(mock_lsp_client, temp_theory_with_errors, 3))
            while len(lock._waiters or ()) < 2:
                await asyncio.sleep(0)
        with pytest.raises(IsabelleToolError) as exc:
            await query
        assert str(exc.value).startswith(
            f"An evaluation is running towards {temp_theory_with_errors}:3")
        assert (await other).status == "in_progress"
