"""What the agent is told when a position-explicit query cannot produce a result.

The prover sends a status word; these are the sentences it becomes (§5.3). They
are checked character for character, because they are the whole of what the agent
sees and because a change to any of them needs a fresh sign-off.
"""

import os

import pytest

from isabelle_mcp import query
from isabelle_mcp.query import QueryReply
from isabelle_mcp.tools.find_theorems import find_theorems
from isabelle_mcp.tools.goal import goal
from isabelle_mcp.utils import IsabelleToolError, MCPLine

CMD = ("by simp", {"start": {"line": 8, "character": 2}, "end": {"line": 8, "character": 9}})


@pytest.fixture(autouse=True)
def _rooted(mock_lsp_client, temp_theory_file):
    """Positions render relative to the project root, as everywhere else."""
    mock_lsp_client.project_root = os.path.dirname(temp_theory_file)


async def _goal_raising(client, path, reply):
    client.proof_state_reply = reply
    client.command_at_position_response = CMD
    with pytest.raises(IsabelleToolError) as excinfo:
        await goal(client, path, MCPLine(9))
    return str(excinfo.value)


class TestProofStateReplies:
    @pytest.mark.asyncio
    async def test_no_stored_state(self, mock_lsp_client, temp_theory_file):
        # The common way here is a cancelled evaluation, which discards the
        # finished commands' states; retrying alone cannot help, so the sentence
        # sends the agent to evaluate again.
        assert await _goal_raising(
            mock_lsp_client, temp_theory_file, QueryReply(status=query.UNDEFINED),
        ) == (
            "The prover no longer holds a proof state for the command at Test.thy:9 — "
            "the evaluation was cancelled. Evaluate the file again to get one."
        )

    @pytest.mark.asyncio
    async def test_not_finished(self, mock_lsp_client, temp_theory_file):
        assert await _goal_raising(
            mock_lsp_client, temp_theory_file, QueryReply(status=query.UNFINISHED),
        ) == (
            "The command at Test.thy:9 has not finished evaluating, so it has no "
            "proof state yet. Retry in a few seconds."
        )

    @pytest.mark.asyncio
    async def test_interrupted(self, mock_lsp_client, temp_theory_file):
        assert await _goal_raising(
            mock_lsp_client, temp_theory_file, QueryReply(status=query.INTERRUPTED),
        ) == (
            "The evaluation of the command at Test.thy:9 was interrupted, so it has "
            "no proof state. Evaluate the file again to get one."
        )

    @pytest.mark.asyncio
    async def test_failed_carries_the_provers_own_words(
        self, mock_lsp_client, temp_theory_file,
    ):
        assert await _goal_raising(
            mock_lsp_client, temp_theory_file,
            QueryReply(status=query.FAILED, content="Undefined fact: bogus"),
        ) == "Reading the proof state at Test.thy:9 failed: Undefined fact: bogus"

    @pytest.mark.asyncio
    async def test_cancelled_crashed_and_timeout_do_not_depend_on_the_question(
        self, mock_lsp_client, temp_theory_file,
    ):
        assert await _goal_raising(
            mock_lsp_client, temp_theory_file, QueryReply(status=query.CANCELLED),
        ) == "The query was cancelled."
        assert await _goal_raising(
            mock_lsp_client, temp_theory_file, QueryReply(status=query.CRASHED),
        ) == "The prover could not answer this query and could not say why."
        assert await _goal_raising(
            mock_lsp_client, temp_theory_file, QueryReply(status=query.TIMEOUT),
        ) == "The prover did not answer this query within 600s."

    @pytest.mark.asyncio
    async def test_a_status_this_side_does_not_know_is_reported_as_a_crash(
        self, mock_lsp_client, temp_theory_file,
    ):
        # A prelude newer than this client would be caught by the version gate,
        # but if anything ever slips through, saying "could not say why" is
        # honest and inventing an explanation is not.
        assert await _goal_raising(
            mock_lsp_client, temp_theory_file, QueryReply(status="something_new"),
        ) == "The prover could not answer this query and could not say why."


class TestProofStateServedAnswers:
    @pytest.mark.asyncio
    async def test_not_a_proof_operation_is_an_answer_not_an_error(
        self, mock_lsp_client, temp_theory_file,
    ):
        # This replaces the old behaviour, where a non-proof command produced no
        # state output and the client waited out a ten-second grace to conclude
        # the same thing in silence.
        mock_lsp_client.proof_state_reply = QueryReply(status=query.NO_PROOF_STATE)
        mock_lsp_client.command_at_position_response = CMD
        result = await goal(mock_lsp_client, temp_theory_file, MCPLine(9))
        assert result.subgoals == []
        assert result.command is not None
        assert result.note == (
            "The command at Test.thy:9 is not a proof operation, so there is no "
            "proof state here."
        )

    @pytest.mark.asyncio
    async def test_forked_work_is_a_note_on_a_served_state(
        self, mock_lsp_client, temp_theory_file,
    ):
        mock_lsp_client.proof_state_reply = QueryReply(
            status=query.OK, forked=True, content='<pre><span class="subgoal">1. P</span></pre>',
        )
        mock_lsp_client.command_at_position_response = CMD
        result = await goal(mock_lsp_client, temp_theory_file, MCPLine(9))
        assert result.subgoals == ["P"]
        assert result.note == (
            "This command forked work that is still running, so a failure may still "
            "surface at Test.thy:9."
        )

    @pytest.mark.asyncio
    async def test_no_command_answers_with_no_command(
        self, mock_lsp_client, temp_theory_file,
    ):
        mock_lsp_client.proof_state_reply = QueryReply(status=query.NO_COMMAND)
        mock_lsp_client.command_at_position_response = None
        result = await goal(mock_lsp_client, temp_theory_file, MCPLine(9))
        assert result.command is None
        assert result.subgoals == []
        assert result.note is None


class TestFindTheoremsReplies:
    @pytest.mark.asyncio
    async def test_past_the_end_of_the_theory_there_is_nothing_to_search(
        self, mock_lsp_client, temp_theory_file,
    ):
        # "end" leaves the pristine toplevel, which has no context at all —
        # the one status isabelle_goal never sees.
        mock_lsp_client.find_theorems_reply = QueryReply(status=query.NO_CONTEXT)
        mock_lsp_client.command_at_position_response = CMD
        with pytest.raises(IsabelleToolError) as excinfo:
            await find_theorems(mock_lsp_client, temp_theory_file, MCPLine(9), names=["add"])
        assert str(excinfo.value) == (
            "There is no theory context at Test.thy:9, so there is nothing to "
            "search here. Ask at a line inside the theory."
        )

    @pytest.mark.asyncio
    async def test_arguments_reach_the_prover_with_allow_dups_uninverted(
        self, mock_lsp_client, temp_theory_file,
    ):
        # The prover reads allow_dups inverted; the tool must send the token it
        # is given, not a rendering of the boolean it means.
        mock_lsp_client.command_at_position_response = CMD
        await find_theorems(
            mock_lsp_client, temp_theory_file, MCPLine(9), names=["add_0"], limit=5,
        )
        assert mock_lsp_client.find_theorems_args == ('name: "add_0"', "5", "false")

    @pytest.mark.asyncio
    async def test_no_command_is_reported_as_nothing_searched(
        self, mock_lsp_client, temp_theory_file,
    ):
        mock_lsp_client.find_theorems_reply = QueryReply(status=query.NO_COMMAND)
        mock_lsp_client.command_at_position_response = None
        result = await find_theorems(mock_lsp_client, temp_theory_file, MCPLine(9))
        assert result.theorems == []
        assert result.note == "No command at the position; nothing was searched."
