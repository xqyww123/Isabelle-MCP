"""End-to-end tests for the position-explicit query tools, against a REAL prover.

These cover what unit tests cannot: that the ML prelude, the Scala adapter and
the Python client agree, and that the statuses of §5.3 are produced by the
situations they claim to describe rather than only by a mock.

The headline is the first test: `isabelle_goal` and `isabelle_find_theorems`
answering *while an evaluation is running*. That is the whole point of the
upgrade — before it, both were refused outright for as long as one was in
flight, because they and the evaluation both drove Isabelle's global caret.

Marked ``integration`` (deselected by default) and skipped unless ``isabelle``
is on PATH:

    PATH=contrib/Isabelle2025-2/bin:$PATH pytest tests/integration -m integration
"""
import asyncio
import os
import shutil

import pytest

from isabelle_mcp import evaluation as ev
from isabelle_mcp.evaluation import evaluate_to, evaluation_status
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.tools.find_theorems import find_theorems
from isabelle_mcp.tools.goal import goal
from isabelle_mcp.utils import IsabelleToolError, LSPLine, MCPLine

pytestmark = pytest.mark.integration

if shutil.which("isabelle") is None:
    pytest.skip("isabelle not on PATH", allow_module_level=True)


# Two theories, because one of them must be evaluable to its very last line and
# the other must contain a command that runs for a minute and a half.
#
# 1-indexed lines, named so the assertions read as questions about the theory.
SLOW = '''theory QE2E
imports Main
begin

definition two :: nat where "two = 2"

lemma unicode: "\\<forall>(x::nat) y. x \\<le> y \\<longrightarrow> x \\<le> y + 1"
  by auto

lemma target: "(x::nat) + 0 = x"
  apply (induct x)
   apply simp
  apply simp
  done

ML \\<open>OS.Process.sleep (Time.fromSeconds 90)\\<close>

lemma after_the_sleep: "two + 0 = two"
  by (simp add: two_def)

end
'''
DEFINITION = 5
UNICODE_LEMMA = 7
TARGET_LEMMA = 10
INDUCT = 11
FIRST_SIMP = 12
DONE = 14
SLEEP = 16
AFTER_SLEEP = 18

QUICK = '''theory QQuick
imports Main
begin

definition two :: nat where "two = 2"

lemma unicode: "\\<forall>(x::nat) y. x \\<le> y \\<longrightarrow> x \\<le> y + 1"
  by auto

lemma target: "(x::nat) + 0 = x"
  apply (induct x)
   apply simp
  apply simp
  done

end
'''
QUICK_END = 16


def _caret(source: str, line_1indexed: int) -> int:
    """The column the tools resolve to: the line's last non-blank character."""
    text = source.split("\n")[line_1indexed - 1].rstrip()
    return len(text) - 1 if text else 0


@pytest.fixture
async def prover(tmp_path):
    """One prover, two theories: the slow one and the quick one."""
    ev.EVAL_POLL_INTERVAL = 4.0
    slow = os.path.join(str(tmp_path), "QE2E.thy")
    quick = os.path.join(str(tmp_path), "QQuick.thy")
    for path, source in ((slow, SLOW), (quick, QUICK)):
        with open(path, "w") as f:
            f.write(source)
    client = IsabelleLSPClient(logic="HOL", project_root=str(tmp_path))
    await client.start()
    try:
        yield client, slow, quick
    finally:
        await client.shutdown()


# "no_evaluation" is what the status reports once nothing is outstanding, and
# evaluate_to itself may already return complete — so check its own answer first.
_SETTLED = ("complete", "no_evaluation")


async def _evaluate_through(client, path, line, tries=40):
    view = await evaluate_to(client, path, line)
    for _ in range(tries):
        if view.status in _SETTLED:
            return True
        await asyncio.sleep(2)
        view = await evaluation_status(client)
    return False


async def _start_the_sleep(client, path, tries=30):
    """Begin evaluating the whole file and wait until the ML sleep is running."""
    await evaluate_to(client, path, -1)
    for _ in range(tries):
        await asyncio.sleep(2)
        view = await evaluation_status(client)
        if any(c.start_line == SLEEP for c in view.running_commands):
            return True
    return False


@pytest.mark.asyncio
async def test_both_tools_answer_while_an_evaluation_is_running(prover):
    client, slow, quick = prover
    assert await _evaluate_through(client, slow, DONE), "the proof never finished"
    assert await _start_the_sleep(client, slow), "the ML sleep never started"

    # The evaluation is stuck on line 16 and everything after it is pending —
    # and both tools still answer about line 11, which finished long ago.
    state = await goal(client, slow, MCPLine(INDUCT))
    assert state.subgoals == [
        "0 + 0 = 0",
        "⋀x. x + 0 = x ⟹ Suc x + 0 = Suc x",
    ]
    found = await find_theorems(client, slow, MCPLine(INDUCT), names=["add_0"], limit=3)
    assert found.found and found.found > 0
    assert found.theorems

    assert (await evaluation_status(client)).status == "in_progress"


@pytest.mark.asyncio
async def test_each_step_of_a_proof_reports_its_own_state(prover):
    client, slow, quick = prover
    assert await _evaluate_through(client, quick, QUICK_END)

    assert (await goal(client, quick, MCPLine(TARGET_LEMMA))).subgoals == ["x + 0 = x"]
    assert (await goal(client, quick, MCPLine(INDUCT))).subgoals == [
        "0 + 0 = 0", "⋀x. x + 0 = x ⟹ Suc x + 0 = Suc x",
    ]
    assert (await goal(client, quick, MCPLine(FIRST_SIMP))).subgoals == [
        "⋀x. x + 0 = x ⟹ Suc x + 0 = Suc x",
    ]


@pytest.mark.asyncio
async def test_a_command_with_no_proof_state_says_so_instead_of_timing_out(prover):
    client, slow, quick = prover
    assert await _evaluate_through(client, quick, QUICK_END)

    result = await goal(client, quick, MCPLine(DEFINITION))
    assert result.subgoals == []
    assert result.note is not None
    assert "not a proof operation" in result.note


@pytest.mark.asyncio
async def test_past_the_end_of_the_theory_there_is_nothing_to_search(prover):
    client, slow, quick = prover
    # "end" leaves the pristine toplevel, which has no context at all.
    assert await _evaluate_through(client, quick, QUICK_END)

    with pytest.raises(IsabelleToolError, match="no theory context"):
        await find_theorems(client, quick, MCPLine(QUICK_END), names=["add"])


@pytest.mark.asyncio
async def test_a_line_past_the_end_of_the_file_has_no_command(prover):
    client, slow, quick = prover
    assert await _evaluate_through(client, quick, QUICK_END)

    reply = await client.get_proof_state_at_position(quick, LSPLine(999), 0)
    assert reply.status == "no_command"


@pytest.mark.asyncio
async def test_a_running_command_has_not_finished_and_an_interrupted_one_says_so(prover):
    client, slow, quick = prover
    assert await _evaluate_through(client, slow, DONE)
    assert await _start_the_sleep(client, slow)

    running = await client.get_proof_state_at_position(
        slow, LSPLine(SLEEP - 1), _caret(SLOW, SLEEP))
    assert running.status == "unfinished"

    # cancel_execution alone, NOT cancel_evaluation: the latter goes through
    # force_interrupt, whose synthetic edit re-creates every command id, and the
    # entry is then gone rather than interrupted.
    await client.cancel_execution()
    await asyncio.sleep(1.0)
    interrupted = await client.get_proof_state_at_position(
        slow, LSPLine(SLEEP - 1), _caret(SLOW, SLEEP))
    assert interrupted.status == "interrupted"
    # Commands that finished before the cancel are untouched.
    assert (await client.get_proof_state_at_position(
        slow, LSPLine(INDUCT - 1), _caret(SLOW, INDUCT))).status == "ok"


@pytest.mark.asyncio
async def test_a_cancelled_query_is_answered_rather_than_orphaned(prover):
    client, slow, quick = prover
    assert await _evaluate_through(client, quick, QUICK_END)

    async def slow_query():
        return await client.request("PIDE/find_theorems_at_position", {
            "token": "cancel-me",
            "textDocument": {"uri": "file://" + quick},
            "position": {"line": INDUCT - 1, "character": _caret(QUICK, INDUCT)},
            "timeout": 600.0, "query": "", "limit": "20000", "allow_dups": "false",
        })

    task = asyncio.create_task(slow_query())
    await asyncio.sleep(0.4)
    await client.notify("PIDE/query_cancel", {"token": "cancel-me"})
    # The point of the assertion is that this returns at all: whoever takes the
    # request out of the table owes it a reply.
    result = await asyncio.wait_for(task, timeout=60)
    assert result["status"] == "cancelled"


@pytest.mark.asyncio
async def test_the_prover_side_backstop_fires_and_replies(prover):
    client, slow, quick = prover
    assert await _evaluate_through(client, quick, QUICK_END)

    result = await client.request("PIDE/find_theorems_at_position", {
        "token": "timeout-me",
        "textDocument": {"uri": "file://" + quick},
        "position": {"line": INDUCT - 1, "character": _caret(QUICK, INDUCT)},
        "timeout": 0.5, "query": "", "limit": "20000", "allow_dups": "false",
    })
    assert result["status"] == "timeout"


@pytest.mark.asyncio
async def test_unicode_notation_survives_the_round_trip(prover):
    client, slow, quick = prover
    assert await _evaluate_through(client, quick, QUICK_END)

    result = await goal(client, quick, MCPLine(UNICODE_LEMMA))
    assert result.subgoals == ["∀x y. x ≤ y ⟶ x ≤ y + 1"]
    assert not any("\\<" in g for g in result.subgoals)
