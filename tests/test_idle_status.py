"""isabelle_evaluation_status when nothing is outstanding (fix plan section 9A,
item 17): the tool the agent calls to ask for status must not hide the failures
that remain once the run has ended.

The idle answer lists every open document that still shows errors or warnings,
with line numbers, under a first line that says whether any errors remain. It
is read from the decoration cache, so a recent edit makes the tool wait until
the edits have stopped (debounce; a further edit re-arms the window). The
footer's rule (silence about old failures once no
run is outstanding) and isabelle_cancel_evaluation's idle reply are untouched.
"""

import asyncio
import os
import time

import pytest

from isabelle_mcp import processing
from isabelle_mcp.evaluation import (
    IDLE_CLEAN_SENTENCE,
    cancel_evaluation,
    evaluation_state,
    evaluation_status,
    format_evaluation_result,
)
from isabelle_mcp.server import isabelle_evaluation_status, mcp
from tests.conftest import MockProcessingTracker
from tests.test_server import _patch_ensure


async def _open_with(client, path: str, **tracker_kw) -> None:
    """Open *path* on the mock client with a tracker showing the given ranges
    (LSP 0-indexed ``(line, char, line, char)``)."""
    await client.open_document(path)
    client._processing_trackers[path] = MockProcessingTracker(**tracker_kw)


def _second_file(tmp_path, name: str) -> str:
    path = tmp_path / name
    path.write_text(
        f"theory {path.stem}\nimports Main\nbegin\n\nlemma x: True by simp\n\nend\n"
    )
    return str(path)


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
            overview_error=[(2, 0, 2, 5)], bad=[(7, 0, 7, 3)],
        )
        view = await evaluation_status(mock_lsp_client)
        assert view.status == "no_evaluation"
        assert view.message == (
            "No evaluation in progress. Nothing is running, but 2 commands failed."
        )
        (fs,) = view.files
        assert fs.file_path == temp_theory_file and fs.lined
        assert fs.errors == [(3, 3), (8, 8)]
        text = format_evaluation_result(view, None)
        assert text.startswith(view.message)
        assert f"{temp_theory_file}:\n  errors: lines 3, 8" in text

    @pytest.mark.asyncio
    async def test_one_failure_is_singular(self, temp_theory_file, mock_lsp_client):
        await _open_with(mock_lsp_client, temp_theory_file, bad=[(2, 0, 2, 5)])
        view = await evaluation_status(mock_lsp_client)
        assert view.message == (
            "No evaluation in progress. Nothing is running, but 1 command failed."
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
        assert "but 1 command failed." in view.message
        assert view.files[0].errors == [(3, 3)]

    @pytest.mark.asyncio
    async def test_a_file_with_only_warnings_is_listed_under_the_clean_line(
        self, temp_theory_file, mock_lsp_client,
    ):
        # Warnings are reported as they are while a run is busy; the first line
        # still speaks only of errors, and the section's own label says the rest.
        await _open_with(
            mock_lsp_client, temp_theory_file, overview_warning=[(4, 0, 4, 3)],
        )
        view = await evaluation_status(mock_lsp_client)
        assert view.message == IDLE_CLEAN_SENTENCE
        (fs,) = view.files
        assert fs.warnings == [(5, 5)] and fs.errors == []
        assert "warnings: line 5" in format_evaluation_result(view, None)

    @pytest.mark.asyncio
    async def test_a_clean_open_file_is_not_listed(
        self, temp_theory_file, mock_lsp_client, tmp_path,
    ):
        clean = _second_file(tmp_path, "Clean.thy")
        await _open_with(mock_lsp_client, clean)
        await _open_with(mock_lsp_client, temp_theory_file, bad=[(2, 0, 2, 5)])
        view = await evaluation_status(mock_lsp_client)
        assert [fs.file_path for fs in view.files] == [temp_theory_file]

    @pytest.mark.asyncio
    async def test_the_first_line_counts_every_listed_file(
        self, temp_theory_file, mock_lsp_client, tmp_path,
    ):
        other = _second_file(tmp_path, "Other.thy")
        await _open_with(mock_lsp_client, other, bad=[(4, 0, 4, 5)])
        await _open_with(
            mock_lsp_client, temp_theory_file,
            overview_error=[(2, 0, 2, 5)], bad=[(7, 0, 7, 3)],
        )
        view = await evaluation_status(mock_lsp_client)
        assert "but 3 commands failed." in view.message
        assert sum(fs.error_count for fs in view.files) == 3

    @pytest.mark.asyncio
    async def test_no_theory_status_and_nothing_auto_opened(
        self, temp_theory_file, mock_lsp_client,
    ):
        # theory_status auto-opens every not-ok theory into auto_opened_files,
        # which only a run's end clears: with no run, that would leak. The idle
        # report must therefore be built from the decoration cache alone.
        calls = []
        original = mock_lsp_client.request_theory_status

        async def counting():
            calls.append(True)
            return await original()

        mock_lsp_client.request_theory_status = counting
        await _open_with(mock_lsp_client, temp_theory_file, bad=[(2, 0, 2, 5)])
        await evaluation_status(mock_lsp_client)
        assert calls == []
        assert evaluation_state.auto_opened_files == set()
        assert not evaluation_state.active


class TestIdleGraceWindow:
    """An edit within DECORATION_GRACE makes the decoration cache describe the
    pre-edit document. The idle branch waits the remaining window out and judges
    afresh, instead of answering from the stale cache or hiding the answer."""

    @pytest.fixture(autouse=True)
    def _short_grace(self, monkeypatch):
        monkeypatch.setattr(processing, "DECORATION_GRACE", 0.05)

    @pytest.mark.asyncio
    async def test_reports_the_post_edit_picture(
        self, temp_theory_file, mock_lsp_client,
    ):
        # Before the edit, the tracker shows an error; the edit fixed it, and the
        # server's fresh decoration lands inside the window.
        await _open_with(mock_lsp_client, temp_theory_file, bad=[(2, 0, 2, 5)])
        processing.note_edit_sent()

        async def fresh_decoration_arrives():
            await asyncio.sleep(0.01)
            mock_lsp_client._processing_trackers[temp_theory_file] = (
                MockProcessingTracker()
            )

        push = asyncio.ensure_future(fresh_decoration_arrives())
        started = time.monotonic()
        view = await evaluation_status(mock_lsp_client)
        await push
        assert time.monotonic() - started >= 0.04
        assert view.message == IDLE_CLEAN_SENTENCE
        assert view.files == []

    @pytest.mark.asyncio
    async def test_work_that_starts_inside_the_window_takes_the_busy_path(
        self, temp_theory_file, mock_lsp_client,
    ):
        # The edit made the prover re-run a command; by the time the window has
        # passed, something is running, and that is the answer.
        await _open_with(mock_lsp_client, temp_theory_file)
        processing.note_edit_sent()

        async def command_starts():
            await asyncio.sleep(0.01)
            # Through the tracker, as production would: the running-command
            # list is derived from the same ranges the sections render.
            mock_lsp_client._processing_trackers[temp_theory_file].running.append(
                (7, 0, 8, 5),
            )

        push = asyncio.ensure_future(command_starts())
        view = await evaluation_status(mock_lsp_client)
        await push
        assert view.status == "in_progress"
        assert view.message == "1 command is still running."

    @pytest.mark.asyncio
    async def test_an_edit_during_the_wait_re_arms_it(
        self, temp_theory_file, mock_lsp_client,
    ):
        # Debounce: a further edit during the wait re-arms the window, so the
        # tool answers only once the edits have stopped — and from the picture
        # after the LAST edit.
        await _open_with(mock_lsp_client, temp_theory_file, bad=[(2, 0, 2, 5)])
        processing.note_edit_sent()

        async def second_edit_then_fix():
            await asyncio.sleep(0.03)
            processing.note_edit_sent()
            mock_lsp_client._processing_trackers[temp_theory_file] = (
                MockProcessingTracker()
            )

        push = asyncio.ensure_future(second_edit_then_fix())
        started = time.monotonic()
        view = await evaluation_status(mock_lsp_client)
        await push
        # 0.03 s until the second edit, then a full re-armed 0.05 s window.
        assert time.monotonic() - started >= 0.07
        assert view.message == IDLE_CLEAN_SENTENCE

    @pytest.mark.asyncio
    async def test_without_a_recent_edit_it_answers_at_once(
        self, temp_theory_file, mock_lsp_client,
    ):
        await _open_with(mock_lsp_client, temp_theory_file, bad=[(2, 0, 2, 5)])
        started = time.monotonic()
        view = await evaluation_status(mock_lsp_client)
        assert time.monotonic() - started < 0.04
        assert "but 1 command failed." in view.message


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
        await _open_with(mock_lsp_client, temp_theory_file, bad=[(2, 0, 2, 5)])
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
            overview_error=[(2, 0, 2, 5)], bad=[(7, 0, 7, 3)],
        )
        with _patch_ensure(mock_lsp_client):
            result = await isabelle_evaluation_status()
        text = result.content[0].text
        assert text.startswith(
            "No evaluation in progress. Nothing is running, but 2 commands failed."
        )
        assert "errors: lines 3, 8" in text
        assert "Call isabelle_evaluation_status" not in text

    @pytest.mark.asyncio
    async def test_description_is_the_one_approved_sentence(self):
        # D-C8: the tool answers when idle too, so "the progress of an ongoing
        # evaluation" would be too narrow.
        tool = await mcp.get_tool("isabelle_evaluation_status")
        assert tool.description == "Check the current evaluation state."
