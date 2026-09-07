r"""The `⌂` placeholder in command output resolves to `<file>:<line>` against a REAL
prover.

A position from a theory in the live document reaches the prover with only a
command id and an offset (PIDE sends commands without line or file), so
`Position.here` prints `\<^here>` -- rendered `⌂` -- which jEdit turns into a
hyperlink and this text-only client used to delete.  The Scala side now resolves
it through `Document.Snapshot.find_command_position`; the Python side relativizes
the path to the project root.  Both halves are pinned here: the raw reply of
`PIDE/output_at_position` carries the absolute path (the contract the unit
fixtures assume), and the tool's message carries the relative one.

    PATH=contrib/Isabelle2025-2/bin:$PATH pytest tests/integration -m integration \
        -k here_positions
"""
import os
import shutil

import pytest

from isabelle_mcp import evaluation as ev
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.tools.command_output import command_output
from isabelle_mcp.utils import LSPCharacter, LSPLine, MCPLine

from .test_debugger_probes import _evaluate_through

pytestmark = pytest.mark.integration

if shutil.which("isabelle") is None:
    pytest.skip("isabelle not on PATH", allow_module_level=True)


THEORY = '''theory Here_Check
  imports Main
begin

lemma "True" by simp

thm nonexistent_fact

end
'''
ERROR_LINE = 7  # thm nonexistent_fact -- "Undefined fact" carries a \<^here> position


@pytest.fixture
async def prover(tmp_path):
    ev.EVAL_POLL_INTERVAL = 4.0
    thy = os.path.join(str(tmp_path), "Here_Check.thy")
    with open(thy, "w") as f:
        f.write(THEORY)
    ev.evaluation_state = ev.EvaluationState()
    client = IsabelleLSPClient(
        logic="HOL",
        project_root=str(tmp_path),
        extra_args=["-o", "editor_tracing_messages=0"],
    )
    await client.start()
    try:
        yield client, thy
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_here_position_resolves_to_file_and_line(prover):
    client, thy = prover
    assert await _evaluate_through(client, thy, ERROR_LINE + 2), "evaluation did not settle"

    # The Scala half: the placeholder is replaced by the absolute path and 1-based line.
    raw = await client.get_output_at_position(thy, LSPLine(ERROR_LINE - 1), LSPCharacter(4))
    assert raw is not None
    _source, _range, html = raw
    assert "⌂" not in html
    assert f'<span class="position"> {os.path.realpath(thy)}:{ERROR_LINE}</span>' in html

    # The Python half: the agent sees the path relative to the project root.
    result = await command_output(client, thy, MCPLine(ERROR_LINE))
    assert [m.kind for m in result.messages] == ["error"]
    assert result.messages[0].message == (
        f'Undefined fact: "nonexistent_fact" Here_Check.thy:{ERROR_LINE}'
    )
