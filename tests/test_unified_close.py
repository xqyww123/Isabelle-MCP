"""The unified close (evaluation.close_settled_documents) and what it rests on:
the evaluation-target mark on DocumentState, the entry record, the open check
on the tracker getters, and the bounded lock helper; and the tool entry
(resync_and_check_freshness) that runs it.

The invariant the sweep enforces: an open document is an evaluation target,
or a file with something still wrong or in flight, or a file the breakpoint
registry mentions. Everything else is closed at the next tool-call entry —
unless the registry lock is busy, in which case the round is skipped and the
file stays open one round longer.
"""

import asyncio
import contextvars

import pytest

from isabelle_mcp import debugger, evaluation as ev
from isabelle_mcp.debugger import Breakpoint
from isabelle_mcp.evaluation import (
    close_settled_documents,
    evaluate_to,
    resync_and_check_freshness,
)
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.utils import acquire_within
from tests.conftest import (
    MockProcessingTracker,
    full_decoration_entries,
    theory_status_record,
)

OLDER = -19
NEWER = -40


def _row(path: str, **kw) -> dict:
    row = {
        "node_name": path, "theory_name": "T", "external": False, "imports": [],
        "ok": True, "total": 10, "unprocessed": 0, "running": 0, "warned": 0,
        "failed": 0, "finished": 10, "canceled": False, "consolidated": True,
        "percentage": 100,
    }
    row.update(kw)
    return row


def _entry(*rows: dict) -> None:
    ev.set_entry_record(theory_status_record(list(rows)))


def _settled(client, *paths: str) -> None:
    _entry(*(_row(p) for p in paths))


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
        _entry(_row(temp_theory_file, **unsettled))
        await close_settled_documents(mock_lsp_client)
        assert temp_theory_file in mock_lsp_client.open_documents

    async def test_a_file_absent_from_theory_status_stays_open(
        self, mock_lsp_client, temp_theory_file,
    ):
        # No row means nothing is known: the safe direction is to keep it.
        await mock_lsp_client.open_document(temp_theory_file)
        _entry()
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
        _entry(_row(target), _row(broken, ok=False, failed=1, finished=9),
               _row(temp_theory_file))
        await close_settled_documents(mock_lsp_client)
        assert set(mock_lsp_client.open_documents) == {target, broken}


class TestSweepDeferral:
    async def test_a_round_that_closed_something_ends_with_one_flush(
        self, mock_lsp_client, temp_theory_file, tmp_path,
    ):
        # A close of an edited file is a text edit the server absorbs in its
        # didClose handler: the round ends with ONE flush (no resync), after
        # the lock block, so the next served read rests on a picture at least
        # as new as the close. A round that closes nothing flushes nothing.
        other = _theory_file(tmp_path, "Other.thy")
        await mock_lsp_client.open_document(temp_theory_file)
        await mock_lsp_client.open_document(other)
        _settled(mock_lsp_client, temp_theory_file, other)
        await close_settled_documents(mock_lsp_client)
        assert mock_lsp_client.open_documents == {}
        assert mock_lsp_client.flush_calls == [False]
        assert not ev._evaluation_state_lock.locked()
        await close_settled_documents(mock_lsp_client)
        assert mock_lsp_client.flush_calls == [False]

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

    @staticmethod
    def _spy_entry(client) -> list[str]:
        """Record the entry's steps in order on the mock client."""
        order: list[str] = []
        real_resync = client.resync_changed_open_documents
        real_status = client.request_theory_status
        real_flush = client.flush

        async def resync():
            order.append("resync")
            await real_resync()

        async def status():
            order.append("theory_status")
            return await real_status()

        async def flush(*, resync_dependencies):
            order.append(f"flush(resync_dependencies={resync_dependencies})")
            return await real_flush(resync_dependencies=resync_dependencies)

        client.resync_changed_open_documents = resync
        client.request_theory_status = status
        client.flush = flush
        return order

    async def test_the_tool_entry_in_order(self, mock_lsp_client, temp_theory_file):
        # resync_and_check_freshness: the open-document sync, one flush with
        # resync_dependencies, the theory_status that becomes this call's
        # entry record, then the sweep — judged on that record — and the
        # sweep's own flush when it closed something.
        await mock_lsp_client.open_document(temp_theory_file)
        order = self._spy_entry(mock_lsp_client)
        await resync_and_check_freshness(mock_lsp_client)
        assert order == [
            "resync", "flush(resync_dependencies=True)", "theory_status",
            "flush(resync_dependencies=False)",
        ]
        assert [t.node_name for t in ev.entry_record().theories] == [temp_theory_file]
        # The mock reports every open document as settled: swept.
        assert temp_theory_file not in mock_lsp_client.open_documents

    async def test_the_cancel_tools_entry_does_the_sync_and_the_record_only(
        self, mock_lsp_client, temp_theory_file,
    ):
        # User ruling (round 2 (b), mutation control M-20): no flush request
        # and no unified close on the cancel tool's entry — a tool that serves
        # no picture does not wait for a document version, and the exempted
        # round closes no settled document (the next entry does).
        await mock_lsp_client.open_document(temp_theory_file)
        order = self._spy_entry(mock_lsp_client)
        await resync_and_check_freshness(mock_lsp_client, cancel_tool=True)
        assert order == ["resync", "theory_status"]
        assert temp_theory_file in mock_lsp_client.open_documents
        await resync_and_check_freshness(mock_lsp_client)
        assert temp_theory_file not in mock_lsp_client.open_documents

    async def test_the_entry_record_is_written_unconditionally(
        self, mock_lsp_client, temp_theory_file,
    ):
        # I-1b, mutation control M-16: the record's contract is its stamp,
        # never its freshness — a newer-stamped push processed between the
        # flush and the theory_status leaves the record written all the same
        # (its stamp is then older than the newest version).
        await mock_lsp_client.open_document(temp_theory_file, evaluation_target=True)
        real_status = mock_lsp_client.request_theory_status

        async def status():
            record = await real_status()
            mock_lsp_client.freshness.advance(NEWER)      # the push lands after
            return record

        mock_lsp_client.theory_status_stamp = OLDER
        mock_lsp_client.request_theory_status = status
        await resync_and_check_freshness(mock_lsp_client)
        assert ev.entry_record().document_version == OLDER
        assert mock_lsp_client.freshness.newest_document_version == NEWER

    async def test_two_overlapping_entries_each_read_their_own_record(
        self, mock_lsp_client, temp_theory_file,
    ):
        # The record is a per-call ContextVar, not a client attribute: two
        # tool calls in flight each read the record their OWN entry wrote.
        await mock_lsp_client.open_document(temp_theory_file, evaluation_target=True)
        my_stamp: contextvars.ContextVar[int] = contextvars.ContextVar("my_stamp")

        async def status():
            await asyncio.sleep(0.01)                      # let the calls overlap
            return theory_status_record([], my_stamp.get())

        mock_lsp_client.request_theory_status = status

        async def one_call(stamp: int) -> int:
            my_stamp.set(stamp)
            await resync_and_check_freshness(mock_lsp_client)
            await asyncio.sleep(0.02)
            return ev.entry_record().document_version

        a, b = await asyncio.gather(one_call(OLDER), one_call(NEWER))
        assert (a, b) == (OLDER, NEWER)


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

        async def notify(method, params, **kw):
            sent.append(method)

        client.notify = notify
        await client.open_document(temp_theory_file)
        doc = client.open_documents[temp_theory_file]
        assert not doc.is_evaluation_target
        await client.open_document(temp_theory_file, evaluation_target=True)
        assert client.open_documents[temp_theory_file] is doc   # the early exit
        assert doc.is_evaluation_target
        await client.open_document(temp_theory_file)
        assert doc.is_evaluation_target
        assert sent == ["textDocument/didOpen"]

    async def test_closing_a_marked_document_is_a_bug(self, temp_theory_file):
        client = IsabelleLSPClient()

        async def notify(method, params, **kw):
            pass

        client.notify = notify
        await client.open_document(temp_theory_file, evaluation_target=True)
        with pytest.raises(AssertionError, match="closing an evaluation target"):
            await client.close_document(temp_theory_file)
        assert temp_theory_file in client.open_documents


class TestTrackerOpenCheck:
    async def test_a_closed_documents_tracker_is_unreadable(self, temp_theory_file):
        # A push for a file this client does not hold open is not folded (it
        # still advances the newest version), so the all-empty ghost that
        # would read as "fully processed" is not representable — and the
        # getter answers None for a closed document whatever the map holds.
        client = IsabelleLSPClient()

        async def notify(method, params, **kw):
            pass

        client.notify = notify
        await client.open_document(temp_theory_file)
        await client._handle_decoration({
            "uri": client.open_documents[temp_theory_file].uri,
            "entries": full_decoration_entries(), "document_version": OLDER,
        })
        assert client.get_processing_tracker(temp_theory_file) is not None
        await client.close_document(temp_theory_file)
        await client._handle_decoration({
            "uri": f"file://{temp_theory_file}",
            "entries": full_decoration_entries(), "document_version": NEWER,
        })
        assert temp_theory_file not in client._processing_trackers    # no ghost
        assert client.get_processing_tracker(temp_theory_file) is None  # unreadable
        assert client.file_all_processed(temp_theory_file) is False
        assert client.freshness.newest_document_version == NEWER       # still counted

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
