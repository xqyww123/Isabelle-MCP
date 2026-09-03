"""The unified close (evaluation.close_settled_documents) and what it rests on:
the evaluation-target mark on DocumentState, the entry theory_status stash,
the open check on the tracker getters, and the bounded lock helper.

The invariant the sweep enforces: an open document is an evaluation target,
or a file with something still wrong or in flight, or a file the breakpoint
registry mentions. Everything else is closed at the next tool-call entry —
unless the post-edit grace gate is open or the registry lock is busy, in
which case the round is skipped and the file stays open one round longer.
"""

import asyncio

import pytest

from isabelle_mcp import debugger, evaluation as ev, processing
from isabelle_mcp.debugger import Breakpoint
from isabelle_mcp.evaluation import (
    _parse_theory_status,
    close_settled_documents,
    evaluate_to,
    resync_and_check_freshness,
)
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.utils import acquire_within
from tests.conftest import MockProcessingTracker


def _row(path: str, **kw) -> dict:
    row = {
        "node_name": path, "theory_name": "T", "external": False, "imports": [],
        "ok": True, "total": 10, "unprocessed": 0, "running": 0, "warned": 0,
        "failed": 0, "finished": 10, "canceled": False, "consolidated": True,
        "percentage": 100,
    }
    row.update(kw)
    return row


def _settled(client, *paths: str) -> None:
    client.entry_theories = [_parse_theory_status(_row(p)) for p in paths]


def _theory_file(tmp_path, name: str) -> str:
    path = tmp_path / name
    path.write_text(f"theory {path.stem}\nimports Main\nbegin\nend\n")
    return str(path)


class TestSweepCriterion:
    async def test_a_settled_unmarked_file_is_closed(self, mock_lsp_client, temp_theory_file):
        await mock_lsp_client.open_document(temp_theory_file)
        _settled(mock_lsp_client, temp_theory_file)
        await close_settled_documents(mock_lsp_client)
        assert temp_theory_file not in mock_lsp_client.open_documents

    async def test_an_evaluation_target_is_never_closed(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        # Observed at the close call, not at the open set: close_document's
        # own guard would refuse a marked document anyway, and the sweep's
        # per-file exception guard would swallow that refusal — so "still
        # open" alone cannot tell the criterion from the guard rail.
        attempted = []

        async def recording_close(path):
            attempted.append(path)

        monkeypatch.setattr(mock_lsp_client, "close_document", recording_close)
        await mock_lsp_client.open_document(temp_theory_file, evaluation_target=True)
        _settled(mock_lsp_client, temp_theory_file)
        await close_settled_documents(mock_lsp_client)
        assert attempted == []

    @pytest.mark.parametrize("unsettled", [
        {"ok": False, "failed": 1, "finished": 9},
        {"unprocessed": 3, "finished": 7, "consolidated": False},
        {"running": 1, "finished": 9, "consolidated": False},
    ])
    async def test_a_file_with_something_wrong_or_in_flight_stays_open(
        self, mock_lsp_client, temp_theory_file, unsettled,
    ):
        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client.entry_theories = [
            _parse_theory_status(_row(temp_theory_file, **unsettled)),
        ]
        await close_settled_documents(mock_lsp_client)
        assert temp_theory_file in mock_lsp_client.open_documents

    async def test_a_file_absent_from_theory_status_stays_open(
        self, mock_lsp_client, temp_theory_file,
    ):
        # No row means nothing is known: the safe direction is to keep it.
        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client.entry_theories = []
        await close_settled_documents(mock_lsp_client)
        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.parametrize("state", [debugger.ARMED, debugger.PENDING])
    async def test_a_file_the_breakpoint_registry_mentions_stays_open(
        self, mock_lsp_client, temp_theory_file, state,
    ):
        # Any entry, armed or pending: closing the file would silently demote
        # every breakpoint on it.
        await mock_lsp_client.open_document(temp_theory_file)
        _settled(mock_lsp_client, temp_theory_file)
        entry = Breakpoint(file_path=temp_theory_file, line=4, anchor="end", state=state)
        debugger.registry.entries.append(entry)
        try:
            await close_settled_documents(mock_lsp_client)
            assert temp_theory_file in mock_lsp_client.open_documents
        finally:
            debugger.registry.entries.remove(entry)

    async def test_the_criterion_is_per_file(self, mock_lsp_client, temp_theory_file, tmp_path):
        target = _theory_file(tmp_path, "Target.thy")
        broken = _theory_file(tmp_path, "Broken.thy")
        await mock_lsp_client.open_document(target, evaluation_target=True)
        await mock_lsp_client.open_document(broken)
        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client.entry_theories = [
            _parse_theory_status(_row(target)),
            _parse_theory_status(_row(broken, ok=False, failed=1, finished=9)),
            _parse_theory_status(_row(temp_theory_file)),
        ]
        await close_settled_documents(mock_lsp_client)
        assert set(mock_lsp_client.open_documents) == {target, broken}


class TestSweepDeferral:
    async def test_the_grace_gate_defers_the_whole_round(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        await mock_lsp_client.open_document(temp_theory_file)
        _settled(mock_lsp_client, temp_theory_file)
        monkeypatch.setattr(processing, "DECORATION_GRACE", 100.0)
        processing.note_edit_sent()
        started = asyncio.get_running_loop().time()
        await close_settled_documents(mock_lsp_client)
        # Non-blocking: no wait for the gate, nothing closed this round.
        assert asyncio.get_running_loop().time() - started < 0.5
        assert temp_theory_file in mock_lsp_client.open_documents

    async def test_a_busy_registry_lock_skips_the_round(
        self, mock_lsp_client, temp_theory_file,
    ):
        await mock_lsp_client.open_document(temp_theory_file)
        _settled(mock_lsp_client, temp_theory_file)
        async with debugger.registry.lock:      # a breakpoint tool mid-transaction
            await close_settled_documents(mock_lsp_client)
        assert temp_theory_file in mock_lsp_client.open_documents
        # The next round, with the lock free, closes it.
        await close_settled_documents(mock_lsp_client)
        assert temp_theory_file not in mock_lsp_client.open_documents

    async def test_the_sweep_holds_the_evaluation_state_lock(
        self, mock_lsp_client, temp_theory_file,
    ):
        await mock_lsp_client.open_document(temp_theory_file)
        _settled(mock_lsp_client, temp_theory_file)
        async with ev._evaluation_state_lock:
            sweep = asyncio.create_task(close_settled_documents(mock_lsp_client))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert temp_theory_file in mock_lsp_client.open_documents   # waiting on us
        await sweep
        assert temp_theory_file not in mock_lsp_client.open_documents

    async def test_a_started_close_completes_under_an_outer_cancel(
        self, mock_lsp_client, temp_theory_file, tmp_path, monkeypatch,
    ):
        # Each close is shielded: a cancel delivered while didClose is on the
        # wire must not abort it (an orphan on the server) nor the closes
        # still to come. (Mutation control: drop shield=True and this reds.)
        import anyio
        other = _theory_file(tmp_path, "Other.thy")
        await mock_lsp_client.open_document(temp_theory_file)
        await mock_lsp_client.open_document(other)
        _settled(mock_lsp_client, temp_theory_file, other)
        real_close = mock_lsp_client.close_document

        async def slow_close(path):
            await asyncio.sleep(0.05)       # the checkpoint the cancel lands on
            await real_close(path)

        monkeypatch.setattr(mock_lsp_client, "close_document", slow_close)
        with anyio.move_on_after(0.02):
            await close_settled_documents(mock_lsp_client)
        assert mock_lsp_client.open_documents == {}
        assert not ev._evaluation_state_lock.locked()
        assert not debugger.registry.lock.locked()

    async def test_a_stuck_close_is_bounded_and_the_next_file_still_closes(
        self, mock_lsp_client, temp_theory_file, tmp_path, monkeypatch,
    ):
        # One stalled pipe must not hang the tool call that ran the sweep:
        # the close is bounded by _CLOSE_TIMEOUT, the stuck file stays open,
        # the next file is closed. (Mutation control: drop move_on_after.)
        other = _theory_file(tmp_path, "Other.thy")
        await mock_lsp_client.open_document(temp_theory_file)
        await mock_lsp_client.open_document(other)
        _settled(mock_lsp_client, temp_theory_file, other)
        real_close = mock_lsp_client.close_document

        async def stuck_then_fine(path):
            if path == temp_theory_file:
                await asyncio.sleep(1.0)    # never comes back in time
            await real_close(path)

        monkeypatch.setattr(mock_lsp_client, "close_document", stuck_then_fine)
        monkeypatch.setattr(ev, "_CLOSE_TIMEOUT", 0.02)
        started = asyncio.get_running_loop().time()
        await close_settled_documents(mock_lsp_client)
        assert asyncio.get_running_loop().time() - started < 0.5
        assert temp_theory_file in mock_lsp_client.open_documents
        assert other not in mock_lsp_client.open_documents

    async def test_one_failing_close_does_not_stop_the_others(
        self, mock_lsp_client, temp_theory_file, tmp_path, monkeypatch,
    ):
        other = _theory_file(tmp_path, "Other.thy")
        await mock_lsp_client.open_document(temp_theory_file)
        await mock_lsp_client.open_document(other)
        _settled(mock_lsp_client, temp_theory_file, other)
        real_close = mock_lsp_client.close_document
        closed = []

        async def flaky_close(path):
            if path == temp_theory_file:
                raise RuntimeError("pipe broken")
            closed.append(path)
            await real_close(path)

        monkeypatch.setattr(mock_lsp_client, "close_document", flaky_close)
        await close_settled_documents(mock_lsp_client)   # no exception escapes
        assert closed == [other]

    async def test_the_sweep_runs_last_at_the_tool_entry(
        self, mock_lsp_client, temp_theory_file, monkeypatch,
    ):
        # resync_and_check_freshness: Layer 2, Layer 3 (which stashes the entry
        # theory_status), then the sweep — judged on that fresh stash.
        await mock_lsp_client.open_document(temp_theory_file)
        order = []
        real_resync = mock_lsp_client.resync_changed_open_documents
        real_status = mock_lsp_client.request_theory_status

        async def resync():
            order.append("resync")
            await real_resync()

        async def status():
            order.append("theory_status")
            return await real_status()

        mock_lsp_client.resync_changed_open_documents = resync
        mock_lsp_client.request_theory_status = status
        mock_lsp_client._dep_stat_sigs = {}
        mock_lsp_client.vscode_load_delay = 0.5
        await resync_and_check_freshness(mock_lsp_client)
        assert order == ["resync", "theory_status"]
        assert [t.node_name for t in mock_lsp_client.entry_theories] == [temp_theory_file]
        # The mock reports every open document as settled: swept.
        assert temp_theory_file not in mock_lsp_client.open_documents


class TestEvaluationTargetMark:
    async def test_evaluate_to_marks_its_file(self, mock_lsp_client, temp_theory_file):
        await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert mock_lsp_client.open_documents[temp_theory_file].is_evaluation_target

    async def test_the_mark_only_ever_rises(self, mock_lsp_client, temp_theory_file):
        # An auto-open of an already-marked file passes the default and must
        # not erase the mark; a later marked open of an unmarked file sets it.
        await mock_lsp_client.open_document(temp_theory_file, evaluation_target=True)
        await mock_lsp_client.open_document(temp_theory_file)
        assert mock_lsp_client.open_documents[temp_theory_file].is_evaluation_target
        mock_lsp_client.open_documents.clear()
        await mock_lsp_client.open_document(temp_theory_file)
        assert not mock_lsp_client.open_documents[temp_theory_file].is_evaluation_target
        await mock_lsp_client.open_document(temp_theory_file, evaluation_target=True)
        assert mock_lsp_client.open_documents[temp_theory_file].is_evaluation_target

    async def test_the_real_client_raises_the_mark_on_every_exit(self, temp_theory_file):
        client = IsabelleLSPClient()
        sent = []

        async def notify(method, params):
            sent.append(method)

        client.notify = notify
        await client.open_document(temp_theory_file, wait_for_decoration=False)
        doc = client.open_documents[temp_theory_file]
        assert not doc.is_evaluation_target
        await client.open_document(
            temp_theory_file, wait_for_decoration=False, evaluation_target=True,
        )
        assert client.open_documents[temp_theory_file] is doc   # the early exit
        assert doc.is_evaluation_target
        await client.open_document(temp_theory_file, wait_for_decoration=False)
        assert doc.is_evaluation_target
        assert sent == ["textDocument/didOpen"]

    async def test_closing_a_marked_document_is_a_bug(self, temp_theory_file):
        client = IsabelleLSPClient()

        async def notify(method, params):
            pass

        client.notify = notify
        await client.open_document(
            temp_theory_file, wait_for_decoration=False, evaluation_target=True,
        )
        with pytest.raises(AssertionError, match="closing an evaluation target"):
            await client.close_document(temp_theory_file)
        assert temp_theory_file in client.open_documents


class TestTrackerOpenCheck:
    async def test_a_closed_documents_tracker_is_unreadable(self, temp_theory_file):
        # The erase push after didClose rebuilds an all-empty tracker — a ghost
        # that would read as "fully processed". The getter hides it.
        client = IsabelleLSPClient()

        async def notify(method, params):
            pass

        client.notify = notify
        await client.open_document(temp_theory_file, wait_for_decoration=False)
        await client._handle_decoration({
            "uri": client.open_documents[temp_theory_file].uri,
            "entries": [{"type": "background_unprocessed1", "content": []}],
        })
        assert client.get_processing_tracker(temp_theory_file) is not None
        await client.close_document(temp_theory_file)
        await client._handle_decoration({
            "uri": f"file://{temp_theory_file}",
            "entries": [{"type": "background_unprocessed1", "content": []}],
        })
        assert temp_theory_file in client._processing_trackers        # the ghost
        assert client.get_processing_tracker(temp_theory_file) is None  # unreadable
        assert client.file_all_processed(temp_theory_file) is False

    def test_the_mock_mirrors_the_open_check(self, mock_lsp_client):
        mock_lsp_client._processing_trackers["/tmp/Ghost.thy"] = MockProcessingTracker()
        assert mock_lsp_client.get_processing_tracker("/tmp/Ghost.thy") is None


class TestAcquireWithin:
    async def test_a_free_lock_is_taken_and_released(self):
        lock = asyncio.Lock()
        async with acquire_within(lock, 0.05) as held:
            assert held and lock.locked()
        assert not lock.locked()

    async def test_a_busy_lock_yields_false_without_blocking(self):
        lock = asyncio.Lock()
        async with lock:
            async with acquire_within(lock, 0.05) as held:
                assert not held
            assert lock.locked()    # still the outer holder's

    async def test_release_survives_an_exception_in_the_block(self):
        lock = asyncio.Lock()
        with pytest.raises(RuntimeError):
            async with acquire_within(lock, 0.05) as held:
                assert held
                raise RuntimeError("boom")
        assert not lock.locked()

    async def test_a_non_positive_timeout_is_refused(self):
        # wait_for with timeout <= 0 never acquires even a free lock.
        with pytest.raises(AssertionError):
            async with acquire_within(asyncio.Lock(), 0):
                pass
