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
from isabelle_mcp.evaluation import _complete_message
from isabelle_mcp.models import EvaluationView, FileSnapshot, RunningCommand
from isabelle_mcp.processing import ProcessingTracker, parse_decoration_ranges
from isabelle_mcp.utils import IsabelleToolError, MCPLine


class MockProcessingTracker:
    """Configurable tracker stub exposing the decoration getters the snapshot reads."""

    def __init__(self, *, all_processed=True, bad=None, overview_error=None,
                 overview_warning=None, running=None, unprocessed=None):
        self._all_processed = all_processed
        self._bad = bad or []
        self._oerr = overview_error or []
        self._owarn = overview_warning or []
        self._running = running or []
        self._unproc = unprocessed or []

    def range_processed(self, start_line, end_line):
        return self._all_processed

    def line_reached(self, line):
        return self._all_processed

    def line_running(self, line):
        return False

    @property
    def all_processed(self):
        return self._all_processed

    def note_doc_update_sent(self):
        pass

    def get_running_ranges(self):
        return list(self._running)

    def get_running_ranges_with_onset(self):
        return []

    def get_unprocessed_ranges(self):
        return list(self._unproc)

    def get_bad_ranges(self):
        return list(self._bad)

    def get_overview_error_ranges(self):
        return list(self._oerr)

    def get_overview_warning_ranges(self):
        return list(self._owarn)

    async def wait_until_processed_bounded(
        self, start_line, end_line, timeout=5.0, health_check=None, check_interval=5.0,
    ):
        return self._all_processed


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
        assert "evaluation_status" in result.message

    @pytest.mark.asyncio
    async def test_while_active_fails(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert evaluation_state.active

        with pytest.raises(IsabelleToolError, match="already in progress"):
            await evaluate_to(mock_lsp_client, temp_theory_file, 10)

    @pytest.mark.asyncio
    async def test_reports_error_lines_from_decoration(self, temp_theory_file, mock_lsp_client):
        # text_overview_error + background_bad on the same 0-indexed line 4 → 1-indexed 5.
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=True,
            overview_error=[(4, 0, 4, 10)],
            bad=[(4, 0, 4, 10)],
        )
        result = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        fs = _file(result, temp_theory_file)
        assert fs is not None and fs.lined
        # union deduped by line → one error span, not two.
        assert fs.errors == [(5, 5)]
        assert fs.warnings == []

    @pytest.mark.asyncio
    async def test_negative_line(self, temp_theory_file, mock_lsp_client):
        result = await evaluate_to(mock_lsp_client, temp_theory_file, -1)
        assert result.status == "complete"
        assert result.destination_line is not None
        assert result.destination_line >= 11

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

        interrupted: list[str] = []
        monkeypatch.setattr(
            mock_lsp_client, "get_all_running_commands",
            lambda: [_fork(temp_theory_file)],
        )

        async def _spy(fp):
            interrupted.append(fp)

        monkeypatch.setattr(mock_lsp_client, "force_interrupt", _spy)

        result = await cancel_evaluation(mock_lsp_client)
        assert result.status == "cancelled"
        assert interrupted == [temp_theory_file]    # the fork was actually interrupted

    @pytest.mark.asyncio
    async def test_idle_reports_no_evaluation(self, mock_lsp_client):
        # No active eval and no running fork → genuinely idle, both stay quiet.
        assert (await evaluation_status(mock_lsp_client)).status == "no_evaluation"
        assert (await cancel_evaluation(mock_lsp_client)).status == "no_evaluation"


class TestSnapshotCategorization:
    """_build_file_snapshot: decoration union + theory_status fallback."""

    def _ts(self, node, **kw):
        from isabelle_mcp.models import TheoryStatus
        base = dict(
            node_name=node, theory_name="T", external=False, imports=[], ok=True,
            total=10, unprocessed=0, running=0, warned=0, failed=0, finished=10,
            consolidated=True,
        )
        base.update(kw)
        return TheoryStatus(**base)

    def test_decoration_union_sorry_and_error_all_errors(self, mock_lsp_client):
        from isabelle_mcp.evaluation import _build_file_snapshot
        path = "/tmp/T.thy"
        # bad has a failed proof (line 5, also in overview_error) AND a sorry (line 7,
        # not in overview_error). Both land in `errors`; no sorry column.
        mock_lsp_client._processing_trackers[path] = MockProcessingTracker(
            overview_error=[(4, 0, 4, 5)],
            bad=[(4, 0, 4, 5), (6, 0, 6, 5)],
            overview_warning=[(8, 0, 8, 5)],
        )
        fs = _build_file_snapshot(mock_lsp_client, path, {path: self._ts(path)})
        assert fs.lined
        assert fs.errors == [(5, 5), (7, 7)]
        assert fs.warnings == [(9, 9)]
        assert fs.running == []

    def test_fallback_counts_when_no_tracker(self, mock_lsp_client):
        from isabelle_mcp.evaluation import _build_file_snapshot
        path = "/tmp/Dep.thy"
        ts = self._ts(path, ok=False, failed=2, warned=1)
        fs = _build_file_snapshot(mock_lsp_client, path, {path: ts})
        assert not fs.lined
        assert fs.state == "problems"
        assert fs.error_count == 2 and fs.warning_count == 1

    def test_fallback_in_progress_not_clean(self, mock_lsp_client):
        from isabelle_mcp.evaluation import _build_file_snapshot
        path = "/tmp/Dep.thy"
        ts = self._ts(path, consolidated=False, unprocessed=3)
        fs = _build_file_snapshot(mock_lsp_client, path, {path: ts})
        assert not fs.lined
        assert fs.state == "in_progress"


class TestCompleteMessage:
    def _run(self, line, text="apply simp"):
        return RunningCommand(
            file_path="/proj/Foo.thy", start_line=line, end_line=line,
            text=text, elapsed_seconds=42.0,
        )

    def _fs(self, error_count=0):
        return FileSnapshot(
            "/proj/Foo.thy", lined=True,
            state="problems" if error_count else "clean",
            error_count=error_count,
        )

    def test_clean_complete(self):
        assert _complete_message(11, [], [self._fs()]) == (
            "Evaluation complete, arrived at line 11."
        )

    def test_running_only(self):
        msg = _complete_message(11, [self._run(11)], [self._fs()])
        assert msg == (
            "Evaluation arrived at line 11 with 1 statement(s) still running "
            "and 0 statement(s) failed."
        )

    def test_failed_only(self):
        msg = _complete_message(11, [], [self._fs(error_count=2)])
        assert msg == (
            "Evaluation arrived at line 11 with 0 statement(s) still running "
            "and 2 statement(s) failed."
        )

    def test_running_and_failed_sum_across_files(self):
        files = [self._fs(error_count=1), self._fs(error_count=2)]
        msg = _complete_message(11, [self._run(11), self._run(11)], files)
        assert "2 statement(s) still running and 3 statement(s) failed" in msg


class TestRendering:
    def test_render_absolute_and_relative(self):
        view = EvaluationView(
            status="complete", destination_line=7,
            message="Evaluation complete, arrived at line 7.",
            files=[
                FileSnapshot("/proj/Foo.thy", lined=True, state="problems",
                             errors=[(5, 5), (9, 11)], warnings=[(6, 6)],
                             error_count=2, warning_count=1),
                FileSnapshot("/proj/Bar.thy", lined=True, state="clean"),
            ],
        )
        rel = format_evaluation_result(view, "/proj")
        assert "Foo.thy:" in rel
        assert "errors: 5, 9-11" in rel
        assert "Bar.thy: clean" in rel
        absolute = format_evaluation_result(view, None)
        assert "/proj/Foo.thy:" in absolute

    def test_render_no_evaluation(self):
        view = EvaluationView(status="no_evaluation", message="No evaluation in progress.")
        assert format_evaluation_result(view, None) == "No evaluation in progress."


class TestDecorationClear:
    """C2 regression: a fixed error/warning/sorry clears via an empty content push."""

    @pytest.mark.asyncio
    async def test_empty_push_clears_all_three_types(self):
        tr = ProcessingTracker()
        present = parse_decoration_ranges([
            {"type": "background_bad", "content": [{"range": [4, 0, 4, 5]}]},
            {"type": "text_overview_error", "content": [{"range": [4, 0, 4, 5]}]},
            {"type": "text_overview_warning", "content": [{"range": [6, 0, 6, 5]}]},
        ])
        await tr.update(present)
        assert tr.get_bad_ranges() and tr.get_overview_error_ranges() and tr.get_overview_warning_ranges()

        emptied = parse_decoration_ranges([
            {"type": "background_bad", "content": []},
            {"type": "text_overview_error", "content": []},
            {"type": "text_overview_warning", "content": []},
        ])
        await tr.update(emptied)
        assert tr.get_bad_ranges() == []
        assert tr.get_overview_error_ranges() == []
        assert tr.get_overview_warning_ranges() == []


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
