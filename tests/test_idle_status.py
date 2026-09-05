"""isabelle_evaluation_status when nothing is outstanding: the tool the agent
calls to ask for status must not hide the failures that remain once the run
has ended — in the files it evaluated and in their dependencies alike.

The idle answer walks the same data path as the busy one: one theory_status
pulled after the entry wait, every failed theory auto-opened, every file with
something wrong listed with line numbers, under a first line that says whether
any errors remain. The entry waits until every open document's picture is
fresh (a content send during the wait re-arms it). Warnings are never
reported. isabelle_cancel_evaluation's idle reply is untouched.
"""

import asyncio
import os
import time

import pytest

from isabelle_mcp.evaluation import (
    IDLE_CLEAN_SENTENCE,
    cancel_evaluation,
    evaluation_state,
    evaluation_status,
    format_evaluation_result,
)
from isabelle_mcp.processing import ProcessingTracker, parse_decoration_ranges
from isabelle_mcp.server import isabelle_evaluation_status, mcp
from isabelle_mcp.utils import MCPLine
from tests.conftest import MockProcessingTracker, full_decoration_entries
from tests.test_server import _patch_ensure

OLDER = -19
NEWER = -40


async def _open_with(client, path: str, **tracker_kw) -> None:
    """Open *path* on the mock client with a tracker showing the given ranges
    (LSP 0-indexed ``(line, char, line, char)``)."""
    await client.open_document(path)
    client._processing_trackers[path] = MockProcessingTracker(**tracker_kw)


async def _open_with_real(client, path: str, document_version: int, **ranges) -> ProcessingTracker:
    """Open *path* with a REAL tracker holding a full picture stamped
    *document_version* (``overview_error=[...]`` and kin as in _open_with)."""
    await client.open_document(path)
    entries = full_decoration_entries(
        text_overview_error=ranges.get("overview_error", []),
        background_bad=ranges.get("bad", []),
        background_running1=ranges.get("running", []),
    )
    tracker = ProcessingTracker(client.freshness)
    await tracker.update(parse_decoration_ranges(entries), document_version)
    client._processing_trackers[path] = tracker
    return tracker


def _second_file(tmp_path, name: str) -> str:
    path = tmp_path / name
    path.write_text(
        f"theory {path.stem}\nimports Main\nbegin\n\nlemma x: True by simp\n\nend\n"
    )
    return str(path)


def _theory_row(path: str, **kw) -> dict:
    """One raw theory_status row, settled unless *kw* says otherwise."""
    row = {
        "node_name": path, "theory_name": os.path.basename(path)[:-4],
        "external": False, "imports": [], "ok": True, "total": 10,
        "unprocessed": 0, "running": 0, "warned": 0, "failed": 0,
        "finished": 10, "canceled": False, "consolidated": True, "percentage": 100,
    }
    row.update(kw)
    return row


class TestIdleReport:
    @pytest.mark.asyncio
    async def test_nothing_open_is_clean(self, mock_lsp_client):
        view = await evaluation_status(mock_lsp_client)
        assert view.status == "no_evaluation"
        assert view.message == IDLE_CLEAN_SENTENCE
        assert view.files == []
        assert format_evaluation_result(view, None) == IDLE_CLEAN_SENTENCE

    @pytest.mark.asyncio
    async def test_failures_are_listed_with_line_numbers(
        self, temp_theory_file, mock_lsp_client,
    ):
        await _open_with(
            mock_lsp_client, temp_theory_file,
            overview_error=[(2, 0, 2, 5), (7, 0, 7, 3)], bad=[(2, 0, 2, 5), (7, 0, 7, 3)],
        )
        view = await evaluation_status(mock_lsp_client)
        assert view.status == "no_evaluation"
        assert view.message == (
            "No evaluation in progress. Nothing is running, but 2 failed commands remain."
        )
        (fs,) = view.files
        assert fs.file_path == temp_theory_file and fs.lined
        assert fs.errors == [(3, 3), (8, 8)]
        text = format_evaluation_result(view, None)
        assert text.startswith(view.message)
        assert f"{temp_theory_file}:\n  errors: lines 3, 8" in text

    @pytest.mark.asyncio
    async def test_one_failure_is_singular(self, temp_theory_file, mock_lsp_client):
        await _open_with(
            mock_lsp_client, temp_theory_file,
            overview_error=[(2, 0, 2, 5)], bad=[(2, 0, 2, 5)],
        )
        view = await evaluation_status(mock_lsp_client)
        assert view.message == (
            "No evaluation in progress. Nothing is running, but 1 failed command remains."
        )

    @pytest.mark.asyncio
    async def test_a_line_in_both_error_channels_counts_once(
        self, temp_theory_file, mock_lsp_client,
    ):
        # The same failed command shows up as a bad range and an overview error;
        # the count is per line, as the file sections count.
        await _open_with(
            mock_lsp_client, temp_theory_file,
            overview_error=[(2, 0, 2, 5)], bad=[(2, 2, 2, 4)],
        )
        view = await evaluation_status(mock_lsp_client)
        assert "but 1 failed command remains." in view.message
        assert view.files[0].errors == [(3, 3)]

    @pytest.mark.asyncio
    async def test_a_file_with_only_a_sorry_gets_a_sorry_row_under_the_clean_line(
        self, temp_theory_file, mock_lsp_client,
    ):
        # A sorry is not an error: the first line still says no errors remain,
        # the file snapshot lists the sorry, and nothing is counted or chased.
        await _open_with(
            mock_lsp_client, temp_theory_file,
            bad=[(4, 0, 4, 5)], sorry=[(4, 0, 4, 5)],
        )
        view = await evaluation_status(mock_lsp_client)
        assert view.message == IDLE_CLEAN_SENTENCE
        (fs,) = view.files
        assert fs.state == "clean" and fs.errors == [] and fs.sorry == [(5, 5)]
        assert format_evaluation_result(view, None, call_to_action=False) == (
            IDLE_CLEAN_SENTENCE + f"\n\n{temp_theory_file}:\n  sorry: line 5"
        )

    @pytest.mark.asyncio
    async def test_a_file_with_only_warnings_is_not_listed(
        self, temp_theory_file, mock_lsp_client,
    ):
        # Warnings are ignored throughout: a file whose only mark is a warning
        # is clean for every purpose, and no row ever says "warnings:".
        await _open_with(
            mock_lsp_client, temp_theory_file, overview_warning=[(4, 0, 4, 3)],
        )
        view = await evaluation_status(mock_lsp_client)
        assert view.message == IDLE_CLEAN_SENTENCE
        assert view.files == []
        assert "warning" not in format_evaluation_result(view, None)

    @pytest.mark.asyncio
    async def test_a_clean_open_file_is_not_listed(
        self, temp_theory_file, mock_lsp_client, tmp_path,
    ):
        clean = _second_file(tmp_path, "Clean.thy")
        await _open_with(mock_lsp_client, clean)
        await _open_with(
            mock_lsp_client, temp_theory_file,
            overview_error=[(2, 0, 2, 5)], bad=[(2, 0, 2, 5)],
        )
        view = await evaluation_status(mock_lsp_client)
        assert [fs.file_path for fs in view.files] == [temp_theory_file]

    @pytest.mark.asyncio
    async def test_the_first_line_counts_every_listed_file(
        self, temp_theory_file, mock_lsp_client, tmp_path,
    ):
        other = _second_file(tmp_path, "Other.thy")
        await _open_with(
            mock_lsp_client, other, overview_error=[(4, 0, 4, 5)], bad=[(4, 0, 4, 5)],
        )
        await _open_with(
            mock_lsp_client, temp_theory_file,
            overview_error=[(2, 0, 2, 5), (7, 0, 7, 3)], bad=[(2, 0, 2, 5), (7, 0, 7, 3)],
        )
        view = await evaluation_status(mock_lsp_client)
        assert "but 3 failed commands remain." in view.message
        assert sum(fs.error_count for fs in view.files) == 3

    @pytest.mark.asyncio
    async def test_a_failed_dependency_is_auto_opened_and_reported(
        self, temp_theory_file, mock_lsp_client, tmp_path,
    ):
        # The idle report walks the busy report's data path: theory_status is
        # pulled once, every failed theory is opened (whether or not any run
        # ever touched it), and its failure is listed — by count until its
        # decoration arrives. A failed import must never fall out of sight
        # just because no run is outstanding.
        dep = _second_file(tmp_path, "Dep.thy")
        calls = []
        real_status = mock_lsp_client.request_theory_status

        async def status():
            calls.append(True)
            return await real_status()

        mock_lsp_client.request_theory_status = status
        mock_lsp_client.theory_rows = [_theory_row(temp_theory_file),
                                       _theory_row(dep, ok=False, failed=1, finished=9)]
        # The auto-opened file has no picture at least as new as the record:
        # the mock plants a tracker on open, so give the record a NEWER stamp
        # than the planted picture's (0) — counts until its decoration arrives.
        mock_lsp_client.theory_status_stamp = NEWER
        await _open_with(mock_lsp_client, temp_theory_file, document_version=NEWER)
        view = await evaluation_status(mock_lsp_client)
        assert calls == [True]
        assert dep in mock_lsp_client.open_documents
        assert not mock_lsp_client.open_documents[dep].is_evaluation_target
        assert view.message == (
            "No evaluation in progress. Nothing is running, but 1 failed command remains."
        )
        (fs,) = view.files
        assert fs.file_path == dep and not fs.lined and fs.error_count == 1
        assert f"{dep}: 1 error (no line info)" in format_evaluation_result(view, None)
        assert not evaluation_state.active

    @pytest.mark.asyncio
    async def test_a_never_evaluated_open_file_still_leaves_the_session_idle(
        self, temp_theory_file, mock_lsp_client,
    ):
        # An open file nothing ever evaluated has unprocessed commands forever.
        # That must not put the session on the busy path (only running work
        # does), and it is not an imported theory, so the summary line does
        # not count it either: the report is the clean sentence alone.
        mock_lsp_client.theory_rows = [
            _theory_row(temp_theory_file, unprocessed=10, finished=0,
                        consolidated=False, percentage=0)]
        await _open_with(mock_lsp_client, temp_theory_file, all_processed=False)
        view = await evaluation_status(mock_lsp_client)
        assert view.status == "no_evaluation"
        assert view.message == IDLE_CLEAN_SENTENCE
        assert view.files == []
        assert view.unprocessed_theories == 0
        assert format_evaluation_result(view, None) == IDLE_CLEAN_SENTENCE

    @pytest.mark.asyncio
    async def test_a_loading_import_of_an_open_file_is_counted_when_idle(
        self, temp_theory_file, mock_lsp_client,
    ):
        # The one kind of member the summary line admits: a theory in the
        # import closure of an open document that still has unprocessed
        # commands. Nothing is running yet (the load has not started), so the
        # session is idle, and the count says what is still to come.
        mock_lsp_client.theory_rows = [
            _theory_row(temp_theory_file, imports=[{"theory_name": "Dep_loading"}]),
            _theory_row("/tmp/Dep_loading.thy", external=True,
                        unprocessed=10, finished=0, consolidated=False)]
        await _open_with(mock_lsp_client, temp_theory_file, all_processed=True)
        view = await evaluation_status(mock_lsp_client)
        assert view.status == "no_evaluation"
        assert view.files == []
        assert view.unprocessed_theories == 1
        assert format_evaluation_result(view, None) == (
            IDLE_CLEAN_SENTENCE + "\n\n1 imported theory is not yet processed."
        )

    @pytest.mark.asyncio
    async def test_a_dependency_running_in_a_closed_file_is_busy(
        self, temp_theory_file, mock_lsp_client,
    ):
        # Nothing is open with a running range, yet theory_status says an
        # import is still running: the answer is busy, and the count comes
        # from theory_status — never "0 commands are still running". No run
        # is outstanding, so the last run's target (temp_theory_file, half
        # evaluated) neither seeds a file snapshot nor clips a pending row —
        # the picture is session-level, exactly as when idle — and its own
        # unprocessed rest is not an imported theory, so no summary line.
        evaluation_state.start(temp_theory_file, MCPLine(5))
        evaluation_state.complete()
        mock_lsp_client.theory_rows = [
            _theory_row(temp_theory_file, unprocessed=4, finished=6, consolidated=False),
            _theory_row("/tmp/Dep_running.thy", running=2, unprocessed=3,
                        finished=5, consolidated=False)]
        await _open_with(mock_lsp_client, temp_theory_file, unprocessed=[(6, 0, 9, 0)])
        view = await evaluation_status(mock_lsp_client)
        assert view.status == "in_progress"
        assert view.message == "2 commands are still running."
        assert view.target_file is None and view.destination_line is None
        assert [fs.file_path for fs in view.files] == ["/tmp/Dep_running.thy"]
        assert format_evaluation_result(view, None, call_to_action=False) == (
            "2 commands are still running.\n\n"
            "/tmp/Dep_running.thy: in progress (2 running so far)"
        )

    @pytest.mark.asyncio
    async def test_a_settled_old_target_is_not_listed_as_clean(
        self, temp_theory_file, mock_lsp_client,
    ):
        # The last run's target, now settled: with no run outstanding it must
        # not be seeded into the report — a lone "Test.thy: clean" block is
        # not approved output.
        evaluation_state.start(temp_theory_file, MCPLine(5))
        evaluation_state.complete()
        mock_lsp_client.theory_rows = [
            _theory_row(temp_theory_file),
            _theory_row("/tmp/Dep_running.thy", running=1, unprocessed=0,
                        finished=9, consolidated=False)]
        await _open_with(mock_lsp_client, temp_theory_file)
        view = await evaluation_status(mock_lsp_client)
        assert [fs.file_path for fs in view.files] == ["/tmp/Dep_running.thy"]
        assert "clean" not in format_evaluation_result(view, None, call_to_action=False)

    @pytest.mark.asyncio
    async def test_the_report_and_the_first_line_count_the_same_failures(
        self, temp_theory_file, mock_lsp_client, tmp_path,
    ):
        # A failed dependency whose decoration has not arrived: the first line
        # and the file snapshot below it are computed from one source, so the
        # count above can never disagree with the rows below.
        from isabelle_mcp.evaluation import _failed_count
        from tests.conftest import theory_status_record
        dep = _second_file(tmp_path, "Dep_count.thy")
        rows = [_theory_row(temp_theory_file),
                _theory_row(dep, ok=False, failed=1, finished=9)]
        mock_lsp_client.theory_rows = rows
        mock_lsp_client.theory_status_stamp = NEWER      # the auto-opened dep: counts
        await _open_with(mock_lsp_client, temp_theory_file, document_version=NEWER)
        view = await evaluation_status(mock_lsp_client)
        assert _failed_count(mock_lsp_client, theory_status_record(rows, NEWER)) == 1
        assert "1 failed command remains." in view.message
        assert sum(fs.error_count for fs in view.files) == 1


class TestIdleEntryWait:
    """The tool's entry waits until every open document's picture is fresh —
    stamped at least as new as the newest version, no unflushed content —
    instead of answering from a picture of an older document state."""

    @pytest.mark.asyncio
    async def test_reports_the_post_edit_picture(
        self, temp_theory_file, mock_lsp_client,
    ):
        # Before the edit, the picture (at OLDER) shows an error; the edit
        # fixed it, the client saw NEWER, and the server's push at NEWER
        # lands while the entry waits.
        tracker = await _open_with_real(
            mock_lsp_client, temp_theory_file, OLDER,
            overview_error=[(2, 0, 2, 5)], bad=[(2, 0, 2, 5)],
        )
        mock_lsp_client.freshness.advance(NEWER)

        async def fresh_decoration_arrives():
            await asyncio.sleep(0.02)
            await tracker.update(parse_decoration_ranges(full_decoration_entries()), NEWER)
            await mock_lsp_client.freshness.notify()

        push = asyncio.ensure_future(fresh_decoration_arrives())
        started = time.monotonic()
        view = await evaluation_status(mock_lsp_client)
        await push
        assert time.monotonic() - started >= 0.015
        assert view.message == IDLE_CLEAN_SENTENCE
        assert view.files == []

    @pytest.mark.asyncio
    async def test_work_that_starts_inside_the_wait_takes_the_busy_path(
        self, temp_theory_file, mock_lsp_client,
    ):
        # The edit made the prover re-run a command; the push that makes the
        # picture fresh shows it running, and that is the answer.
        tracker = await _open_with_real(mock_lsp_client, temp_theory_file, OLDER)
        mock_lsp_client.freshness.advance(NEWER)

        async def command_starts():
            await asyncio.sleep(0.02)
            await tracker.update(parse_decoration_ranges(full_decoration_entries(
                background_running1=[(7, 0, 8, 5)])), NEWER)
            await mock_lsp_client.freshness.notify()

        push = asyncio.ensure_future(command_starts())
        view = await evaluation_status(mock_lsp_client)
        await push
        assert view.status == "in_progress"
        assert view.message == "1 command is still running."

    @pytest.mark.asyncio
    async def test_a_content_send_during_the_wait_re_arms_it(
        self, temp_theory_file, mock_lsp_client,
    ):
        # A didChange landing while the entry waits (the watcher's sink) is
        # unflushed content: the wait re-arms its flush and answers from the
        # picture after the LAST edit.
        tracker = await _open_with_real(
            mock_lsp_client, temp_theory_file, OLDER,
            overview_error=[(2, 0, 2, 5)], bad=[(2, 0, 2, 5)],
        )
        mock_lsp_client.freshness.advance(NEWER)

        async def second_edit_then_fix():
            await asyncio.sleep(0.02)
            mock_lsp_client.freshness.content_sends += 1      # the second edit
            await mock_lsp_client.freshness.notify()
            await asyncio.sleep(0.02)
            await tracker.update(parse_decoration_ranges(full_decoration_entries()), NEWER)
            await mock_lsp_client.freshness.notify()

        push = asyncio.ensure_future(second_edit_then_fix())
        view = await evaluation_status(mock_lsp_client)
        await push
        assert view.message == IDLE_CLEAN_SENTENCE
        assert mock_lsp_client.flush_calls == [False, False]   # re-armed once
        assert not mock_lsp_client.freshness.unflushed_content

    @pytest.mark.asyncio
    async def test_with_a_fresh_picture_it_answers_at_once(
        self, temp_theory_file, mock_lsp_client,
    ):
        await _open_with(
            mock_lsp_client, temp_theory_file,
            overview_error=[(2, 0, 2, 5)], bad=[(2, 0, 2, 5)],
        )
        started = time.monotonic()
        view = await evaluation_status(mock_lsp_client)
        assert time.monotonic() - started < 0.04
        assert "but 1 failed command remains." in view.message
        assert mock_lsp_client.flush_calls == [False]         # the entry wait's flush


class TestIdleReport2:
    @pytest.mark.asyncio
    async def test_a_running_decoration_alone_takes_the_busy_path(
        self, temp_theory_file, mock_lsp_client,
    ):
        # The running-command list is derived from the very tracker ranges the
        # sections render, so a running decoration fails _no_pending_work:
        # "Nothing is running" can never sit above a "running:" row.
        await _open_with(mock_lsp_client, temp_theory_file, running=[(4, 0, 4, 5)])
        view = await evaluation_status(mock_lsp_client)
        assert view.status == "in_progress"
        assert view.message == "1 command is still running."

    @pytest.mark.asyncio
    async def test_a_session_that_never_evaluated_lists_no_file_for_its_absent_target(
        self, temp_theory_file, mock_lsp_client, monkeypatch,
    ):
        # A save made the prover re-check a command, so the busy path is taken
        # with no run on the books. ``evaluation_state.file_path`` is still the
        # never-evaluated "" -- only start() ever writes it -- and there is no
        # target to report. Setting it explicitly is the precondition, not a
        # double: cancel() (which the autouse reset calls) leaves file_path
        # alone, so an earlier test's target would otherwise stand in for it.
        monkeypatch.setattr(evaluation_state, "file_path", "")
        await _open_with(mock_lsp_client, temp_theory_file, running=[(4, 0, 4, 5)])
        view = await evaluation_status(mock_lsp_client)
        assert view.status == "in_progress"
        assert view.message == "1 command is still running."
        # The absent target must not be snapshotted: relativize("") renders the
        # project root, so a section would name a DIRECTORY as a file in progress.
        assert [fs.file_path for fs in view.files] == [temp_theory_file]
        text = format_evaluation_result(view, os.getcwd(), call_to_action=False)
        assert ".: in progress" not in text
        assert text == (
            "1 command is still running.\n\n"
            f"{temp_theory_file}:\n  running: line 5"
        )


class TestNeighboursUnchanged:
    @pytest.mark.asyncio
    async def test_cancel_when_idle_still_says_only_no_evaluation(
        self, temp_theory_file, mock_lsp_client,
    ):
        # Cancelling has nothing to report about old failures: that is the
        # status tool's job.
        await _open_with(
            mock_lsp_client, temp_theory_file,
            overview_error=[(2, 0, 2, 5)], bad=[(2, 0, 2, 5)],
        )
        view = await cancel_evaluation(mock_lsp_client)
        assert view.status == "no_evaluation"
        assert format_evaluation_result(view, None) == "No evaluation in progress."


class TestTool:
    @pytest.mark.asyncio
    async def test_text_lists_failures_and_never_points_at_itself(
        self, temp_theory_file, mock_lsp_client,
    ):
        await _open_with(
            mock_lsp_client, temp_theory_file,
            overview_error=[(2, 0, 2, 5), (7, 0, 7, 3)], bad=[(2, 0, 2, 5), (7, 0, 7, 3)],
        )
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_evaluation_status()
        text = result.content[0].text
        assert text.startswith(
            "No evaluation in progress. Nothing is running, but 2 failed commands remain."
        )
        assert "errors: lines 3, 8" in text
        assert "Call isabelle_evaluation_status" not in text

    @pytest.mark.asyncio
    async def test_description_is_the_one_approved_sentence(self):
        # D-C8: the tool answers when idle too, so "the progress of an ongoing
        # evaluation" would be too narrow.
        tool = await mcp.get_tool("isabelle_evaluation_status")
        assert tool.description == "Check the current evaluation state."
