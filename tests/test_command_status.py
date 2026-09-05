"""isabelle_command_status — the position-to-state table (§4.5)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from isabelle_mcp import evaluation as ev, processing
from isabelle_mcp.models import LinePosition
from isabelle_mcp.processing import FreshnessState, ProcessingTracker
from isabelle_mcp.tools.command_status import (
    CANCELLED,
    FILE_NOT_OPEN,
    NO_COMMAND,
    NOT_EVALUATED,
    PROCESSED,
    UNKNOWN,
    command_status,
    format_command_status,
)
from isabelle_mcp.utils import IsabelleToolError
from tests.conftest import full_decoration_entries, theory_status_record

NEWER = -40


def _range(start_line: int, end_line: int | None = None) -> dict:
    end = start_line if end_line is None else end_line
    return {"start": {"line": start_line, "character": 0},
            "end": {"line": end, "character": 40}}


def _doc(evaluation_target: bool = False) -> SimpleNamespace:
    """The shape the tool reads off an open document."""
    return SimpleNamespace(content="", is_evaluation_target=evaluation_target)


class FakeClient:
    """Enough of IsabelleLSPClient for the tool: which files are open, what the
    server says the commands are, one tracker per file, and — for the reopen
    of a theory the prover holds — the entry theory_status and open_document.

    ``commands`` lists the files that are open here AND held by the prover;
    ``held`` lists files the prover holds that are NOT open here (their
    commands become answerable once reopened); ``unheld_open`` lists files
    open here that the prover answers ``open: false`` for (the residual path).
    """

    project_root = "/proj"
    STALL_TIMEOUT = 60.0
    PROGRESS_CHECK_INTERVAL = 5.0

    def __init__(self, *, commands: dict[str, dict[int, list]] | None = None,
                 trackers: dict[str, ProcessingTracker] | None = None,
                 held: dict[str, dict[int, list]] | None = None,
                 unheld_open: tuple[str, ...] = ()):
        self._commands = dict(commands or {})
        self._trackers = trackers or {}
        self._held = held or {}
        self.open_documents = {p: _doc() for p in self._commands}
        for path in unheld_open:
            self.open_documents[path] = _doc()
        # The entry record: what the prover holds (the reopen reads it).
        ev.set_entry_record(theory_status_record(
            [{"node_name": p} for p in [*self._commands, *self._held]]))
        # The freshness state: the newest version stays 0, as every tracker's
        # stamp does, so a reopened file's picture is fresh once it exists.
        self.freshness = FreshnessState()
        self.flushes = 0
        self.requests: list[tuple[str, list[int]]] = []
        self.opened: list[tuple[str, bool]] = []

    def _check_server_health(self, stall_timeout: float) -> None:
        pass

    async def flush(self, *, resync_dependencies):
        self.flushes += 1
        snapshot = self.freshness.content_sends
        self.freshness.content_sends_flushed = max(self.freshness.content_sends_flushed, snapshot)
        await self.freshness.notify()
        return {"document_version": 0, "changed_uris": []}, snapshot

    def get_processing_tracker(self, file_path):
        if file_path not in self.open_documents:
            return None
        return self._trackers.get(file_path)

    async def open_document(self, file_path, *, evaluation_target=False, **_):
        self.opened.append((file_path, evaluation_target))
        doc = self.open_documents.get(file_path)
        if doc is not None:                      # the real client: the mark only rises
            doc.is_evaluation_target = doc.is_evaluation_target or evaluation_target
            return
        if file_path in self.unreadable:
            raise OSError(f"cannot read {file_path}")
        self.open_documents[file_path] = _doc(evaluation_target)
        self._commands[file_path] = self._held.pop(file_path)

    # Files the prover holds but the disk cannot give us (a reopen fails).
    unreadable: frozenset[str] = frozenset()

    async def set_caret(self, *a, **k):
        raise AssertionError("isabelle_command_status moved the caret")

    async def get_commands_at_lines(self, file_path, lines):
        self.requests.append((file_path, [int(x) for x in lines]))
        per_file = self._commands.get(file_path)
        if per_file is None:
            return None
        return {int(line): per_file.get(int(line), []) for line in lines}


async def _tracker(state: FreshnessState | None = None, **ranges) -> ProcessingTracker:
    """A real tracker with a full picture stamped 0 on its own freshness state
    (newest 0, nothing unflushed: fresh) unless *state* says otherwise."""
    tracker = ProcessingTracker(state or FreshnessState())
    await tracker.update(processing.parse_decoration_ranges(full_decoration_entries(
        background_unprocessed1=ranges.get("unprocessed", []),
        background_running1=ranges.get("running", []),
        background_canceled=ranges.get("canceled", []),
    )), 0)
    return tracker


async def test_one_command_per_line_reports_its_state():
    # 0-indexed line 41 is the 1-indexed line 42 the agent asks about.
    client = FakeClient(
        commands={"/proj/My.thy": {41: [(_range(41), 'lemma foo: "P"')]}},
        trackers={"/proj/My.thy": await _tracker()},
    )
    [answer] = await command_status(client, [LinePosition(file_path="/proj/My.thy", line=42)])
    assert answer.state == PROCESSED
    assert answer.commands == []
    assert format_command_status([answer], "/proj") == "My.thy:42 — processed"


async def test_agreeing_commands_on_one_line_do_not_get_a_breakdown():
    client = FakeClient(
        commands={"/proj/My.thy": {
            41: [(_range(41), 'have "P x"'), (_range(41), "by blast")]}},
        trackers={"/proj/My.thy": await _tracker()},
    )
    [answer] = await command_status(client, [LinePosition(file_path="/proj/My.thy", line=42)])
    assert answer.commands == []
    assert format_command_status([answer], "/proj") == "My.thy:42 — processed"


async def test_disagreeing_commands_get_a_breakdown():
    # The second command of the line is still running; one state cannot speak
    # for the line, so both are printed.
    client = FakeClient(
        commands={"/proj/My.thy": {
            41: [(_range(41), 'have "P x" by blast'), (_range(41, 43), "by auto")]}},
        trackers={"/proj/My.thy": await _tracker(running=[(42, 0, 43, 0)])},
    )
    [answer] = await command_status(client, [LinePosition(file_path="/proj/My.thy", line=42)])
    assert answer.state == ""
    assert [c.state for c in answer.commands] == [PROCESSED, "running for 0s"]
    rendered = format_command_status([answer], "/proj").split("\n")
    assert rendered[0] == "My.thy:42 — 2 commands, states differ"
    # The state column is padded to the widest state in the breakdown.
    assert rendered[1] == "  processed       have \"P x\" by blast"
    assert rendered[2] == "  running for 0s  by auto"


async def test_a_line_with_no_command_says_so():
    client = FakeClient(
        commands={"/proj/My.thy": {41: []}},
        trackers={"/proj/My.thy": await _tracker()},
    )
    [answer] = await command_status(client, [LinePosition(file_path="/proj/My.thy", line=42)])
    assert answer.state == NO_COMMAND


async def test_a_theory_the_prover_does_not_hold_is_not_evaluated():
    # Not open here and not held by the prover — a mistyped path, or a file
    # nothing has evaluated: nothing has been evaluated there, and that is
    # the answer. Nothing is opened for it.
    client = FakeClient(commands={})
    [answer] = await command_status(
        client, [LinePosition(file_path="/proj/Missing.thy", line=3)])
    assert answer.state == NOT_EVALUATED
    assert format_command_status([answer], "/proj") == "Missing.thy:3 — not evaluated"
    assert client.opened == [] and client.requests == []


async def test_a_theory_the_prover_holds_but_we_closed_is_reopened_and_answered():
    # The unified close tidied the file away; the prover still holds it. The
    # tool reopens it (marked, so it stays open) and answers from the live
    # tracker — never from nothing, never "not evaluated". Mutation control:
    # drop the reopen step and the answer degrades to `not evaluated`.
    client = FakeClient(
        held={"/proj/Swept.thy": {41: [(_range(41), 'lemma foo: "P"')]}},
    )
    client._trackers["/proj/Swept.thy"] = await _tracker()
    [answer] = await command_status(
        client, [LinePosition(file_path="/proj/Swept.thy", line=42)])
    assert client.opened == [("/proj/Swept.thy", True)]
    assert answer.state == PROCESSED


async def test_a_reopen_is_followed_by_one_wait_for_the_batch():
    # A reopened file has no picture until its first push: the tool waits
    # ONCE for the reopened batch (the wait's own flush is the witness) and
    # then answers from the picture — never `unknown` for a processed line.
    # A call that reopens nothing waits for nothing.
    client = FakeClient(
        held={"/proj/Swept.thy": {41: [(_range(41), "by auto")]}},
    )
    client._trackers["/proj/Swept.thy"] = await _tracker()
    [answer] = await command_status(
        client, [LinePosition(file_path="/proj/Swept.thy", line=42)])
    assert answer.state == PROCESSED
    assert client.flushes == 1
    [answer] = await command_status(
        client, [LinePosition(file_path="/proj/Swept.thy", line=42)])
    assert answer.state == PROCESSED and client.flushes == 1


async def test_the_batch_wait_is_keyed_on_freshness_not_on_a_position():
    # One reopened file, two positions: one evaluated, one past the frontier.
    # A wait keyed on the farthest position's verdict would return at once
    # (`not evaluated` needs no fresh picture) and leave the evaluated line
    # answered `unknown`; the wait is keyed on the pictures being fresh, so
    # both answer truthfully, whatever order the positions are asked in.
    for lines in ([42, 200], [200, 42]):
        client = FakeClient(
            held={"/proj/Half.thy": {
                41: [(_range(41), "by auto")], 199: [(_range(199), "by auto")]}},
        )
        client._trackers["/proj/Half.thy"] = await _tracker(unprocessed=[(100, 0, 300, 0)])
        answers = await command_status(
            client, [LinePosition(file_path="/proj/Half.thy", line=n) for n in lines])
        assert {a.line: a.state for a in answers} == {42: PROCESSED, 200: NOT_EVALUATED}


async def test_the_batch_wait_covers_a_second_reopened_file():
    # Two reopened files: the first entirely unevaluated, the second fully
    # processed. One wait, over the reopened batch, serves both.
    client = FakeClient(held={
        "/proj/A.thy": {0: [(_range(0), "theory A")]},
        "/proj/B.thy": {41: [(_range(41), "by auto")]},
    })
    client._trackers["/proj/A.thy"] = await _tracker(unprocessed=[(0, 0, 50, 0)])
    client._trackers["/proj/B.thy"] = await _tracker()
    answers = await command_status(client, [
        LinePosition(file_path="/proj/A.thy", line=1),
        LinePosition(file_path="/proj/B.thy", line=42),
    ])
    assert [a.state for a in answers] == [NOT_EVALUATED, PROCESSED]
    assert client.flushes == 1


async def test_no_position_query_writes_the_run_or_the_caret(trap_run_writes):
    # Ruling 22's structural invariant for isabelle_command_status: the
    # reopen branch and the not-open branch both complete with every
    # run-writing entry point booby-trapped (set_caret asserts on the fake).
    from isabelle_mcp import evaluation as ev
    client = FakeClient(
        held={"/proj/Swept.thy": {41: [(_range(41), "by auto")]}},
    )
    client._trackers["/proj/Swept.thy"] = await _tracker()
    answers = await command_status(client, [
        LinePosition(file_path="/proj/Swept.thy", line=42),
        LinePosition(file_path="/proj/Ghost.thy", line=3),
    ])
    assert [a.state for a in answers] == [PROCESSED, NOT_EVALUATED]
    assert client.opened == [("/proj/Swept.thy", True)]
    assert not ev.evaluation_state.active


async def test_one_failed_reopen_never_ends_a_multi_position_call():
    # A theory the prover holds but the disk cannot give back: its positions
    # keep the not-open answer, every other position is answered, and the
    # published contract — one line per asked position, in order — holds.
    client = FakeClient(
        commands={"/proj/Fine.thy": {41: [(_range(41), "by auto")]}},
        held={"/proj/Gone.thy": {41: [(_range(41), "by auto")]}},
        trackers={},
    )
    client._trackers["/proj/Fine.thy"] = await _tracker()
    client.unreadable = frozenset({"/proj/Gone.thy"})
    answers = await command_status(client, [
        LinePosition(file_path="/proj/Gone.thy", line=42),
        LinePosition(file_path="/proj/Fine.thy", line=42),
        LinePosition(file_path="/proj/Gone.thy", line=43),
    ])
    assert [(a.file_path, a.line, a.state) for a in answers] == [
        ("/proj/Gone.thy", 42, NOT_EVALUATED),
        ("/proj/Fine.thy", 42, PROCESSED),
        ("/proj/Gone.thy", 43, NOT_EVALUATED),
    ]
    assert "/proj/Gone.thy" not in client.open_documents


async def test_file_not_open_is_produced_only_by_the_residual_path():
    # Open here, yet the prover answers ``open: false``: the one bookkeeping
    # mismatch the state word is left for. Nothing else produces it for a .thy.
    client = FakeClient(unheld_open=("/proj/Odd.thy",))
    [answer] = await command_status(
        client, [LinePosition(file_path="/proj/Odd.thy", line=3)])
    assert answer.state == FILE_NOT_OPEN
    assert format_command_status([answer], "/proj") == "Odd.thy:3 — file not open"
    assert client.opened == []


async def test_an_ml_position_keeps_its_answer_of_old():
    # A .ML blob is never a document of ours and is never reopened; its
    # answer is byte for byte what it was.
    client = FakeClient(commands={})
    ev.set_entry_record(theory_status_record([{"node_name": "/proj/Blob.ML"}]))
    [answer] = await command_status(
        client, [LinePosition(file_path="/proj/Blob.ML", line=3)])
    assert answer.state == FILE_NOT_OPEN
    assert format_command_status([answer], "/proj") == "Blob.ML:3 — file not open"
    assert client.opened == []


async def test_a_file_with_no_decoration_yet_is_not_evaluated():
    # No tracker at all: nothing has been processed, which is what the agent
    # must act on.
    client = FakeClient(
        commands={"/proj/My.thy": {41: [(_range(41), "lemma foo")]}})
    [answer] = await command_status(client, [LinePosition(file_path="/proj/My.thy", line=42)])
    assert answer.state == NOT_EVALUATED


async def test_cancelled_and_unknown_carry_their_hints():
    # Both are states where a bare word would leave the agent with no next step.
    client = FakeClient(
        commands={"/proj/My.thy": {41: [(_range(41), "by auto")]}},
        trackers={"/proj/My.thy": await _tracker(canceled=[(41, 0, 41, 9)])},
    )
    [answer] = await command_status(client, [LinePosition(file_path="/proj/My.thy", line=42)])
    assert answer.state == CANCELLED == "cancelled, re-evaluate to get a result"

    # A file open all along whose picture is not fresh (a newer version was
    # seen): `unknown`, and the tool never waits for it (ruling 26).
    state = FreshnessState()
    client = FakeClient(
        commands={"/proj/My.thy": {41: [(_range(41), "by auto")]}},
        trackers={"/proj/My.thy": await _tracker(state)},
    )
    state.advance(NEWER)
    [answer] = await command_status(
        client, [LinePosition(file_path="/proj/My.thy", line=42)])
    assert answer.state == UNKNOWN == "unknown, retry in a few seconds"
    assert client.flushes == 0


async def test_a_command_spanning_lines_answers_every_line_it_covers():
    # The proof runs 10..14 (0-indexed 9..13) and is still running; asking about
    # a line in its middle reports that proof's command.
    span = _range(9, 13)
    client = FakeClient(
        commands={"/proj/My.thy": {9: [(span, "proof -")], 11: [(span, "proof -")]}},
        trackers={"/proj/My.thy": await _tracker(running=[(9, 0, 13, 0)])},
    )
    answers = await command_status(client, [
        LinePosition(file_path="/proj/My.thy", line=10),
        LinePosition(file_path="/proj/My.thy", line=12),
    ])
    assert [a.state for a in answers] == ["running for 0s", "running for 0s"]


async def test_positions_are_grouped_by_file_and_answered_in_the_order_asked():
    client = FakeClient(
        commands={
            "/proj/A.thy": {0: [(_range(0), "theory A")], 4: [(_range(4), "lemma a")]},
            "/proj/B.thy": {2: [(_range(2), "lemma b")]},
        },
        trackers={"/proj/A.thy": await _tracker(), "/proj/B.thy": await _tracker()},
    )
    answers = await command_status(client, [
        LinePosition(file_path="/proj/A.thy", line=1),
        LinePosition(file_path="/proj/B.thy", line=3),
        LinePosition(file_path="/proj/A.thy", line=5),
    ])
    # One round trip per distinct file, however many lines it names.
    assert client.requests == [("/proj/A.thy", [0, 4]), ("/proj/B.thy", [2])]
    assert [(a.file_path, a.line) for a in answers] == [
        ("/proj/A.thy", 1), ("/proj/B.thy", 3), ("/proj/A.thy", 5)]


async def test_repeated_positions_are_each_answered():
    client = FakeClient(
        commands={"/proj/My.thy": {41: [(_range(41), "by auto")]}},
        trackers={"/proj/My.thy": await _tracker()},
    )
    pos = [LinePosition(file_path="/proj/My.thy", line=42)] * 2
    answers = await command_status(client, pos)
    assert len(answers) == 2


async def test_bad_input_is_refused():
    client = FakeClient()
    with pytest.raises(IsabelleToolError):
        await command_status(client, [])
    with pytest.raises(IsabelleToolError):
        await command_status(client, [LinePosition(file_path="/proj/My.thy", line=0)])
