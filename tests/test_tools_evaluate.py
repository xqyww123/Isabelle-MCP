import asyncio
import os
from pathlib import Path

import pytest

from isabelle_mcp import evaluation as ev
from isabelle_mcp.evaluation import (
    cancel_evaluation,
    evaluate_to,
    evaluation_state,
    evaluation_status,
    format_evaluation_result,
    resync_changed_open_documents_locked,
    sync_file_locked,
)
from isabelle_mcp.lsp_client import DocumentState
from isabelle_mcp.models import EvaluationView, FileSnapshot, RunningCommand
from isabelle_mcp.processing import ProcessingTracker, parse_decoration_ranges
from isabelle_mcp.utils import IsabelleToolError, MCPLine
from tests.conftest import MockProcessingTracker


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    # Keep the in_progress/timeout path from busy-waiting the full poll interval.
    monkeypatch.setattr(ev, "EVAL_POLL_INTERVAL", 0.05)


def _file(view: EvaluationView, path: str) -> FileSnapshot | None:
    return next((f for f in view.files if f.file_path == path), None)


class TestEvaluateTo:
    @pytest.mark.asyncio
    async def test_completes_immediately(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=True,
        )
        result = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert result.status == "complete"
        assert result.destination_line == 5
        assert "complete" in result.message.lower()

    @pytest.mark.asyncio
    async def test_times_out(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        result = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert result.status == "in_progress"
        assert result.destination_line == 5
        assert result.message == (
            f"Evaluating towards {temp_theory_file}:5."
        )

    @pytest.mark.asyncio
    async def test_another_file_while_active_is_refused(
        self, temp_theory_file, temp_theory_with_errors, mock_lsp_client,
    ):
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert evaluation_state.active

        with pytest.raises(IsabelleToolError) as excinfo:
            await evaluate_to(mock_lsp_client, temp_theory_with_errors, 3)
        # The approved sentence: who has the prover, and the two ways out.
        assert str(excinfo.value) == (
            f"An evaluation is running towards {temp_theory_file}:5. You cannot "
            "evaluate another file until it finishes, or you cancel it with "
            "isabelle_cancel_evaluation."
        )

    @pytest.mark.asyncio
    async def test_reports_error_lines_from_decoration(self, temp_theory_file, mock_lsp_client):
        # errors is text_overview_error and nothing else: the failed proof on
        # 0-indexed line 4 (also bad) is an error; the bad-only range on line 6
        # (a `back`, a sorry the server also marks bad) is not.
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=True,
            overview_error=[(4, 0, 4, 10)],
            bad=[(4, 0, 4, 10), (6, 0, 6, 5)],
        )
        result = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        fs = _file(result, temp_theory_file)
        assert fs is not None and fs.lined
        assert fs.errors == [(5, 5)] and fs.error_count == 1
        assert fs.sorry == []

    @pytest.mark.asyncio
    async def test_negative_line(self, temp_theory_file, mock_lsp_client):
        # The fixture is 11 real lines ending in "end\n"; -1 is that "end"
        # line, not the empty string after the final newline.
        result = await evaluate_to(mock_lsp_client, temp_theory_file, -1)
        assert result.status == "complete"
        assert result.destination_line == 11

    @pytest.mark.asyncio
    async def test_negative_line_without_trailing_newline(self, tmp_path, mock_lsp_client):
        path = tmp_path / "NoNl.thy"
        path.write_text("theory NoNl\nimports Main\nbegin\nend")
        result = await evaluate_to(mock_lsp_client, str(path), -1)
        assert result.destination_line == 4

    @pytest.mark.asyncio
    async def test_negative_line_with_after_text(self, temp_theory_file, mock_lsp_client):
        # after_text on the phantom empty line used to fail with "not found".
        result = await evaluate_to(mock_lsp_client, temp_theory_file, -1, after_text="end")
        assert result.status == "complete"
        assert result.destination_line == 11

    @pytest.mark.asyncio
    async def test_after_text_same_line(self, temp_theory_file, mock_lsp_client):
        result = await evaluate_to(
            mock_lsp_client, temp_theory_file, 5, after_text="my_const",
        )
        assert result.status == "complete"
        assert result.destination_line == 5

    @pytest.mark.asyncio
    async def test_after_text_spans_to_later_line(self, temp_theory_file, mock_lsp_client):
        result = await evaluate_to(
            mock_lsp_client, temp_theory_file, 8, after_text='= 42" by',
        )
        assert result.status == "complete"
        assert result.destination_line == 9

    @pytest.mark.asyncio
    async def test_after_text_not_found(self, temp_theory_file, mock_lsp_client):
        with pytest.raises(IsabelleToolError, match="not found on line 5"):
            await evaluate_to(
                mock_lsp_client, temp_theory_file, 5, after_text="no_such_token_zzz",
            )

    @pytest.mark.asyncio
    async def test_fork_pending_returns_in_progress_not_clean(
        self, temp_theory_file, mock_lsp_client,
    ):
        # Frontier reached dest, but a trailing command in the prefix is still
        # unprocessed (a queued/in-flight fork). Must NOT report complete/clean;
        # returns in_progress immediately (grace=0) and lists the pending line.
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            frontier=True, quiet=False, unprocessed=[(7, 0, 7, 5)],  # 0-idx 7 -> line 8
        )
        result = await evaluate_to(mock_lsp_client, temp_theory_file, 9)
        assert result.status == "in_progress"
        assert "clean" not in result.message.lower()
        fs = _file(result, temp_theory_file)
        assert fs is not None and fs.lined
        assert fs.pending == [(8, 8)]
        assert fs.state != "clean"

    @pytest.mark.asyncio
    async def test_pending_clipped_to_dest(self, temp_theory_file, mock_lsp_client):
        # Unprocessed spans past the destination; pending is capped at dest, never
        # reporting the unevaluated tail.
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            frontier=True, quiet=False, unprocessed=[(2, 0, 9, 5)],  # 0-idx lines 2..9
        )
        result = await evaluate_to(mock_lsp_client, temp_theory_file, 5)  # dest line 5
        fs = _file(result, temp_theory_file)
        assert fs is not None
        assert fs.pending == [(3, 5)]  # 0-idx 2..4 (capped at dest) -> 1-idx 3..5


class TestEvaluationStatus:
    @pytest.mark.asyncio
    async def test_no_evaluation(self, mock_lsp_client):
        result = await evaluation_status(mock_lsp_client)
        assert result.status == "no_evaluation"

    @pytest.mark.asyncio
    async def test_completes(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        await evaluate_to(mock_lsp_client, temp_theory_file, 5)

        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=True,
        )
        result = await evaluation_status(mock_lsp_client)
        assert result.status == "complete"

    @pytest.mark.asyncio
    async def test_full_errors_each_poll(self, temp_theory_file, mock_lsp_client):
        # Drop the old incremental semantics: every poll reports the FULL set.
        tracker = MockProcessingTracker(
            all_processed=False, overview_error=[(2, 0, 2, 5)],
        )
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        r1 = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert _file(r1, temp_theory_file).errors == [(3, 3)]

        tracker._oerr.append((3, 0, 3, 5))
        r2 = await evaluation_status(mock_lsp_client)
        # Both errors present — not just the newly-appeared one.
        assert _file(r2, temp_theory_file).errors == [(3, 3), (4, 4)]

    @pytest.mark.asyncio
    async def test_fork_resolves_to_error_then_complete(
        self, temp_theory_file, mock_lsp_client,
    ):
        # While the fork is in flight: frontier reached, prefix not quiet -> in_progress.
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            frontier=True, quiet=False, unprocessed=[(7, 0, 7, 5)],
        )
        r1 = await evaluate_to(mock_lsp_client, temp_theory_file, 9)
        assert r1.status == "in_progress"

        # Fork joins and FAILS: same push clears the busy state AND posts the error
        # (modelled as quiet=True with the overview_error now present).
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            frontier=True, quiet=True, overview_error=[(7, 0, 7, 5)],  # line 8
        )
        r2 = await evaluation_status(mock_lsp_client)
        assert r2.status == "complete"
        fs = _file(r2, temp_theory_file)
        assert fs is not None and fs.errors == [(8, 8)]
        assert fs.state == "problems"
        # One completion vocabulary (problem 8): internal complete ⇒ COMPLETED,
        # failures and all — they are above, per line, in the file section.
        assert r2.message == f"Evaluation has completed up to {temp_theory_file}:9."

    @pytest.mark.asyncio
    async def test_a_restarted_run_is_not_stamped_by_the_old_observer(
        self, temp_theory_file, mock_lsp_client,
    ):
        # Cancel + restart at the same file and line during the theory_status
        # round trip: a bare line-equality check would pass, but the run this
        # call judged is gone. The stamp is refused, the successor reports
        # in_progress truthfully, and the next poll completes it.
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker()
        real_status = mock_lsp_client.request_theory_status

        async def cancel_and_restart():
            result = await real_status()
            if evaluation_state.active and evaluation_state.current.outcome == "":
                evaluation_state.cancel()
                evaluation_state.start(temp_theory_file, MCPLine(5))
            return result

        mock_lsp_client.request_theory_status = cancel_and_restart
        view = await evaluation_status(mock_lsp_client)
        assert view.status == "in_progress"
        assert evaluation_state.active
        assert evaluation_state.current.outcome == ""

        mock_lsp_client.request_theory_status = real_status
        final = await evaluation_status(mock_lsp_client)
        assert final.status == "complete"


class TestCancelEvaluation:
    @pytest.mark.asyncio
    async def test_no_evaluation(self, mock_lsp_client):
        result = await cancel_evaluation(mock_lsp_client)
        assert result.status == "no_evaluation"

    @pytest.mark.asyncio
    async def test_cancel_in_progress(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert evaluation_state.active

        result = await cancel_evaluation(mock_lsp_client)
        assert result.status == "cancelled"
        assert not evaluation_state.active


def _fork(path: str) -> RunningCommand:
    return RunningCommand(
        file_path=path, start_line=5, end_line=5,
        text='value "slow"', elapsed_seconds=3.0,
    )


class TestLingeringFork:
    """evaluate_to clears `active` once the frontier reaches the target, but a
    forked command (value / async proof) may still run. status and cancel must
    keep seeing and interrupting it — not short-circuit to 'no evaluation'."""

    @pytest.mark.asyncio
    async def test_status_sees_lingering_fork(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=True,
        )
        done = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert done.status == "complete"
        assert not evaluation_state.active

        monkeypatch.setattr(
            mock_lsp_client, "get_all_running_commands",
            lambda: [_fork(temp_theory_file)],
        )
        result = await evaluation_status(mock_lsp_client)
        assert result.status == "in_progress"      # not collapsed to no_evaluation
        assert result.running_commands             # the fork is surfaced

    @pytest.mark.asyncio
    async def test_cancel_interrupts_lingering_fork(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=True,
        )
        await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert not evaluation_state.active

        interrupted: list[bool] = []
        monkeypatch.setattr(
            mock_lsp_client, "get_all_running_commands",
            lambda: [_fork(temp_theory_file)],
        )

        async def _spy():
            interrupted.append(True)
            return {"outcome": "retired", "retired": [], "excluded": [],
                    "waived": [], "unloaded_from": []}

        monkeypatch.setattr(mock_lsp_client, "force_interrupt", _spy)

        result = await cancel_evaluation(mock_lsp_client)
        assert result.status == "cancelled"
        assert interrupted == [True]    # the request went to the server

    @pytest.mark.asyncio
    async def test_idle_reports_no_evaluation(self, mock_lsp_client):
        # No active eval and no running fork → genuinely idle, both stay quiet.
        assert (await evaluation_status(mock_lsp_client)).status == "no_evaluation"
        assert (await cancel_evaluation(mock_lsp_client)).status == "no_evaluation"


class TestSnapshotCategorization:
    """_build_file_snapshot: decoration categorisation + theory_status fallback."""

    def _ts(self, node, **kw):
        from isabelle_mcp.models import TheoryStatus
        base = dict(
            node_name=node, theory_name="T", external=False, imports=[], ok=True,
            total=10, unprocessed=0, running=0, warned=0, failed=0, finished=10,
            consolidated=True,
        )
        base.update(kw)
        return TheoryStatus(**base)

    @staticmethod
    def _open(client, path: str, content: str = "theory T\nimports Main\nbegin\n\n\n\n\n\n\n\nend\n"):
        # A tracker is readable only for an open document.
        client.open_documents[path] = DocumentState(
            file_path=path, uri=f"file://{path}", version=1, content=content,
        )

    def test_errors_are_overview_errors_only_and_sorry_has_its_own_row(self, mock_lsp_client):
        from isabelle_mcp.evaluation import _build_file_snapshot
        path = "/tmp/T.thy"
        # A failed proof (line 5: bad AND overview_error) is an error; a sorry
        # (line 7: bad, and the server's own background_sorry) is a sorry row,
        # not an error and not counted. The warning is not reported anywhere.
        self._open(mock_lsp_client, path)
        mock_lsp_client._processing_trackers[path] = MockProcessingTracker(
            overview_error=[(4, 0, 4, 5)],
            bad=[(4, 0, 4, 5), (6, 0, 6, 5)],
            sorry=[(6, 0, 6, 5)],
            overview_warning=[(8, 0, 8, 5)],
        )
        fs = _build_file_snapshot(mock_lsp_client, path, {path: self._ts(path)})
        assert fs.lined and fs.state == "problems"
        assert fs.errors == [(5, 5)] and fs.error_count == 1
        assert fs.sorry == [(7, 7)]
        assert fs.running == []
        assert not hasattr(fs, "warnings") and not hasattr(fs, "sorry_count")

    def test_a_sorry_only_file_is_clean_with_a_sorry_row(self, mock_lsp_client):
        from isabelle_mcp.evaluation import _build_file_snapshot
        path = "/tmp/S.thy"
        self._open(mock_lsp_client, path)
        mock_lsp_client._processing_trackers[path] = MockProcessingTracker(
            bad=[(4, 0, 4, 5)], sorry=[(4, 0, 4, 5)],
        )
        fs = _build_file_snapshot(mock_lsp_client, path, {path: self._ts(path)})
        assert fs.state == "clean" and fs.error_count == 0
        assert fs.sorry == [(5, 5)]
        # The sorry is what brings the file into a report, by its own name —
        # even if the server stopped marking a sorry as bad.
        mock_lsp_client._processing_trackers[path] = MockProcessingTracker(
            sorry=[(4, 0, 4, 5)],
        )
        from isabelle_mcp.evaluation import _relevant_files
        assert _relevant_files(mock_lsp_client, "", [self._ts(path)]) == [path]

    def test_end_of_document_decoration_survives_clipping(self, mock_lsp_client):
        """An error anchored after the final newline (0-idx line == number of
        newlines) is a real Isabelle position; the "+1" line count must keep it.
        A count of real lines (as evaluate_to uses for -1) would drop it."""
        from isabelle_mcp.evaluation import _build_file_snapshot, _failed_count
        path = "/tmp/T.thy"
        content = "theory T\nimports Main\nbegin\n"          # 3 newlines -> 0-idx line 3 exists for decorations
        self._open(mock_lsp_client, path, content)
        theories = [self._ts(path)]
        mock_lsp_client._processing_trackers[path] = MockProcessingTracker(
            overview_error=[(3, 0, 3, 0)], bad=[(3, 0, 3, 0)],
        )
        fs = _build_file_snapshot(mock_lsp_client, path, {path: theories[0]})
        assert fs.errors == [(4, 4)]
        assert _failed_count(mock_lsp_client, theories) == 1
        # ...while a range starting further past EOF is still clipped away.
        mock_lsp_client._processing_trackers[path] = MockProcessingTracker(
            overview_error=[(4, 0, 4, 0)], bad=[(4, 0, 4, 0)],
        )
        fs = _build_file_snapshot(mock_lsp_client, path, {path: theories[0]})
        assert fs.errors == []
        assert _failed_count(mock_lsp_client, theories) == 0

    def test_fallback_counts_when_no_tracker(self, mock_lsp_client):
        from isabelle_mcp.evaluation import _build_file_snapshot
        path = "/tmp/Dep.thy"
        ts = self._ts(path, ok=False, failed=2, warned=1)
        fs = _build_file_snapshot(mock_lsp_client, path, {path: ts})
        assert not fs.lined
        assert fs.state == "problems"
        assert fs.error_count == 2

    def test_a_warning_alone_is_no_problem_in_either_flavour(self, mock_lsp_client):
        from isabelle_mcp.evaluation import _build_file_snapshot
        path = "/tmp/Warned.thy"
        # theory_status flavour: warned is not a problem, and not a reason to
        # distrust a decoration that shows nothing.
        ts = self._ts(path, warned=1)
        assert _build_file_snapshot(mock_lsp_client, path, {path: ts}).state == "clean"
        # decoration flavour: a warning range renders no row, yet it still
        # proves the decoration is fresh content (the deco_has_content test).
        self._open(mock_lsp_client, path)
        mock_lsp_client._processing_trackers[path] = MockProcessingTracker(
            overview_warning=[(4, 0, 4, 5)],
        )
        fs = _build_file_snapshot(
            mock_lsp_client, path, {path: self._ts(path, ok=False, failed=1)},
        )
        assert fs.lined and fs.state == "clean" and fs.error_count == 0

    def test_fallback_in_progress_not_clean(self, mock_lsp_client):
        from isabelle_mcp.evaluation import _build_file_snapshot
        path = "/tmp/Dep.thy"
        ts = self._ts(path, consolidated=False, unprocessed=3)
        fs = _build_file_snapshot(mock_lsp_client, path, {path: ts})
        assert not fs.lined
        assert fs.state == "in_progress"

    def test_pending_surfaced_for_target_with_dest(self, mock_lsp_client):
        from isabelle_mcp.evaluation import _build_file_snapshot
        path = "/tmp/T.thy"
        # Unprocessed prefix, no errors -> in_progress with the pending lines, not clean.
        self._open(mock_lsp_client, path)
        mock_lsp_client._processing_trackers[path] = MockProcessingTracker(
            unprocessed=[(2, 0, 4, 5)],  # 0-idx 2..4 -> 1-idx 3..5
        )
        fs = _build_file_snapshot(
            mock_lsp_client, path, {path: self._ts(path)}, dest_line=MCPLine(8),
        )
        assert fs.lined
        assert fs.pending == [(3, 5)] and fs.pending_count == 1
        assert fs.errors == [] and fs.state == "in_progress"

    def test_pending_absent_without_dest(self, mock_lsp_client):
        # Non-target files (dest_line=None) never surface pending, even when unprocessed.
        from isabelle_mcp.evaluation import _build_file_snapshot
        path = "/tmp/Dep.thy"
        self._open(mock_lsp_client, path)
        mock_lsp_client._processing_trackers[path] = MockProcessingTracker(
            unprocessed=[(2, 0, 4, 5)],
        )
        fs = _build_file_snapshot(mock_lsp_client, path, {path: self._ts(path)})
        assert fs.lined and fs.pending == [] and fs.state == "clean"


class TestReportUnion:
    """_relevant_files / _snapshot_files: the report's file set is the explicit
    union of the target, every theory_status row with something failed or
    running (whole document model), and every open document with a problem
    or running decoration; nodes with only unprocessed commands are counted
    in the summary line."""

    def _ts(self, node, **kw):
        from isabelle_mcp.models import TheoryStatus
        base = dict(
            node_name=node, theory_name=Path(node).stem, external=False,
            imports=[], ok=True, total=10, unprocessed=0, running=0, warned=0,
            failed=0, finished=10, consolidated=True,
        )
        base.update(kw)
        return TheoryStatus(**base)

    @pytest.mark.asyncio
    async def test_a_failed_import_outside_the_open_set_is_listed_by_count(
        self, temp_theory_file, mock_lsp_client,
    ):
        from isabelle_mcp.evaluation import _snapshot_files
        await mock_lsp_client.open_document(temp_theory_file)
        theories = [
            self._ts(temp_theory_file),
            self._ts("/lib/Broken.thy", ok=False, failed=2, external=True),
            self._ts("/lib/Loading.thy", unprocessed=7, finished=3,
                     consolidated=False, external=True),
        ]
        files = _snapshot_files(mock_lsp_client, temp_theory_file, theories, MCPLine(5))
        assert [fs.file_path for fs in files] == [temp_theory_file, "/lib/Broken.thy"]
        broken = files[1]
        assert not broken.lined and broken.error_count == 2
        # The loading import has nothing failed or running: summary line only.
        from isabelle_mcp.evaluation import _unprocessed_theory_count
        assert _unprocessed_theory_count(theories, {fs.file_path for fs in files}) == 1

    @pytest.mark.asyncio
    async def test_a_rendered_file_is_not_counted_again_in_the_summary_line(
        self, temp_theory_file, mock_lsp_client,
    ):
        # The caret perspective leaves everything past the target unprocessed,
        # so the target's own row has unprocessed > 0 on every mid-file
        # evaluation. It is rendered above; it is not "1 theory is not yet
        # processed." below. An import that is merely loading still counts.
        from isabelle_mcp.evaluation import _snapshot_files, _summary_count
        await mock_lsp_client.open_document(temp_theory_file)
        theories = [
            self._ts(temp_theory_file, unprocessed=6, finished=4, consolidated=False),
            self._ts("/lib/Loading.thy", unprocessed=7, finished=3,
                     consolidated=False, external=True),
        ]
        files = _snapshot_files(mock_lsp_client, temp_theory_file, theories, MCPLine(5))
        assert [fs.file_path for fs in files] == [temp_theory_file]
        assert _summary_count(theories, files) == 1
        # The same for a file the decoration scan brought in.
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            overview_error=[(2, 0, 2, 5)], bad=[(2, 0, 2, 5)],
        )
        files = _snapshot_files(mock_lsp_client, "", theories)
        assert [fs.file_path for fs in files] == [temp_theory_file]
        assert _summary_count(theories, files) == 1

    @pytest.mark.asyncio
    async def test_evaluating_to_mid_file_shows_no_summary_line_for_the_target(
        self, temp_theory_file, mock_lsp_client,
    ):
        real_status = mock_lsp_client.request_theory_status

        async def mid_file():
            theories = await real_status()
            theories[0].update(unprocessed=6, finished=4, consolidated=False)
            return theories

        mock_lsp_client.request_theory_status = mid_file
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.status == "complete" and view.unprocessed_theories == 0
        assert "not yet processed" not in format_evaluation_result(view, None)

    @pytest.mark.asyncio
    async def test_a_theory_status_row_keeps_its_snapshot_even_without_rows(
        self, temp_theory_file, mock_lsp_client,
    ):
        # A failed theory whose open document's tracker holds only unprocessed
        # ranges renders no row — and must still be listed: item 5 lists every
        # failed/running row per file. (Mutation control for the `f in listed`
        # branch of _snapshot_files.)
        from isabelle_mcp.evaluation import _snapshot_files
        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            unprocessed=[(6, 0, 9, 0)],
        )
        theories = [self._ts(temp_theory_file, ok=False, failed=1, finished=9)]
        files = _snapshot_files(mock_lsp_client, "", theories)
        assert [fs.file_path for fs in files] == [temp_theory_file]

    @pytest.mark.asyncio
    async def test_a_symlinked_node_name_lists_the_file_once_with_its_counts(
        self, temp_theory_file, mock_lsp_client, tmp_path,
    ):
        """Negative control for the canonicalization at _parse_theory_status:
        drop the _canon there and this test reds — the symlink spelling passes
        the string dedup as a second file, and misses the ts_map, so the row
        renders with neither line numbers nor counts."""
        from isabelle_mcp.evaluation import _parse_theory_status, _snapshot_files
        real = temp_theory_file
        link_dir = tmp_path / "link"
        link_dir.symlink_to(Path(real).parent, target_is_directory=True)
        alias = str(link_dir / Path(real).name)
        assert alias != real and os.path.realpath(alias) == real
        # The file is open under its real path with an error decoration; the
        # prover names it by the symlink spelling.
        await mock_lsp_client.open_document(real)
        mock_lsp_client._processing_trackers[real] = MockProcessingTracker(
            all_processed=True,
        )
        theories = [_parse_theory_status({
            "node_name": alias, "theory_name": "Test", "external": True,
            "imports": [], "ok": False, "total": 10, "unprocessed": 0,
            "running": 0, "warned": 0, "failed": 1, "finished": 9,
            "canceled": False, "consolidated": True, "percentage": 100,
        })]
        files = _snapshot_files(mock_lsp_client, "", theories)
        assert [fs.file_path for fs in files] == [real]
        assert files[0].error_count == 1

    @pytest.mark.asyncio
    async def test_a_decoration_only_file_that_renders_no_row_is_left_out(
        self, temp_theory_file, mock_lsp_client,
    ):
        from isabelle_mcp.evaluation import _snapshot_files
        await mock_lsp_client.open_document(temp_theory_file)
        # A benign bad range (a `back`): the tracker brings the file in, no row
        # comes of it, and a lone "clean" line is not a report.
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            bad=[(4, 0, 4, 4)],
        )
        assert _snapshot_files(mock_lsp_client, "", [self._ts(temp_theory_file)]) == []
        # Likewise a running range past EOF, which clipping leaves nothing of.
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            running=[(40, 0, 40, 5)],
        )
        assert _snapshot_files(mock_lsp_client, "", [self._ts(temp_theory_file)]) == []
        # The target keeps its snapshot whatever it renders.
        files = _snapshot_files(
            mock_lsp_client, temp_theory_file, [self._ts(temp_theory_file)], MCPLine(5),
        )
        assert [fs.file_path for fs in files] == [temp_theory_file]

    def test_the_summary_line_renders_after_the_file_snapshots(self):
        view = EvaluationView(
            status="in_progress", target_file="/proj/Foo.thy", destination_line=20,
            message="Evaluating towards Foo.thy:20.",
            files=[FileSnapshot("/proj/Foo.thy", lined=True, state="in_progress",
                                pending=[(7, 9)], pending_count=1)],
            unprocessed_theories=3,
        )
        assert format_evaluation_result(view, "/proj", call_to_action=False) == (
            "Evaluating towards Foo.thy:20.\n\n"
            "Foo.thy:\n  pending: lines 7-9\n\n"
            "3 theories are not yet processed."
        )
        view.unprocessed_theories = 1
        assert format_evaluation_result(view, "/proj", call_to_action=False).endswith(
            "\n\n1 theory is not yet processed."
        )

    @pytest.mark.asyncio
    async def test_a_failed_import_does_not_keep_the_evaluation_from_completing(
        self, temp_theory_file, mock_lsp_client, tmp_path,
    ):
        # _dependency_done is not theory_settled: a failed import that has
        # gone quiet is a settled old debt for the completion verdict — even
        # with commands left unprocessed behind the failure (the prover stops
        # there), which is the branch nothing else covers.
        broken = str(tmp_path / "Broken.thy")       # not on disk: the auto-open fails, the count row stays
        real_status = mock_lsp_client.request_theory_status

        async def with_failed_import():
            theories = await real_status()
            theories[0]["imports"] = [{"theory_name": "Broken"}]
            theories.append({
                "node_name": broken, "theory_name": "Broken",
                "external": True, "imports": [], "ok": False, "total": 3,
                "unprocessed": 1, "running": 0, "warned": 0, "failed": 1,
                "finished": 1, "canceled": False, "consolidated": False,
            })
            return theories

        mock_lsp_client.request_theory_status = with_failed_import
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.status == "complete" and not evaluation_state.active
        assert [fs.file_path for fs in view.files] == [temp_theory_file, broken]
        assert view.files[1].error_count == 1 and not view.files[1].lined


class TestCompletionSentence:
    """One completion vocabulary (problem 8, D-B2): internal complete ⇒ the
    COMPLETED sentence at every outlet, whatever failed or still runs. The
    wording holds no second completion judgement."""

    @pytest.mark.asyncio
    async def test_completed_with_failures_says_completed(
        self, temp_theory_file, mock_lsp_client,
    ):
        mock_lsp_client._processing_trackers[temp_theory_file] = (
            MockProcessingTracker(overview_error=[(2, 0, 2, 5)])
        )
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.status == "complete"
        assert view.message == f"Evaluation has completed up to {temp_theory_file}:5."
        # The failure is not hidden: it is below, per line, in the file section.
        assert _file(view, temp_theory_file).errors == [(3, 3)]

    @pytest.mark.asyncio
    async def test_completed_with_a_command_running_past_the_target_says_completed(
        self, temp_theory_file, mock_lsp_client,
    ):
        # Running beyond the destination: the run *to the target* is done;
        # saying "arrived" would deny the agent its terminal sentence forever.
        # The running command keeps its running: row in the file section.
        mock_lsp_client._processing_trackers[temp_theory_file] = (
            MockProcessingTracker(running=[(9, 0, 9, 4)])
        )
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.status == "complete"
        assert view.message == f"Evaluation has completed up to {temp_theory_file}:5."
        assert _file(view, temp_theory_file).running == [(10, 10)]

    @pytest.mark.asyncio
    async def test_a_successor_run_cannot_hijack_the_reply(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        # 10A must-fix 1: while this call waited, a concurrent status observed
        # the completion, and the agent started a run on ANOTHER file. The
        # reply stays this run's — complete, with its own target line — and
        # the successor is left unstamped and running.
        async def loop_then_successor(client, file_path, state, evaluation, timeout):
            state.complete()
            state.start("/tmp/Other_successor.thy", MCPLine(3))
            return "complete", [], []

        monkeypatch.setattr(ev, "_evaluation_wait_loop", loop_then_successor)
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.status == "complete"
        assert view.message == f"Evaluation has completed up to {temp_theory_file}:5."
        assert view.destination_line == 5
        successor = evaluation_state.current
        assert evaluation_state.active and successor is not None
        assert evaluation_state.file_path == "/tmp/Other_successor.thy"
        assert successor.outcome == ""


class TestRendering:
    def test_render_absolute_and_relative(self):
        view = EvaluationView(
            status="complete", destination_line=7,
            message="Evaluation complete, arrived at line 7.",
            files=[
                FileSnapshot("/proj/Foo.thy", lined=True, state="problems",
                             errors=[(5, 5), (9, 11)], error_count=2),
                FileSnapshot("/proj/Bar.thy", lined=True, state="clean"),
            ],
        )
        rel = format_evaluation_result(view, "/proj")
        assert "Foo.thy:" in rel
        assert "errors: lines 5, 9-11" in rel
        assert "Bar.thy: clean" in rel
        absolute = format_evaluation_result(view, None)
        assert "/proj/Foo.thy:" in absolute

    def test_render_no_evaluation(self):
        view = EvaluationView(status="no_evaluation", message="No evaluation in progress.")
        assert format_evaluation_result(view, None) == "No evaluation in progress."

    def test_render_the_sorry_row_after_errors_with_no_call_to_action(self):
        # The approved row: `sorry: line 5` / `sorry: lines 12, 40`, after the
        # errors row; a sorry alone earns no call to action.
        view = EvaluationView(
            status="complete", target_file="/proj/Foo.thy", destination_line=42,
            message="Evaluation has completed up to Foo.thy:42.",
            files=[FileSnapshot("/proj/Foo.thy", lined=True, state="problems",
                                errors=[(37, 37), (40, 41)], sorry=[(12, 12)],
                                error_count=2)],
        )
        assert format_evaluation_result(view, "/proj") == (
            "Evaluation has completed up to Foo.thy:42.\n\n"
            "Foo.thy:\n  errors: lines 37, 40-41\n  sorry: line 12\n\n"
            "Call isabelle_evaluation_status to check progress."
        )
        only_sorry = EvaluationView(
            status="complete", target_file="/proj/Foo.thy", destination_line=42,
            message="Evaluation has completed up to Foo.thy:42.",
            files=[FileSnapshot("/proj/Foo.thy", lined=True, state="clean",
                                sorry=[(12, 12), (40, 40)])],
        )
        assert format_evaluation_result(only_sorry, "/proj") == (
            "Evaluation has completed up to Foo.thy:42.\n\n"
            "Foo.thy:\n  sorry: lines 12, 40"
        )

    def test_render_pending_row_not_clean(self):
        view = EvaluationView(
            status="in_progress", destination_line=10,
            message="Evaluation in progress. Call evaluation_status to check progress.",
            files=[FileSnapshot("/proj/Foo.thy", lined=True, state="in_progress",
                                pending=[(7, 9)], pending_count=1)],
        )
        out = format_evaluation_result(view, "/proj")
        assert "pending: lines 7-9" in out
        assert "Foo.thy: clean" not in out


class TestResultLayout:
    """§4.6: detail nests under the row it belongs to, so nothing is said twice."""

    def _view(self, elapsed, **fs_kwargs):
        return EvaluationView(
            status="in_progress", target_file="/proj/Foo.thy", destination_line=20,
            message="Evaluating towards Foo.thy:20.",
            files=[FileSnapshot("/proj/Foo.thy", lined=True, state="in_progress",
                                running=[(8, 8)], running_count=1, **fs_kwargs)],
            running_commands=[RunningCommand(
                file_path="/proj/Foo.thy", start_line=8, end_line=8,
                text="by (auto simp: field_simps)", elapsed_seconds=elapsed,
            )],
        )

    def test_slow_command_nests_with_its_elapsed_time(self):
        out = format_evaluation_result(self._view(14.0), "/proj")
        assert "  running:\n    line 8 (14s) by (auto simp: field_simps)" in out

    def test_young_command_shows_the_range_alone(self):
        # Below the threshold it is ordinary progress, not something to act on.
        out = format_evaluation_result(self._view(3.0), "/proj")
        assert "  running: line 8" in out
        assert "(3s)" not in out

    def test_line_spans_carry_a_unit_word(self):
        # "errors: 12" reads as "12 errors"; "errors: line 12" cannot.
        single = EvaluationView(
            status="in_progress", destination_line=20,
            files=[FileSnapshot("/proj/Foo.thy", lined=True, state="problems",
                                errors=[(45, 45)], error_count=1)],
        )
        assert "errors: line 45" in format_evaluation_result(single, "/proj")
        several = EvaluationView(
            status="in_progress", destination_line=20,
            files=[FileSnapshot("/proj/Foo.thy", lined=True, state="problems",
                                errors=[(9, 11), (20, 20)], error_count=2)],
        )
        assert "errors: lines 9-11, 20" in format_evaluation_result(several, "/proj")

    def test_call_to_action_only_when_there_is_something_to_watch(self):
        call = "Call isabelle_evaluation_status to check progress."

        assert format_evaluation_result(self._view(14.0), "/proj").endswith(call)
        assert call not in format_evaluation_result(self._view(3.0), "/proj")
        # A failure is also worth watching, however briefly it has been running.
        with_error = self._view(3.0, errors=[(4, 4)], error_count=1)
        assert format_evaluation_result(with_error, "/proj").endswith(call)

    def test_evaluation_status_does_not_point_at_itself(self):
        out = format_evaluation_result(
            self._view(14.0), "/proj", call_to_action=False,
        )
        assert "Call isabelle_evaluation_status" not in out


class TestDecorationClear:
    """C2 regression: a fixed error/warning/sorry clears via an empty content push."""

    @pytest.mark.asyncio
    async def test_empty_push_clears_all_four_types(self):
        tr = ProcessingTracker()
        present = parse_decoration_ranges([
            {"type": "background_bad", "content": [{"range": [4, 0, 4, 5]}]},
            {"type": "background_sorry", "content": [{"range": [7, 2, 7, 7]}]},
            {"type": "text_overview_error", "content": [{"range": [4, 0, 4, 5]}]},
            {"type": "text_overview_warning", "content": [{"range": [6, 0, 6, 5]}]},
        ])
        await tr.update(present)
        assert tr.get_bad_ranges() and tr.get_overview_error_ranges() and tr.get_overview_warning_ranges()
        assert tr.get_sorry_ranges() == [(7, 2, 7, 7)]

        emptied = parse_decoration_ranges([
            {"type": "background_bad", "content": []},
            {"type": "background_sorry", "content": []},
            {"type": "text_overview_error", "content": []},
            {"type": "text_overview_warning", "content": []},
        ])
        await tr.update(emptied)
        assert tr.get_bad_ranges() == []
        assert tr.get_sorry_ranges() == []
        assert tr.get_overview_error_ranges() == []
        assert tr.get_overview_warning_ranges() == []

    @pytest.mark.asyncio
    async def test_reset_forgets_the_sorry_ranges_too(self):
        tr = ProcessingTracker()
        await tr.update(parse_decoration_ranges([
            {"type": "background_sorry", "content": [{"range": [7, 2, 7, 7]}]},
        ]))
        await tr.reset()
        assert tr.get_sorry_ranges() == []


class TestLatchRegression:
    """The 0.1.1 latch, pinned at the level it occurred: evaluate_to with a REAL
    tracker must complete when no decoration push follows the edit (the server
    re-sends nothing when decorations are unchanged). Pre-fix code waited for a
    strictly-newer push and reported in_progress forever."""

    @pytest.mark.asyncio
    async def test_evaluate_completes_without_new_push_after_edit(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        from isabelle_mcp import processing

        monkeypatch.setattr(processing, "DECORATION_GRACE", 0.1)
        monkeypatch.setattr(processing, "_last_edit_sent", float("-inf"))
        monkeypatch.setattr(ev, "EVAL_POLL_INTERVAL", 5.0)

        tracker = ProcessingTracker()
        await tracker.update(
            {"background_unprocessed1": [], "background_running1": []},
        )
        processing.note_edit_sent()  # an edit went out; no push will ever follow
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker

        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.status == "complete"


class TestCancelSafety:
    """v4 cancel-safety regression guards: a CancelledError on ANY evaluate_to
    await must reset evaluation_state.active (not wedge the :523 guard). Each
    test reds on the pre-v4 code (`except Exception` misses CancelledError /
    reset-after-await / grace re-check outside the try)."""

    @pytest.mark.asyncio
    async def test_cancel_in_first_wait_loop_resets_state(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        import asyncio

        async def fake_loop(client, file_path, state, evaluation, timeout):
            raise asyncio.CancelledError()

        monkeypatch.setattr(ev, "_evaluation_wait_loop", fake_loop)
        with pytest.raises(asyncio.CancelledError):
            await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        # except BaseException ran: active reset.
        assert evaluation_state.active is False

    @pytest.mark.asyncio
    async def test_cancel_in_grace_recheck_resets_state(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        import asyncio
        import os

        from isabelle_mcp import processing

        mock_lsp_client.heap_sources = {os.path.realpath(temp_theory_file)}
        monkeypatch.setattr(processing, "DECORATION_GRACE", 100.0)
        processing.note_edit_sent()  # open the grace window → _grace_remaining() > 0

        calls = []

        async def fake_loop(client, file_path, state, evaluation, timeout):
            calls.append(1)
            if len(calls) == 1:
                return "in_progress", [], []
            raise asyncio.CancelledError()

        monkeypatch.setattr(ev, "_evaluation_wait_loop", fake_loop)
        with pytest.raises(asyncio.CancelledError):
            await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        # The cancel landed on the SECOND (grace re-check) wait loop, now inside the
        # try; 改动 C/F1 resets state instead of leaking active=True.
        assert calls == [1, 1]
        assert evaluation_state.active is False

    @pytest.mark.asyncio
    async def test_cancel_evaluation_resets_state_when_force_interrupt_cancelled(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        import asyncio

        from isabelle_mcp.evaluation import cancel_evaluation

        evaluation_state.start(temp_theory_file, MCPLine(5))  # active=True, in progress

        async def boom():
            raise asyncio.CancelledError()

        monkeypatch.setattr(mock_lsp_client, "force_interrupt", boom)
        with pytest.raises(asyncio.CancelledError):
            await cancel_evaluation(mock_lsp_client)
        # S3: the try/finally reset state even though force_interrupt was cancelled
        # before evaluation_state.cancel() could run (else active wedges every later
        # evaluate_to).
        assert evaluation_state.active is False


class TestLockedSync:
    @pytest.mark.asyncio
    async def test_sync_file_locked_pushes_single_path(self, mock_lsp_client):
        synced: list[set[str]] = []

        async def fake_sync(dirty):
            synced.append(dirty)

        mock_lsp_client.sync_dirty_files = fake_sync
        await sync_file_locked(mock_lsp_client, "/tmp/Foo.thy")
        assert synced == [{"/tmp/Foo.thy"}]

    @pytest.mark.asyncio
    async def test_sync_file_locked_pushes_even_when_evaluation_active(
        self, temp_theory_file, mock_lsp_client,
    ):
        synced: list[set[str]] = []

        async def fake_sync(dirty):
            synced.append(dirty)

        mock_lsp_client.sync_dirty_files = fake_sync
        evaluation_state.start(temp_theory_file, MCPLine(5))
        try:
            assert evaluation_state.active
            await sync_file_locked(mock_lsp_client, temp_theory_file)
            assert synced == [{temp_theory_file}]
        finally:
            evaluation_state.cancel()

    @pytest.mark.asyncio
    async def test_resync_locked_delegates_to_client(self, mock_lsp_client):
        called = {"n": 0}

        async def fake_resync():
            called["n"] += 1

        mock_lsp_client.resync_changed_open_documents = fake_resync
        await resync_changed_open_documents_locked(mock_lsp_client)
        assert called["n"] == 1


class TestDependencyEditStamp:
    """Layer-3: an external import/.ML file changing on disk is an edit too
    (the server's own File_Watcher will didChange it internally) — detection
    must bump the global edit clock."""

    @pytest.mark.asyncio
    async def test_external_dep_change_bumps_edit_clock(
        self, mock_lsp_client, tmp_path, monkeypatch,
    ):
        from isabelle_mcp import processing

        dep = tmp_path / "Helper.ML"
        dep.write_text("val x = 1;")
        mock_lsp_client._dep_stat_sigs = {}
        mock_lsp_client.vscode_load_delay = 0.5

        async def theory_status():
            return [{"node_name": str(dep), "external": True}]

        mock_lsp_client.request_theory_status = theory_status
        monkeypatch.setattr(processing, "_last_edit_sent", float("-inf"))

        await ev._dependency_freshness_wait(mock_lsp_client)  # baseline stat
        assert processing._grace_remaining() == 0.0           # no change yet

        from isabelle_mcp import debugger
        debugger.registry.pop_dirty()   # isolate from earlier tests
        dep.write_text("val x = 2; (* edited externally *)")
        wait = await ev._dependency_freshness_wait(mock_lsp_client)
        assert processing._grace_remaining() > 0.0            # clock bumped
        assert wait > 0.0                                     # debounce wait requested
        # Phase D wiring: the changed blob is marked dirty for breakpoint
        # reconciliation.
        assert str(dep) in debugger.registry.pop_dirty()


class TestEvaluationLifecycle:
    """§4.8: only the run that started this state may end it, and the outcome
    belongs to the run that was ended.

    Every test here reds on the pre-handle code, where a single boolean carried
    both facts: a completed run was reported as "in progress", and a finished
    run's tail stamped a newer run."""

    def test_start_mints_a_fresh_handle_and_owns_it(self, temp_theory_file):
        first = evaluation_state.start(temp_theory_file, MCPLine(5))
        assert evaluation_state.owns(first)
        second = evaluation_state.start(temp_theory_file, MCPLine(5))
        assert first is not second
        assert evaluation_state.owns(second)
        assert not evaluation_state.owns(first)

    def test_complete_and_cancel_stamp_the_outcome(self, temp_theory_file):
        run = evaluation_state.start(temp_theory_file, MCPLine(5))
        evaluation_state.complete()
        assert run.outcome == "complete"
        assert evaluation_state.active is False

        run = evaluation_state.start(temp_theory_file, MCPLine(5))
        evaluation_state.cancel()
        assert run.outcome == "cancelled"

    def test_stamp_is_write_once(self, temp_theory_file):
        # A later cancel of a lingering fork must not rewrite a finished run's story.
        run = evaluation_state.start(temp_theory_file, MCPLine(5))
        evaluation_state.complete()
        evaluation_state.cancel()
        assert run.outcome == "complete"

    @pytest.mark.asyncio
    async def test_finish_if_owner_is_a_noop_for_a_superseded_run(
        self, temp_theory_file, mock_lsp_client,
    ):
        first = evaluation_state.start(temp_theory_file, MCPLine(5))
        second = evaluation_state.start(temp_theory_file, MCPLine(5))

        assert await ev._finish_if_owner(
            mock_lsp_client, first, "complete", judged_dest=MCPLine(5),
        ) is False
        # The second run's flag and stamp are untouched.
        assert evaluation_state.active is True
        assert second.outcome == ""

    @pytest.mark.asyncio
    async def test_finish_if_owner_stamps_the_outcome_it_is_given(
        self, temp_theory_file, mock_lsp_client,
    ):
        """§6: the outcome lands verbatim. An if/else that folds everything
        non-complete into cancel() would silently coerce the third outcome."""
        run = evaluation_state.start(temp_theory_file, MCPLine(5))
        assert await ev._finish_if_owner(
            mock_lsp_client, run, "abandoned", judged_dest=None,
        ) is True
        assert run.outcome == "abandoned"
        assert evaluation_state.active is False

    @pytest.mark.asyncio
    async def test_wait_loop_reports_the_recorded_outcome(
        self, temp_theory_file, mock_lsp_client,
    ):
        """A concurrent evaluation_status observing completion clears the flag;
        the loop must say "complete", not guess "cancelled"."""
        run = evaluation_state.start(temp_theory_file, MCPLine(5))
        evaluation_state.complete()
        status, theories, running = await ev._evaluation_wait_loop(
            mock_lsp_client, temp_theory_file, evaluation_state, run, 1.0,
        )
        assert status == "complete"

    @pytest.mark.asyncio
    async def test_wait_loop_carries_out_the_running_commands(
        self, temp_theory_file, mock_lsp_client,
    ):
        run = evaluation_state.start(temp_theory_file, MCPLine(5))
        evaluation_state.cancel()
        cmd = RunningCommand(
            file_path=temp_theory_file, start_line=3, end_line=3,
            text="by auto", elapsed_seconds=12.0,
        )
        mock_lsp_client.get_all_running_commands = lambda: [cmd]
        status, theories, running = await ev._evaluation_wait_loop(
            mock_lsp_client, temp_theory_file, evaluation_state, run, 1.0,
        )
        assert status == "cancelled"
        assert running == [cmd]

    @pytest.mark.asyncio
    async def test_evaluate_to_reports_a_concurrent_completion_as_complete(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        """The defect this fix exists for: evaluation_status observes the run
        succeed, and evaluate_to used to answer "Evaluation in progress."."""

        async def fake_loop(client, file_path, state, evaluation, timeout):
            state.complete()                       # what evaluation_status does
            return "cancelled", [], []             # what the old loop guessed

        monkeypatch.setattr(ev, "_evaluation_wait_loop", fake_loop)
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.status == "complete"
        assert view.message == f"Evaluation has completed up to {temp_theory_file}:5."
        assert "in progress" not in view.message

    @pytest.mark.asyncio
    async def test_evaluate_to_reports_a_concurrent_cancel_as_cancelled(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        async def fake_loop(client, file_path, state, evaluation, timeout):
            state.cancel()
            return "in_progress", [], []

        monkeypatch.setattr(ev, "_evaluation_wait_loop", fake_loop)
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.status == "cancelled"
        assert view.message == ev.CANCELLED_MESSAGE
        assert evaluation_state.active is False

    @pytest.mark.asyncio
    async def test_a_cancelled_heap_run_does_not_claim_the_heap_stopped_it(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        """Demotion (§4.8): "Evaluation abandoned: the file differs…" names a
        cause, so it may only be said when the run stopped on its own."""
        import os

        mock_lsp_client.heap_sources = {os.path.realpath(temp_theory_file)}

        async def fake_loop(client, file_path, state, evaluation, timeout):
            state.cancel()
            return "in_progress", [], []

        monkeypatch.setattr(ev, "_evaluation_wait_loop", fake_loop)
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.status == "cancelled"
        assert view.message == ev.CANCELLED_MESSAGE

    @pytest.mark.asyncio
    async def test_an_abandoned_evaluation_is_not_reported_as_a_cancel(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        """D-B5: after an abandonment the query tools must not fabricate
        "the evaluation was cancelled" — last_evaluation_was_cancelled()
        keys that sentence, so it must stay False."""
        import os

        mock_lsp_client.heap_sources = {os.path.realpath(temp_theory_file)}
        # Real wait loop: the file never processes, so the heap budget expires.
        mock_lsp_client._processing_trackers[temp_theory_file] = (
            MockProcessingTracker(all_processed=False)
        )
        monkeypatch.setattr(ev, "HEAP_POLL_INTERVAL", 0.05)
        with pytest.raises(IsabelleToolError):   # §6A: the decider raises D-C7
            await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert ev.evaluation_state.current is not None
        assert ev.evaluation_state.current.outcome == "abandoned"
        assert ev.last_evaluation_was_cancelled() is False

    @pytest.mark.asyncio
    async def test_a_rider_that_reads_a_peers_abandon_stamp_does_not_raise(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        """§6A landing note: only the request that made the abandonment
        decision raises D-C7. One that merely read a peer's stamp keeps the
        reporting shape — its own loop may have watched the target arrive."""
        import os

        mock_lsp_client.heap_sources = {os.path.realpath(temp_theory_file)}

        async def fake_loop(client, file_path, state, evaluation, timeout):
            state._finish("abandoned")   # a peer abandons while we wait
            return "in_progress", [], []

        monkeypatch.setattr(ev, "_evaluation_wait_loop", fake_loop)
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.status == "abandoned"
        assert "has not taken effect" in view.message   # D-C7: one event, one wording

    @pytest.mark.asyncio
    async def test_a_finished_run_does_not_stamp_a_newer_run(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        """Defect 2: evaluate_to holds no lock across the wait, so a second run
        can start before the first one's tail runs. Only a run that still owns
        the state may stamp it (mutation control: drop the ownership test in
        _finish_if_owner and run two ends here)."""
        runs = []

        async def fake_loop(client, file_path, state, evaluation, timeout):
            # A second evaluation starts while we were waiting.
            runs.append(state.start(file_path, state.destination_line))
            return "complete", [], []

        monkeypatch.setattr(ev, "_evaluation_wait_loop", fake_loop)
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)

        assert view.status == "complete"           # run one reports its own verdict
        assert evaluation_state.active is True     # run two is still in flight
        (run_two,) = runs
        assert evaluation_state.owns(run_two) and run_two.outcome == ""


class TestGuardPositionDecision:
    """§4.3: the guard judges the requested position, and waits out an
    untrustworthy cache instead of guessing in either direction."""

    @pytest.mark.asyncio
    async def test_unknown_is_waited_out_then_served(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        from isabelle_mcp import processing

        monkeypatch.setattr(processing, "DECORATION_GRACE", 0.3)
        await mock_lsp_client.open_document(temp_theory_file)
        tracker = ProcessingTracker()
        await tracker.update(
            {"background_unprocessed1": [], "background_running1": []},
        )
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        processing.note_edit_sent()          # cache is untrustworthy right now

        assert tracker.position_state(4) == processing.UNKNOWN
        carets = []
        mock_lsp_client.set_caret = (
            lambda *a, **k: carets.append(a) or asyncio.sleep(0)
        )
        # Neither a refusal nor a re-evaluation: wait the window out and answer.
        assert await ev.check_evaluation_guard(
            mock_lsp_client, temp_theory_file, MCPLine(5),
        ) is None
        # The old guard also ended at None — but by running a whole evaluate_to,
        # which moves the caret. That is the behaviour this clause removes.
        assert carets == []
        assert evaluation_state.active is False

    @pytest.mark.asyncio
    async def test_stale_running_range_in_the_grace_window_is_not_served(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        """The agent edits a line the prover is executing, then queries it.

        A `running` range from before the edit describes a command that no longer
        exists. Serving it hands back pre-edit output with a note claiming a fork
        is still executing; the freshness test has to come first."""
        from isabelle_mcp import processing

        monkeypatch.setattr(processing, "DECORATION_GRACE", 100.0)
        await mock_lsp_client.open_document(temp_theory_file)
        tracker = ProcessingTracker()
        await tracker.update({
            "background_unprocessed1": [], "background_running1": [(4, 0, 4, 20)],
            "background_canceled": [],
        })
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        processing.note_edit_sent()          # the edit the agent just made

        assert tracker.position_state(4) == processing.UNKNOWN
        assert tracker.position_state(4) != processing.RUNNING

    @pytest.mark.asyncio
    async def test_stale_canceled_range_in_the_grace_window_is_not_served(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        from isabelle_mcp import processing

        monkeypatch.setattr(processing, "DECORATION_GRACE", 100.0)
        tracker = ProcessingTracker()
        await tracker.update({
            "background_unprocessed1": [], "background_running1": [],
            "background_canceled": [(4, 0, 4, 20)],
        })
        processing.note_edit_sent()
        assert tracker.position_state(4) == processing.UNKNOWN

    @pytest.mark.asyncio
    async def test_unprocessed_stays_definite_in_the_grace_window(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        """The one scan that may stay in front: an edit can only un-process a
        line, and the caller's response to NOT_EVALUATED is to evaluate it."""
        from isabelle_mcp import processing

        monkeypatch.setattr(processing, "DECORATION_GRACE", 100.0)
        tracker = ProcessingTracker()
        await tracker.update({"background_unprocessed1": [(4, 0, 4, 20)]})
        processing.note_edit_sent()
        assert tracker.position_state(4) == processing.NOT_EVALUATED

    @pytest.mark.asyncio
    async def test_interrupted_position_is_served_with_a_note(
        self, mock_lsp_client, temp_theory_file,
    ):
        await mock_lsp_client.open_document(temp_theory_file)
        tracker = ProcessingTracker()
        await tracker.update({
            "background_unprocessed1": [], "background_running1": [],
            "background_canceled": [(4, 0, 4, 20)],
        })
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker

        note = await ev.check_evaluation_guard(
            mock_lsp_client, temp_theory_file, MCPLine(5),
        )
        assert note == (
            f"The evaluation of the command at {temp_theory_file}:5 was "
            "interrupted; its output may be incomplete."
        )

    @pytest.mark.asyncio
    async def test_still_unknown_after_the_wait_is_refused_not_evaluated(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        from isabelle_mcp import processing

        class AlwaysUnknown:
            def position_state(self, line):
                return processing.UNKNOWN

            async def wait_until_line_reached_bounded(self, *a, **k):
                return False

        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client._processing_trackers[temp_theory_file] = AlwaysUnknown()
        monkeypatch.setattr(ev, "_grace_remaining", lambda: 0.05)

        with pytest.raises(IsabelleToolError, match="Cannot tell whether") as exc:
            await ev.check_evaluation_guard(
                mock_lsp_client, temp_theory_file, MCPLine(5),
            )
        # Must not claim the line was not reached, and must not auto-start.
        assert "not been evaluated" not in str(exc.value)
        assert evaluation_state.active is False

    @pytest.mark.asyncio
    async def test_every_query_tool_is_now_judged_by_position_alone(
        self, mock_lsp_client, temp_theory_file,
    ):
        """§4.4: the two caret-moving tools used to be refused outright while an
        evaluation ran, because they and the evaluation both wrote the global
        caret. Part B removed the caret from both, so the guard has one rule for
        all six tools and takes no flag saying which one is asking."""
        import inspect

        await mock_lsp_client.open_document(temp_theory_file)   # processed
        evaluation_state.start(temp_theory_file, MCPLine(100))

        assert await ev.check_evaluation_guard(
            mock_lsp_client, temp_theory_file, MCPLine(5),
        ) is None
        assert "moves_caret" not in inspect.signature(ev.check_evaluation_guard).parameters


class TestTargetOnTheResultModel:
    """§1: the result model carried a destination line and no target file at all."""

    @pytest.mark.asyncio
    async def test_evaluate_to_names_the_target_file(
        self, temp_theory_file, mock_lsp_client,
    ):
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.target_file == temp_theory_file
        assert view.destination_line == 5

    @pytest.mark.asyncio
    async def test_status_drops_the_target_once_no_run_owns_it(
        self, temp_theory_file, mock_lsp_client,
    ):
        # A lingering fork has no target: the evaluation that had one is over.
        cmd = RunningCommand(
            file_path=temp_theory_file, start_line=5, end_line=5,
            text="by auto", elapsed_seconds=3.0,
        )
        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client.get_all_running_commands = lambda: [cmd]
        evaluation_state.start(temp_theory_file, MCPLine(5))
        evaluation_state.complete()
        view = await evaluation_status(mock_lsp_client)
        assert view.target_file is None
        assert view.message == "1 command is still running."


class TestPluralWording:
    """§4.2 bans `command(s)`; the branch that matters is the one above 1."""

    def test_two_running_and_two_failed(self):
        cmds = [
            RunningCommand(file_path="/p/A.thy", start_line=i, end_line=i,
                           text="by auto", elapsed_seconds=42.0)
            for i in (3, 8)
        ]
        assert ev._activity_sentences(cmds, 2) == [
            "2 commands have been running for over 10s.",
            "2 failed commands remain.",
        ]

    def test_one_of_each(self):
        cmd = RunningCommand(file_path="/p/A.thy", start_line=3, end_line=3,
                             text="by auto", elapsed_seconds=42.0)
        assert ev._activity_sentences([cmd], 1) == [
            "1 command has been running for over 10s.",
            "1 failed command remains.",
        ]

    def test_still_running_sentence_agrees_with_its_verb(self):
        assert ev._still_running_sentence(1) == "1 command is still running."
        assert ev._still_running_sentence(2) == "2 commands are still running."

    def test_the_summary_line_agrees_with_its_verb(self):
        assert ev.unprocessed_theories_sentence(1) == "1 theory is not yet processed."
        assert ev.unprocessed_theories_sentence(2) == "2 theories are not yet processed."

    def test_line_span_unit_word_pluralises(self):
        assert ev._fmt_spans([(5, 5)]) == "line 5"
        assert ev._fmt_spans([(5, 7)]) == "lines 5-7"
        assert ev._fmt_spans([(5, 5), (9, 9)]) == "lines 5, 9"


class TestEvaluationFooter:
    """§4.2: one line of ambient context on every query-tool result."""

    async def _client(self, mock_lsp_client, temp_theory_file, **ranges):
        await mock_lsp_client.open_document(temp_theory_file)
        tracker = ProcessingTracker()
        await tracker.update({
            "background_unprocessed1": ranges.get("unprocessed", []),
            "background_running1": ranges.get("running", []),
            "background_bad": ranges.get("bad", []),
            "text_overview_error": ranges.get("overview_error", []),
        })
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        return mock_lsp_client

    def _slow(self, path):
        return RunningCommand(
            file_path=path, start_line=8, end_line=8,
            text="by auto", elapsed_seconds=14.0,
        )

    @pytest.mark.asyncio
    async def test_nothing_outstanding_and_nothing_running_says_nothing(
        self, mock_lsp_client, temp_theory_file,
    ):
        client = await self._client(mock_lsp_client, temp_theory_file)
        assert await ev.evaluation_footer(client) == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outlet", [
        "no_target", "towards", "arrived", "completed", "import_not_done",
    ])
    async def test_a_live_hit_appends_the_pause_line_at_every_outlet(
        self, mock_lsp_client, temp_theory_file, monkeypatch, outlet,
    ):
        """D-B15 (revised): every outlet keeps its status line minus the
        running sentence; the pause line is appended as a second line."""
        from isabelle_mcp import debugger

        lead = "Breakpoint hit: h1. The affected evaluation is paused."
        pause_line = lead + " " + ev.FOOTER_HIT_DETAILS_CALL
        monkeypatch.setattr(debugger, "paused_lead", lambda client: lead)
        ranges = {
            "no_target": {},
            "towards": {"unprocessed": [(5, 0, 30, 0)]},
            "arrived": {"running": [(2, 0, 2, 9)]},
            "completed": {"overview_error": [(3, 0, 3, 9)], "bad": [(3, 0, 3, 9)]},
            "import_not_done": {},
        }[outlet]
        client = await self._client(mock_lsp_client, temp_theory_file, **ranges)
        client.get_all_running_commands = lambda: [self._slow(temp_theory_file)]
        if outlet == "import_not_done":
            real_status = client.request_theory_status

            async def with_unfinished_import():
                theories = await real_status()
                theories[0]["imports"] = [{"theory_name": "Dep_import"}]
                theories.append({
                    "node_name": "/tmp/Dep_import.thy",
                    "theory_name": "Dep_import", "imports": [], "ok": True,
                    "consolidated": False, "running": 1, "unprocessed": 5,
                })
                return theories

            client.request_theory_status = with_unfinished_import
        if outlet != "no_target":
            target = 20 if outlet == "towards" else 5
            evaluation_state.start(temp_theory_file, MCPLine(target))

        footer = await ev.evaluation_footer(client)
        assert footer == pause_line or footer.endswith("\n" + pause_line)
        assert "has been running" not in footer
        if outlet == "towards":
            # The ruling's worked example, verbatim.
            assert footer == (
                f"Evaluating towards {temp_theory_file}:20.\n" + pause_line)
        if outlet == "completed":
            # The failure suffix survives: it is a dependency's only
            # notification on a query result.
            assert footer.split("\n")[0] == (
                f"Evaluation has completed up to {temp_theory_file}:5. "
                "1 failed command remains. Call isabelle_evaluation_status for details."
            )

    @pytest.mark.asyncio
    async def test_nothing_outstanding_but_work_running_drops_the_main_sentence(
        self, mock_lsp_client, temp_theory_file,
    ):
        # "Nothing is under evaluation." + "1 command has been running…" would
        # argue with itself, so only the activity is reported. The failures
        # that still stand are reported as the state they are — with no run
        # behind the footer as much as with one.
        client = await self._client(
            mock_lsp_client, temp_theory_file,
            overview_error=[(4, 0, 4, 9)], bad=[(4, 0, 4, 9)],
        )
        client.get_all_running_commands = lambda: [self._slow(temp_theory_file)]
        assert await ev.evaluation_footer(client) == (
            "1 command has been running for over 10s. 1 failed command remains. "
            "Call isabelle_evaluation_status for details."
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outlet", ["completed", "arrived", "towards"])
    async def test_the_count_is_pinned_to_the_theory_status_the_verdict_was_judged_on(
        self, mock_lsp_client, temp_theory_file, tmp_path, outlet,
    ):
        """Item 14 (a): with a run outstanding, the failure count comes from
        the SAME freshly parsed theory_status the verdict is judged on — a
        dependency whose failure is known only to theory_status (no
        decoration yet) counts — never from the entry stash, which is set
        here to a clean list so the two sources diverge. The idle status line
        agrees with the footer. (Mutation control: read client.entry_theories
        at any of the three fresh-read sites and its case reds.)"""
        from isabelle_mcp.evaluation import _parse_theory_status
        # "towards" here is the branch AFTER the theory_status round trip: the
        # target line is reached but an import is still running, so the
        # frontier is not (the not-reached branch before the round trip reads
        # the entry stash by design).
        ranges = {"completed": {}, "arrived": {"running": [(2, 0, 2, 9)]},
                  "towards": {}}[outlet]
        client = await self._client(mock_lsp_client, temp_theory_file, **ranges)
        dep = str(tmp_path / "Dep_status_only.thy")        # not on disk, no tracker
        real_status = client.request_theory_status

        async def with_failed_dep():
            theories = await real_status()
            theories.append({
                "node_name": dep, "theory_name": "Dep_status_only", "external": True,
                "imports": [], "ok": False, "total": 3, "unprocessed": 0, "running": 0,
                "warned": 0, "failed": 1, "finished": 2, "canceled": False,
                "consolidated": True,
            })
            if outlet == "towards":
                theories[0]["imports"] = [{"theory_name": "Dep_import"}]
                theories.append({
                    "node_name": "/tmp/Dep_import.thy", "theory_name": "Dep_import",
                    "imports": [], "ok": True, "consolidated": False,
                    "running": 1, "unprocessed": 5,
                })
            return theories

        client.request_theory_status = with_failed_dep
        client.entry_theories = [_parse_theory_status(t) for t in await real_status()]
        evaluation_state.start(temp_theory_file, MCPLine(5))
        footer = await ev.evaluation_footer(client)
        lead = {
            "completed": f"Evaluation has completed up to {temp_theory_file}:5.",
            "arrived": f"Evaluation has arrived at {temp_theory_file}:5.",
            "towards": f"Evaluating towards {temp_theory_file}:5.",
        }[outlet]
        assert footer == (
            f"{lead} 1 failed command remains. Call isabelle_evaluation_status for details."
        )
        if outlet == "completed":
            view = await evaluation_status(client)
            assert view.message == (
                "No evaluation in progress. Nothing is running, but 1 failed command remains."
            )
            assert [(fs.file_path, fs.lined, fs.error_count) for fs in view.files] == [
                (dep, False, 1),
            ]

    @pytest.mark.asyncio
    async def test_the_ambient_count_comes_from_the_entry_theory_status(
        self, mock_lsp_client, temp_theory_file,
    ):
        # No run, nothing open with an error — but the entry theory_status
        # knows of a failed dependency: the footer counts it, without a round
        # trip (a request here would surface in the mock's theory listing).
        from isabelle_mcp.evaluation import _parse_theory_status
        client = await self._client(mock_lsp_client, temp_theory_file)
        client.get_all_running_commands = lambda: [self._slow(temp_theory_file)]
        client.entry_theories = [_parse_theory_status({
            "node_name": "/lib/Broken.thy", "theory_name": "Broken",
            "external": True, "ok": False, "total": 2, "unprocessed": 0,
            "running": 0, "failed": 1, "finished": 1, "consolidated": True,
        })]
        assert await ev.evaluation_footer(client) == (
            "1 command has been running for over 10s. 1 failed command remains. "
            "Call isabelle_evaluation_status for details."
        )

    @pytest.mark.asyncio
    async def test_target_not_reached(self, mock_lsp_client, temp_theory_file):
        client = await self._client(
            mock_lsp_client, temp_theory_file,
            unprocessed=[(5, 0, 30, 0)],
            overview_error=[(3, 0, 3, 9)], bad=[(3, 0, 3, 9)],
        )
        client.get_all_running_commands = lambda: [self._slow(temp_theory_file)]
        evaluation_state.start(temp_theory_file, MCPLine(20))
        assert await ev.evaluation_footer(client) == (
            f"Evaluating towards {temp_theory_file}:20. "
            "1 command has been running for over 10s. 1 failed command remains. "
            "Call isabelle_evaluation_status for details."
        )

    @pytest.mark.asyncio
    async def test_stale_cache_reports_the_target_and_no_counts(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        from isabelle_mcp import processing

        client = await self._client(mock_lsp_client, temp_theory_file)
        client.get_all_running_commands = lambda: [self._slow(temp_theory_file)]
        evaluation_state.start(temp_theory_file, MCPLine(20))
        monkeypatch.setattr(processing, "DECORATION_GRACE", 100.0)
        processing.note_edit_sent()
        # The counts would come from the same cache that is not trusted here.
        assert await ev.evaluation_footer(client) == (
            f"Evaluating towards {temp_theory_file}:20."
        )

    @pytest.mark.asyncio
    async def test_arrived_with_work_left(self, mock_lsp_client, temp_theory_file):
        # The running command sits INSIDE the evaluated prefix, so the run is
        # arrived-not-complete; one running past the target would be complete
        # (see TestCompletionSentence).
        client = await self._client(
            mock_lsp_client, temp_theory_file, running=[(2, 0, 2, 9)],
        )
        client.get_all_running_commands = lambda: [self._slow(temp_theory_file)]
        evaluation_state.start(temp_theory_file, MCPLine(5))
        assert await ev.evaluation_footer(client) == (
            f"Evaluation has arrived at {temp_theory_file}:5. "
            "1 command has been running for over 10s. "
            "Call isabelle_evaluation_status for details."
        )

    @pytest.mark.asyncio
    async def test_completion_with_failures_carries_the_failure_suffix(
        self, mock_lsp_client, temp_theory_file,
    ):
        # D-C3: the footer hangs on another query's result, which has no file
        # sections — this suffix is the failure count's last chance to appear
        # with its run (and a closed dependency's only notification).
        client = await self._client(
            mock_lsp_client, temp_theory_file,
            overview_error=[(3, 0, 3, 9)], bad=[(3, 0, 3, 9)],
        )
        run = evaluation_state.start(temp_theory_file, MCPLine(5))
        assert await ev.evaluation_footer(client) == (
            f"Evaluation has completed up to {temp_theory_file}:5. "
            "1 failed command remains. Call isabelle_evaluation_status for details."
        )
        assert run.outcome == "complete" and not evaluation_state.active

    @pytest.mark.asyncio
    async def test_an_import_not_done_says_towards(
        self, mock_lsp_client, temp_theory_file,
    ):
        # D-B3's headline fix: the target line's decoration is reached, but an
        # import is still checking, so the frontier is not. The old footer said
        # a hollow "arrived"; the truth is "towards", and the run is unstamped.
        client = await self._client(mock_lsp_client, temp_theory_file)
        real_status = client.request_theory_status

        async def with_unfinished_import():
            theories = await real_status()
            theories[0]["imports"] = [{"theory_name": "Dep_import"}]
            theories.append({
                "node_name": "/tmp/Dep_import.thy", "theory_name": "Dep_import",
                "imports": [], "ok": True, "consolidated": False,
                "running": 1, "unprocessed": 5,
            })
            return theories

        client.request_theory_status = with_unfinished_import
        run = evaluation_state.start(temp_theory_file, MCPLine(5))
        assert await ev.evaluation_footer(client) == (
            f"Evaluating towards {temp_theory_file}:5."
        )
        assert evaluation_state.active and run.outcome == ""

    @pytest.mark.asyncio
    async def test_a_dependency_failure_is_counted_and_the_dependency_stays_open(
        self, mock_lsp_client, temp_theory_file, tmp_path,
    ):
        # A failed dependency the status report opened: its failure is in the
        # suffix, and the completion closes nothing — the file stays open, and
        # reported, until it is fixed (only the unified close closes files).
        client = await self._client(mock_lsp_client, temp_theory_file)
        dep = str(tmp_path / "Dep_footer2.thy")
        Path(dep).write_text("theory Dep_footer2\nimports Main\nbegin\nx\nend\n")
        await client.open_document(dep)
        client._processing_trackers[dep] = MockProcessingTracker(
            overview_error=[(3, 0, 3, 1)], bad=[(3, 0, 3, 1)],
        )
        evaluation_state.start(temp_theory_file, MCPLine(5))
        assert await ev.evaluation_footer(client) == (
            f"Evaluation has completed up to {temp_theory_file}:5. "
            "1 failed command remains. Call isabelle_evaluation_status for details."
        )
        assert dep in client.open_documents

    @pytest.mark.asyncio
    async def test_completion_is_observed_and_ends_the_evaluation(
        self, mock_lsp_client, temp_theory_file,
    ):
        """The one case that costs a request — and the only place a quietly
        finished evaluation is noticed without anyone polling."""
        client = await self._client(mock_lsp_client, temp_theory_file)
        run = evaluation_state.start(temp_theory_file, MCPLine(5))

        assert await ev.evaluation_footer(client) == (
            f"Evaluation has completed up to {temp_theory_file}:5."
        )
        assert evaluation_state.active is False
        assert run.outcome == "complete"

    @pytest.mark.asyncio
    async def test_the_footer_never_opens_a_document(
        self, mock_lsp_client, temp_theory_file,
    ):
        """It must not go through _build_status_snapshot, which auto-opens
        failed theories as a side effect."""
        client = await self._client(mock_lsp_client, temp_theory_file)
        evaluation_state.start(temp_theory_file, MCPLine(5))

        async def fail_theory_status():
            return [{
                "node_name": "/tmp/Broken_footer.thy", "theory_name": "Broken",
                "external": False, "imports": [], "ok": False, "total": 1,
                "unprocessed": 0, "running": 0, "warned": 0, "failed": 1,
                "finished": 0, "canceled": False, "consolidated": True,
                "percentage": 100,
            }]

        client.request_theory_status = fail_theory_status
        await ev.evaluation_footer(client)
        assert "/tmp/Broken_footer.thy" not in client.open_documents


class TestForceInterruptContract:
    """force_interrupt admits only the two success outcomes; everything else
    is the catastrophe."""

    def _client(self, monkeypatch, reply):
        from unittest.mock import AsyncMock
        from isabelle_mcp.lsp_client import IsabelleLSPClient
        client = IsabelleLSPClient.__new__(IsabelleLSPClient)
        client.request = AsyncMock(**reply)
        monkeypatch.setattr("isabelle_mcp.lsp_client.note_edit_sent", lambda: None)
        return client

    @pytest.mark.asyncio
    async def test_unknown_outcome_is_a_catastrophe(self, monkeypatch):
        from isabelle_mcp.utils import IsabelleCatastrophe
        client = self._client(monkeypatch, {"return_value": {"outcome": "degraded"}})
        with pytest.raises(IsabelleCatastrophe, match="degraded"):
            await client.force_interrupt()

    @pytest.mark.asyncio
    async def test_aborted_reply_is_a_catastrophe(self, monkeypatch):
        from isabelle_mcp.utils import IsabelleCatastrophe
        client = self._client(monkeypatch, {
            "return_value": {"outcome": "aborted", "reason": "budget exhausted"}})
        with pytest.raises(IsabelleCatastrophe, match="budget exhausted"):
            await client.force_interrupt()

    @pytest.mark.asyncio
    async def test_transport_failure_is_a_catastrophe(self, monkeypatch):
        from isabelle_mcp.utils import IsabelleCatastrophe
        client = self._client(monkeypatch, {
            "side_effect": IsabelleToolError("LSP request timed out")})
        with pytest.raises(IsabelleCatastrophe, match="timed out"):
            await client.force_interrupt()


class TestCancelCatastrophe:
    """Every non-success exit of cancel_evaluation leaves as IsabelleCatastrophe
    (the tool boundary tears the prover down), with the state reset; a genuine
    cancellation of the tool call passes through untouched."""

    @pytest.mark.asyncio
    async def test_catastrophe_propagates_with_state_reset(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        from isabelle_mcp.utils import IsabelleCatastrophe
        evaluation_state.start(temp_theory_file, MCPLine(5))

        async def _aborted():
            raise IsabelleCatastrophe("cancellation aborted: budget exhausted")

        monkeypatch.setattr(mock_lsp_client, "force_interrupt", _aborted)
        with pytest.raises(IsabelleCatastrophe, match="budget exhausted"):
            await cancel_evaluation(mock_lsp_client)
        assert not evaluation_state.active
        assert mock_lsp_client.process is not None   # the boundary tears down, not here

    @pytest.mark.asyncio
    async def test_total_budget_is_a_catastrophe(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        import asyncio
        from isabelle_mcp import evaluation as ev
        from isabelle_mcp.utils import IsabelleCatastrophe
        evaluation_state.start(temp_theory_file, MCPLine(5))

        async def _slow():
            await asyncio.sleep(5)
            return {"outcome": "nothing_running"}

        monkeypatch.setattr(ev, "CANCEL_TOTAL_BUDGET", 0.05)
        monkeypatch.setattr(mock_lsp_client, "force_interrupt", _slow)
        with pytest.raises(IsabelleCatastrophe, match="budget"):
            await cancel_evaluation(mock_lsp_client)
        assert not evaluation_state.active

    @pytest.mark.asyncio
    async def test_wrap_up_failure_is_a_catastrophe(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        from isabelle_mcp import debugger
        from isabelle_mcp.utils import IsabelleCatastrophe
        evaluation_state.start(temp_theory_file, MCPLine(5))

        async def _boom(client, marked, payload):
            raise RuntimeError("sweep exploded")

        monkeypatch.setattr(debugger, "finish_cancel_sweep", _boom)
        with pytest.raises(IsabelleCatastrophe, match="sweep exploded"):
            await cancel_evaluation(mock_lsp_client)
        assert not evaluation_state.active

    @pytest.mark.asyncio
    async def test_retired_reply_renders_lists(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        from isabelle_mcp import evaluation as ev
        evaluation_state.start(temp_theory_file, MCPLine(5))

        async def _retired():
            return {
                "outcome": "retired",
                "retired": [{"file": temp_theory_file, "line": 3, "command": "lemma"}],
                "excluded": [{"file": temp_theory_file, "id": 7, "reason": "gone"}],
                "waived": [],
                "unloaded_from": [],
            }

        monkeypatch.setattr(mock_lsp_client, "force_interrupt", _retired)
        result = await cancel_evaluation(mock_lsp_client)
        lines = result.message.split("\n")
        assert lines[0] == ev.CANCEL_MESSAGES["retired"]
        assert lines[1].startswith("Reset to unevaluated: ") and ":3 (lemma)" in lines[1]
        assert len(lines) == 2          # excluded commands are logged, not rendered
        assert mock_lsp_client.process is not None   # no teardown on success


class TestEvaluationTargetWhitelist:
    """R-D3 (v): only .thy is an evaluation target; .ML/.sml redirect to a
    unique load command; other suffixes are refused; queries never redirect."""

    @pytest.mark.asyncio
    async def test_other_suffix_refused(self, tmp_path, mock_lsp_client):
        from isabelle_mcp import evaluation as ev
        bib = str(tmp_path / "refs.bib")
        with pytest.raises(IsabelleToolError) as exc:
            await evaluate_to(mock_lsp_client, bib, 1)
        assert str(exc.value) == ev.NON_THEORY_TARGET.format(file=bib)
        assert bib not in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_lowercase_ml_is_not_a_load_target(self, tmp_path, mock_lsp_client):
        from isabelle_mcp import evaluation as ev
        ml = str(tmp_path / "x.ml")
        with pytest.raises(IsabelleToolError) as exc:
            await evaluate_to(mock_lsp_client, ml, 1)
        assert str(exc.value) == ev.NON_THEORY_TARGET.format(file=ml)

    @pytest.mark.asyncio
    async def test_ml_with_unique_loader_without_line_generic(
        self, tmp_path, mock_lsp_client,
    ):
        ml = str(tmp_path / "Foo.ML")
        mock_lsp_client.loaders = [
            {"file": str(tmp_path / "A.thy"), "command": "ML_file",
             "state": "unevaluated"},                  # no "line": not a model yet
        ]
        with pytest.raises(IsabelleToolError) as exc:
            await evaluate_to(mock_lsp_client, ml, 1)
        assert str(exc.value) == (
            f"{ml} is a .ML file, which cannot be an evaluation target.")

    @pytest.mark.asyncio
    async def test_ml_without_loader_generic(self, tmp_path, mock_lsp_client):
        ml = str(tmp_path / "Foo.ML")
        mock_lsp_client.loaders = []
        with pytest.raises(IsabelleToolError) as exc:
            await evaluate_to(mock_lsp_client, ml, 1)
        assert str(exc.value) == (
            f"{ml} is a .ML file, which cannot be an evaluation target.")

    @pytest.mark.asyncio
    async def test_sml_with_two_loaders_points(self, tmp_path, mock_lsp_client):
        sml = str(tmp_path / "Foo.sml")
        a, b = str(tmp_path / "A.thy"), str(tmp_path / "B.thy")
        mock_lsp_client.loaders = [
            {"file": a, "line": 4, "command": "SML_file", "state": "unevaluated"},
            {"file": b, "line": 9, "command": "SML_file", "state": "unevaluated"},
        ]
        with pytest.raises(IsabelleToolError) as exc:
            await evaluate_to(mock_lsp_client, sml, 1)
        assert str(exc.value) == (
            f"{sml} is a .sml file, which cannot be an evaluation target. "
            f"Evaluate to {a}:4 or {b}:9 (the SML_file commands that load this "
            "file) instead.")

    @pytest.mark.asyncio
    async def test_ml_with_unique_loader_redirects(
        self, temp_theory_file, mock_lsp_client,
    ):
        ml = str(Path(temp_theory_file).with_suffix(".ML"))
        Path(temp_theory_file).write_text(
            'theory Test\nimports Main\nbegin\nML_file "Test.ML"\nend\n')
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=True,
        )
        mock_lsp_client.loaders = [
            {"file": temp_theory_file, "line": 4, "command": "ML_file",
             "state": "unevaluated"},
        ]
        # the caller's line and after_text are dropped (approved)
        result = await evaluate_to(mock_lsp_client, ml, 42, after_text="ignored")
        assert result.target_file == temp_theory_file
        assert result.destination_line == 4
        assert result.message.split("\n")[0] == (
            f"Redirected: {ml} is a .ML file loaded by the ML_file command at "
            f"{temp_theory_file}:4; evaluated through that command instead.")
        assert temp_theory_file in mock_lsp_client.open_documents
        assert ml not in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_query_guard_refuses_without_redirect(
        self, temp_theory_file, mock_lsp_client,
    ):
        from isabelle_mcp.evaluation import check_evaluation_guard
        ml = str(Path(temp_theory_file).with_suffix(".ML"))
        mock_lsp_client.loaders = [
            {"file": temp_theory_file, "line": 3, "command": "ML_file",
             "state": "unevaluated"},
        ]
        with pytest.raises(IsabelleToolError) as exc:
            await check_evaluation_guard(mock_lsp_client, ml, MCPLine(1))
        assert str(exc.value) == (
            f"{ml} is a .ML file, which cannot be an evaluation target. "
            f"Evaluate to {temp_theory_file}:3 (the ML_file command that loads "
            "this file) instead.")


class TestGuardDispatch:
    """check_evaluation_guard after ruling 22: queries never evaluate. The
    dispatch, in priority order — served; the pure wait on the run that will
    reach the line; the reopen of a theory the prover holds; the refusal
    naming another file's evaluation; the not-evaluated sentence."""

    def _not_evaluated(self, file_path: str, line: int) -> str:
        return ev.NOT_EVALUATED_MESSAGE.format(file=file_path, line=line)

    @pytest.mark.asyncio
    async def test_a_never_evaluated_open_file_gets_the_sentence(
        self, mock_lsp_client, temp_theory_file,
    ):
        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        with pytest.raises(IsabelleToolError) as exc:
            await ev.check_evaluation_guard(mock_lsp_client, temp_theory_file, MCPLine(5))
        assert str(exc.value) == self._not_evaluated(temp_theory_file, 5)
        assert evaluation_state.active is False

    @pytest.mark.asyncio
    async def test_a_position_beyond_the_frontier_gets_the_sentence(
        self, mock_lsp_client, temp_theory_file,
    ):
        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            unprocessed=[(6, 0, 9, 0)],
        )
        assert await ev.check_evaluation_guard(
            mock_lsp_client, temp_theory_file, MCPLine(5)) is None
        with pytest.raises(IsabelleToolError) as exc:
            await ev.check_evaluation_guard(mock_lsp_client, temp_theory_file, MCPLine(8))
        assert str(exc.value) == self._not_evaluated(temp_theory_file, 8)

    @pytest.mark.asyncio
    async def test_a_theory_the_prover_does_not_hold_gets_the_sentence(
        self, mock_lsp_client, temp_theory_file,
    ):
        assert temp_theory_file not in mock_lsp_client.open_documents
        with pytest.raises(IsabelleToolError) as exc:
            await ev.check_evaluation_guard(mock_lsp_client, temp_theory_file, MCPLine(5))
        assert str(exc.value) == self._not_evaluated(temp_theory_file, 5)
        assert temp_theory_file not in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_a_swept_theory_the_prover_holds_is_reopened_and_served(
        self, mock_lsp_client, temp_theory_file,
    ):
        # Mutation control: drop the reopen step and this reds — the sentence
        # comes back instead of the answer.
        mock_lsp_client.entry_theories = [
            ev._parse_theory_status({"node_name": temp_theory_file, "ok": True}),
        ]
        opened = []
        real_open = mock_lsp_client.open_document

        async def recording_open(path, **kw):
            opened.append((path, kw.get("evaluation_target", False)))
            await real_open(path, **kw)

        mock_lsp_client.open_document = recording_open
        assert await ev.check_evaluation_guard(
            mock_lsp_client, temp_theory_file, MCPLine(5)) is None
        assert opened == [(temp_theory_file, True)]
        assert mock_lsp_client.open_documents[temp_theory_file].is_evaluation_target
        assert evaluation_state.active is False

    @pytest.mark.asyncio
    async def test_a_swept_theory_is_reopened_even_while_another_file_is_busy(
        self, mock_lsp_client, temp_theory_file,
    ):
        # The reopen comes before the busy test: a reopen executes nothing and
        # has no quarrel with the running evaluation.
        mock_lsp_client.entry_theories = [
            ev._parse_theory_status({"node_name": temp_theory_file, "ok": True}),
        ]
        evaluation_state.start("/tmp/Other.thy", MCPLine(100))
        assert await ev.check_evaluation_guard(
            mock_lsp_client, temp_theory_file, MCPLine(5)) is None
        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_a_reopened_theory_still_unevaluated_at_the_line_gets_the_sentence(
        self, mock_lsp_client, temp_theory_file,
    ):
        # Reopened because the prover holds it, yet the position was never
        # processed (an in-model node nothing evaluated): the honest error.
        mock_lsp_client.entry_theories = [
            ev._parse_theory_status({"node_name": temp_theory_file, "unprocessed": 10}),
        ]
        real_open = mock_lsp_client.open_document

        async def open_unevaluated(path, **kw):
            await real_open(path, **kw)
            mock_lsp_client._processing_trackers[path] = MockProcessingTracker(
                all_processed=False,
            )

        mock_lsp_client.open_document = open_unevaluated
        with pytest.raises(IsabelleToolError) as exc:
            await ev.check_evaluation_guard(mock_lsp_client, temp_theory_file, MCPLine(5))
        assert str(exc.value) == self._not_evaluated(temp_theory_file, 5)
        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_the_busy_refusal_still_names_the_other_evaluation(
        self, mock_lsp_client, temp_theory_file,
    ):
        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        evaluation_state.start("/tmp/Other.thy", MCPLine(100))
        with pytest.raises(IsabelleToolError) as exc:
            await ev.check_evaluation_guard(mock_lsp_client, temp_theory_file, MCPLine(5))
        assert str(exc.value) == ev.NOT_EVALUATED_REFUSAL.format(
            file=temp_theory_file, line=5, target="/tmp/Other.thy", target_line=100)

    @pytest.mark.asyncio
    async def test_the_pure_wait_rides_the_run_and_serves_when_the_line_is_reached(
        self, mock_lsp_client, temp_theory_file,
    ):
        await mock_lsp_client.open_document(temp_theory_file)
        tracker = MockProcessingTracker(unprocessed=[(2, 0, 9, 0)])
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        run = evaluation_state.start(temp_theory_file, MCPLine(9))
        run.riders += 1                                     # the evaluate_to still polling
        carets = []
        mock_lsp_client.set_caret = (
            lambda *a, **k: carets.append(a) or asyncio.sleep(0))

        async def frontier_advances():
            # Runs at the wait's first yield: the query rides by then.
            assert run.riders == 2
            tracker.unprocessed.clear()

        advance = asyncio.create_task(frontier_advances())
        assert await ev.check_evaluation_guard(
            mock_lsp_client, temp_theory_file, MCPLine(5)) is None
        await advance
        assert run.riders == 1                              # and steps off
        assert evaluation_state.active and evaluation_state.destination_line == 9
        assert carets == []

    @pytest.mark.asyncio
    async def test_the_pure_wait_times_out_into_a_progress_report(
        self, mock_lsp_client, temp_theory_file,
    ):
        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            unprocessed=[(2, 0, 9, 0)],
        )
        run = evaluation_state.start(temp_theory_file, MCPLine(9))
        run.riders += 1
        view = await ev.check_evaluation_guard(mock_lsp_client, temp_theory_file, MCPLine(5))
        assert isinstance(view, EvaluationView)
        assert view.status == "in_progress"
        assert view.message == f"Evaluating towards {temp_theory_file}:9."
        assert _file(view, temp_theory_file).pending == [(3, 9)]
        assert run.riders == 1 and evaluation_state.active
        assert evaluation_state.destination_line == 9

    @pytest.mark.asyncio
    async def test_a_run_cancelled_under_the_wait_leaves_the_sentence(
        self, mock_lsp_client, temp_theory_file,
    ):
        await mock_lsp_client.open_document(temp_theory_file)
        tracker = MockProcessingTracker(unprocessed=[(2, 0, 9, 0)])
        mock_lsp_client._processing_trackers[temp_theory_file] = tracker
        run = evaluation_state.start(temp_theory_file, MCPLine(9))
        run.riders += 1

        async def cancel_under_the_wait():
            assert run.riders == 2                          # at the wait's first yield
            evaluation_state.cancel()

        cancel = asyncio.create_task(cancel_under_the_wait())
        with pytest.raises(IsabelleToolError) as exc:
            await ev.check_evaluation_guard(mock_lsp_client, temp_theory_file, MCPLine(5))
        await cancel
        assert str(exc.value) == self._not_evaluated(temp_theory_file, 5)
        assert run.riders == 1

    @pytest.mark.asyncio
    async def test_no_tool_path_but_evaluate_to_writes_the_run_or_the_caret(
        self, mock_lsp_client, temp_theory_file, temp_theory_with_errors, monkeypatch,
    ):
        """The structural invariant: with every run-writing entry point and
        the caret send booby-trapped, the guard walks all five branches."""
        def trap(*a, **k):
            raise AssertionError("a query wrote the evaluation state")

        async def async_trap(*a, **k):
            trap()

        monkeypatch.setattr(ev.EvaluationState, "join_or_start", trap)
        monkeypatch.setattr(ev.EvaluationState, "start", trap)
        monkeypatch.setattr(ev.EvaluationState, "advance", trap)
        monkeypatch.setattr(mock_lsp_client, "set_caret", async_trap)
        guard = ev.check_evaluation_guard

        # 1. served
        await mock_lsp_client.open_document(temp_theory_file)
        assert await guard(mock_lsp_client, temp_theory_file, MCPLine(5)) is None
        # 5. never evaluated
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        with pytest.raises(IsabelleToolError, match="then ask again"):
            await guard(mock_lsp_client, temp_theory_file, MCPLine(5))
        # 3. reopened (the prover holds it)
        await mock_lsp_client.close_document(temp_theory_file)
        mock_lsp_client.entry_theories = [
            ev._parse_theory_status({"node_name": temp_theory_file, "ok": True}),
        ]
        assert await guard(mock_lsp_client, temp_theory_file, MCPLine(5)) is None
        # 2. the pure wait, run active on this file (start() is trapped, so
        #    the run is minted through the dataclass directly)
        evaluation_state.active = True
        evaluation_state.file_path = temp_theory_file
        evaluation_state.destination_line = MCPLine(9)
        evaluation_state.current = ev.Evaluation(riders=1)
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            unprocessed=[(2, 0, 20, 0)],
        )
        assert isinstance(
            await guard(mock_lsp_client, temp_theory_file, MCPLine(5)), EvaluationView)
        with pytest.raises(IsabelleToolError, match="then ask again"):   # past the target
            await guard(mock_lsp_client, temp_theory_file, MCPLine(11))
        assert evaluation_state.destination_line == 9
        # 4. another file is busy
        with pytest.raises(IsabelleToolError, match="cannot be while an evaluation"):
            await guard(mock_lsp_client, temp_theory_with_errors, MCPLine(3))


class TestSorryIsContent:
    def test_a_sorry_alone_proves_the_decoration_is_fresh(self, mock_lsp_client):
        """deco_has_content names sorry in its own right: with theory_status
        lagging (it still says a command failed) and the decoration showing
        only the sorry, the decoration is trusted — not the stale count."""
        from isabelle_mcp.evaluation import _build_file_snapshot
        from isabelle_mcp.models import TheoryStatus
        path = "/tmp/Fresh.thy"
        mock_lsp_client.open_documents[path] = DocumentState(
            file_path=path, uri=f"file://{path}", version=1,
            content="theory Fresh\nimports Main\nbegin\nlemma x: False sorry\nend\n",
        )
        mock_lsp_client._processing_trackers[path] = MockProcessingTracker(
            sorry=[(3, 15, 3, 20)],
        )
        stale = TheoryStatus(
            node_name=path, theory_name="Fresh", external=False, imports=[],
            ok=False, total=2, unprocessed=0, running=0, failed=1, finished=1,
            consolidated=True,
        )
        fs = _build_file_snapshot(mock_lsp_client, path, {path: stale})
        assert fs.lined and fs.state == "clean" and fs.error_count == 0
        assert fs.sorry == [(4, 4)]
