"""``isabelle_command_status``: the state of the commands covering given lines.

The internal position-state helper the evaluation guard uses answers by line and
from the local decoration cache. This tool answers the same question out loud,
so the agent can ask instead of discovering the answer by tripping over a
refusal — and it answers per *command*, which needs the server, because only the
server knows where one command stops and the next begins.
"""

from __future__ import annotations

import os
from collections import OrderedDict

from isabelle_mcp import processing
from isabelle_mcp.evaluation import relativize
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import CommandStatusLine, CommandStatusPosition, LinePosition
from isabelle_mcp.processing import ProcessingTracker
from isabelle_mcp.utils import IsabelleToolError, LSPLine, MCPLine, plural

# The state vocabulary, fixed and used nowhere else with another meaning. Two of
# them carry a hint, and for the same reason: they are the states where a bare
# word leaves the agent with no next step.
PROCESSED = "processed"
NOT_EVALUATED = "not evaluated"
CANCELLED = "cancelled, re-evaluate to get a result"
UNKNOWN = "unknown, retry in a few seconds"
NO_COMMAND = "no command"
FILE_NOT_OPEN = "file not open"


def _running(elapsed: float) -> str:
    return f"running for {int(elapsed)}s"


_STATES = {
    processing.PROCESSED: PROCESSED,
    processing.NOT_EVALUATED: NOT_EVALUATED,
    processing.CANCELLED: CANCELLED,
    processing.UNKNOWN: UNKNOWN,
}


def _state_word(state: str, elapsed: float) -> str:
    if state == processing.RUNNING:
        return _running(elapsed)
    return _STATES.get(state, UNKNOWN)


def _snippet(source: str) -> str:
    """A command's first line, truncated — enough to tell two commands on one
    line apart, which is the only thing the breakdown needs it for."""
    first = source.split("\n", 1)[0].strip()
    return (first[:60] + "...") if len(first) > 60 else first


async def command_status(
    client: IsabelleLSPClient, positions: list[LinePosition],
) -> list[CommandStatusLine]:
    """Answer each position with the state of every command covering its line.

    Positions are grouped by file and each file is asked once, so a bulk call
    costs one round trip per distinct file however many lines it names.
    """
    if not positions:
        raise IsabelleToolError("positions must not be empty")
    for pos in positions:
        if pos.line < 1:
            raise IsabelleToolError(f"line must be >= 1, got {pos.line}")

    # Grouped in first-seen order, and answered in the order asked: the agent
    # reads the reply against the list it sent.
    by_file: OrderedDict[str, list[LinePosition]] = OrderedDict()
    for pos in positions:
        by_file.setdefault(os.path.realpath(pos.file_path), []).append(pos)

    answers: dict[int, CommandStatusLine] = {}
    for file_path, group in by_file.items():
        lines = [LSPLine(int(MCPLine(pos.line).to_lsp())) for pos in group]
        # A file with no tracker has had no decoration, and an untouched tracker
        # answers NOT_EVALUATED for everything — which is exactly right, and is
        # the same rule position_state follows.
        tracker = client.get_processing_tracker(file_path) or ProcessingTracker()
        commands = None
        if file_path in client.open_documents:
            commands = await client.get_commands_at_lines(file_path, lines)

        for pos, lsp_line in zip(group, lines, strict=True):
            answers[id(pos)] = _answer(pos, lsp_line, commands, tracker)

    return [answers[id(pos)] for pos in positions]


def _answer(
    pos: LinePosition,
    lsp_line: LSPLine,
    commands: dict[int, list] | None,
    tracker: ProcessingTracker,
) -> CommandStatusLine:
    if commands is None:
        return CommandStatusLine(
            file_path=pos.file_path, line=pos.line, state=FILE_NOT_OPEN, commands=[],
        )
    found = commands.get(int(lsp_line), [])
    if not found:
        return CommandStatusLine(
            file_path=pos.file_path, line=pos.line, state=NO_COMMAND, commands=[],
        )

    per_command = []
    for rng, source in found:
        start = rng.get("start", {}).get("line", int(lsp_line))
        end = rng.get("end", {}).get("line", start)
        state, elapsed = tracker.range_state(start, end)
        per_command.append(
            CommandStatusPosition(state=_state_word(state, elapsed), text=_snippet(source)),
        )

    states = {c.state for c in per_command}
    if len(states) == 1:
        return CommandStatusLine(
            file_path=pos.file_path, line=pos.line, state=per_command[0].state, commands=[],
        )
    return CommandStatusLine(
        file_path=pos.file_path, line=pos.line, state="", commands=per_command,
    )


def format_command_status(result: list[CommandStatusLine], root: str | None) -> str:
    """One line per requested position; a breakdown only where the commands on a
    line disagree, because that is the only case where one state cannot speak for
    the whole line."""
    out = []
    for entry in result:
        where = f"{relativize(entry.file_path, root)}:{entry.line}"
        if not entry.commands:
            out.append(f"{where} — {entry.state}")
            continue
        out.append(f"{where} — {plural(len(entry.commands), 'command')}, states differ")
        width = max(len(c.state) for c in entry.commands)
        for c in entry.commands:
            out.append(f"  {c.state.ljust(width)}  {c.text}")
    return "\n".join(out)
