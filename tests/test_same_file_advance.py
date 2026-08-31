"""A second evaluate_to on the file under evaluation joins the running
evaluation and can only move its target forward (plan section 3).

The run's target is shared state (``evaluation_state.destination_line``);
every completion decision reads it right before deciding, never a copy taken
at entry. On abort, only the last request that had not given up on the run
may end it.
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

    async def test_the_wait_loop_reads_the_target_afresh_each_round(
        self, mock_lsp_client, temp_theory_file,
    ):
        """The loop starts towards 5 with lines 5..10 pending. During its
        first wait the frontier passes 5 AND the target is advanced to 9: a
        loop judging a copy taken at entry would say complete."""
        await mock_lsp_client.open_document(temp_theory_file)
        tracker = MockProcessingTracker(unprocessed=[(4, 0, 9, 0)])
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        run = evaluation_state.start(temp_theory_file, MCPLine(5))
        real_wait = tracker.wait_until_line_reached_bounded

        async def wait_and_move_on(line, **kw):
            tracker.unprocessed[:] = [(8, 0, 9, 0)]        # lines 5..8 now done
            evaluation_state.advance(temp_theory_file, MCPLine(9))
            return await real_wait(line, **kw)
        tracker.wait_until_line_reached_bounded = wait_and_move_on
        status, _, _ = await ev._evaluation_wait_loop(
            mock_lsp_client, temp_theory_file, evaluation_state, run, 0.2)
        assert status == "in_progress"
        assert evaluation_state.destination_line == 9
        tracker.unprocessed.clear()
        status, _, _ = await ev._evaluation_wait_loop(
            mock_lsp_client, temp_theory_file, evaluation_state, run, 0.2)
        assert status == "complete"

    async def test_a_request_of_an_ended_run_keeps_its_own_target(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        """The post-wait re-read is guarded by ownership: if this run ended and
        a successor for another file started while we waited, the successor's
        target must not be reported for this file."""
        async def fake_loop(client, file_path, state, evaluation, timeout):
            state.cancel()
            state.start("/tmp/Other.thy", MCPLine(3))
            return "in_progress", [], []
        monkeypatch.setattr(ev, "_evaluation_wait_loop", fake_loop)
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.destination_line == 5
        assert evaluation_state.active                        # the successor is untouched
        assert evaluation_state.file_path == "/tmp/Other.thy"


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
    """On abort, only the last request that had not given up on the run ends
    it. A request that returned in_progress has not given up: it will poll."""

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

    async def test_the_sole_request_aborting_ends_the_run(
        self, mock_lsp_client, temp_theory_file,
    ):
        tracker = MockProcessingTracker(unprocessed=[(4, 0, 9, 0)])
        driver = await self._run_with_dep(mock_lsp_client, temp_theory_file, tracker)
        driver.cancel()
        with pytest.raises(asyncio.CancelledError):
            await driver
        assert not evaluation_state.active
        assert evaluation_state.auto_opened_files == set()

    async def _abort(self, task):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_an_overtaken_contributor_aborting_leaves_the_run(
        self, mock_lsp_client, temp_theory_file,
    ):
        """A drove to 5, B advanced to 9, A is aborted: the run survives and
        B gets its completion (blocking item 2 of the merged-tree review)."""
        tracker = MockProcessingTracker(unprocessed=[(4, 0, 9, 0)])
        a = await self._run_with_dep(mock_lsp_client, temp_theory_file, tracker)
        b = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 9))
        await _settle()
        await self._abort(a)
        assert evaluation_state.active
        assert evaluation_state.auto_opened_files == {"/tmp/Dep.thy"}
        tracker.unprocessed.clear()
        rb = await b
        assert (rb.status, rb.destination_line) == ("complete", 9)

    async def test_the_advancer_aborting_leaves_the_run_to_the_overtaken(
        self, mock_lsp_client, temp_theory_file,
    ):
        """The mirror case: A drove to 5, B advanced to 9, B is aborted."""
        tracker = MockProcessingTracker(unprocessed=[(4, 0, 9, 0)])
        a = await self._run_with_dep(mock_lsp_client, temp_theory_file, tracker)
        b = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 9))
        await _settle()
        await self._abort(b)
        assert evaluation_state.active
        assert evaluation_state.auto_opened_files == {"/tmp/Dep.thy"}
        tracker.unprocessed.clear()
        ra = await a
        assert (ra.status, ra.destination_line) == ("complete", 9)

    async def test_two_requests_at_the_same_line_either_abort_leaves_the_run(
        self, mock_lsp_client, temp_theory_file,
    ):
        """A query tool at the target line falls through the guard into a
        second evaluate_to at the same line; its timeout must not end the
        run for the first request."""
        tracker = MockProcessingTracker(unprocessed=[(4, 0, 9, 0)])
        a = await self._run_with_dep(mock_lsp_client, temp_theory_file, tracker)
        b = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 5))
        await _settle()
        await self._abort(b)
        assert evaluation_state.active
        assert evaluation_state.auto_opened_files == {"/tmp/Dep.thy"}
        c = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 5))
        await _settle()
        await self._abort(a)                                    # the earlier one this time
        assert evaluation_state.active
        tracker.unprocessed.clear()
        assert (await c).status == "complete"

    async def test_a_joiner_whose_caret_send_fails_leaves_the_run(
        self, mock_lsp_client, temp_theory_file,
    ):
        tracker = MockProcessingTracker(unprocessed=[(4, 0, 9, 0)])
        a = await self._run_with_dep(mock_lsp_client, temp_theory_file, tracker)

        async def failing_caret(file_path, line, character=0):
            raise IsabelleToolError("transport down")
        mock_lsp_client.set_caret = failing_caret
        with pytest.raises(IsabelleToolError, match="transport down"):
            await evaluate_to(mock_lsp_client, temp_theory_file, 9)
        assert evaluation_state.active
        assert evaluation_state.auto_opened_files == {"/tmp/Dep.thy"}
        assert evaluation_state.destination_line == 9
        tracker.unprocessed.clear()
        assert (await a).status == "complete"

    async def test_a_request_that_returned_in_progress_has_not_given_up(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        """A returns in_progress at the poll interval (it will poll again);
        B joins at the same line and is aborted: the run must survive, or A's
        next evaluation_status would say no evaluation exists."""
        monkeypatch.setattr(ev, "EVAL_POLL_INTERVAL", 0.05)
        tracker = MockProcessingTracker(unprocessed=[(8, 0, 9, 0)])
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        a = await evaluate_to(mock_lsp_client, temp_theory_file, 9)
        assert a.status == "in_progress"
        evaluation_state.auto_opened_files.add("/tmp/Dep.thy")
        b = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 9))
        await _settle()
        await self._abort(b)
        assert evaluation_state.active
        assert evaluation_state.auto_opened_files == {"/tmp/Dep.thy"}

    async def test_three_requests_the_last_to_give_up_ends_the_run(
        self, mock_lsp_client, temp_theory_file,
    ):
        tracker = MockProcessingTracker(unprocessed=[(4, 0, 9, 0)])
        a = await self._run_with_dep(mock_lsp_client, temp_theory_file, tracker)
        b = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 7))
        c = asyncio.create_task(evaluate_to(mock_lsp_client, temp_theory_file, 9))
        await _settle()
        run = evaluation_state.current
        assert run is not None and run.riders == 3
        await self._abort(c)
        await self._abort(b)
        assert evaluation_state.active and run.riders == 1
        await self._abort(a)                  # the last one, though behind the target
        assert not evaluation_state.active and run.riders == 0
        assert evaluation_state.auto_opened_files == set()


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

    async def test_a_run_cancelled_during_the_round_trip_is_not_stamped_complete(
        self, mock_lsp_client, temp_theory_file,
    ):
        # The theory_status round trip holds no lock; a cancel landing inside
        # it must not be followed by a COMPLETED sentence (the footer's
        # outcome gate).
        text = await self._footer_at_target(
            mock_lsp_client, temp_theory_file,
            lambda: evaluation_state.cancel())
        assert text == f"Evaluation has arrived at {temp_theory_file}:5."
        assert evaluation_state.current.outcome == "cancelled"
        assert not evaluation_state.active

    async def test_a_completion_stamped_by_a_concurrent_observer_still_says_completed(
        self, mock_lsp_client, temp_theory_file,
    ):
        # One vocabulary in every interleaving: if another outlet stamped the
        # SAME verdict during the round trip, this footer repeats it rather
        # than downgrade to "arrived" (re-finishing an ended run it owns is a
        # no-op). Only a run that ended for another reason blocks the sentence.
        text = await self._footer_at_target(
            mock_lsp_client, temp_theory_file,
            lambda: evaluation_state.complete())
        assert text == f"Evaluation has completed up to {temp_theory_file}:5."
        assert evaluation_state.current.outcome == "complete"


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
            async def queued(n: int) -> None:
                for _ in range(100):
                    if len(lock._waiters or ()) >= n:
                        return
                    await asyncio.sleep(0)
                raise AssertionError(f"{n} waiter(s) never queued on the lock")

            query = asyncio.create_task(
                check_evaluation_guard(mock_lsp_client, temp_theory_file, MCPLine(5)))
            await queued(1)
            other = asyncio.create_task(
                evaluate_to(mock_lsp_client, temp_theory_with_errors, 3))
            await queued(2)
        with pytest.raises(IsabelleToolError) as exc:
            await query
        assert str(exc.value).startswith(
            f"An evaluation is running towards {temp_theory_with_errors}:3")
        assert (await other).status == "in_progress"
