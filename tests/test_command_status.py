"""isabelle_command_status — the position-to-state table (§4.5)."""

from __future__ import annotations

import pytest

from isabelle_mcp import processing
from isabelle_mcp.models import LinePosition
from isabelle_mcp.processing import ProcessingTracker
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


def _range(start_line: int, end_line: int | None = None) -> dict:
    end = start_line if end_line is None else end_line
    return {"start": {"line": start_line, "character": 0},
            "end": {"line": end, "character": 40}}


class FakeClient:
    """Enough of IsabelleLSPClient for the tool: which files are open, what the
    server says the commands are, and one tracker per file."""

    project_root = "/proj"

    def __init__(self, *, commands: dict[str, dict[int, list]] | None = None,
                 trackers: dict[str, ProcessingTracker] | None = None):
        self._commands = commands or {}
        self._trackers = trackers or {}
        self.open_documents = dict.fromkeys(self._commands, object())
        self.requests: list[tuple[str, list[int]]] = []

    def get_processing_tracker(self, file_path):
        return self._trackers.get(file_path)

    async def get_commands_at_lines(self, file_path, lines):
        self.requests.append((file_path, [int(x) for x in lines]))
        per_file = self._commands.get(file_path)
        if per_file is None:
            return None
        return {int(line): per_file.get(int(line), []) for line in lines}


async def _tracker(**ranges) -> ProcessingTracker:
    tracker = ProcessingTracker()
    await tracker.update({
        "background_unprocessed1": ranges.get("unprocessed", []),
        "background_running1": ranges.get("running", []),
        "background_canceled": ranges.get("canceled", []),
    })
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


async def test_a_file_the_server_does_not_hold_says_so():
    client = FakeClient(commands={})
    [answer] = await command_status(
        client, [LinePosition(file_path="/proj/Missing.thy", line=3)])
    assert answer.state == FILE_NOT_OPEN
    assert format_command_status([answer], "/proj") == "Missing.thy:3 — file not open"


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

    client = FakeClient(
        commands={"/proj/My.thy": {41: [(_range(41), "by auto")]}},
        trackers={"/proj/My.thy": await _tracker()},
    )
    processing.note_edit_sent()
    try:
        [answer] = await command_status(
            client, [LinePosition(file_path="/proj/My.thy", line=42)])
        assert answer.state == UNKNOWN == "unknown, retry in a few seconds"
    finally:
        processing._last_edit_sent = float("-inf")


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
