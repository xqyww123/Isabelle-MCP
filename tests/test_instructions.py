"""The server instructions fit Claude Code's budget, and the guidance moved out
of them lives in the tool descriptions (in the body, not swallowed into an
``Args:`` schema field)."""

import pytest

from isabelle_mcp.instructions import get_instructions
from isabelle_mcp.server import mcp

CLAUDE_CODE_INSTRUCTIONS_LIMIT = 2048


def test_instructions_fit_claude_code_budget():
    assert len(get_instructions()) <= CLAUDE_CODE_INSTRUCTIONS_LIMIT


@pytest.mark.parametrize("tool, sentence", [
    ("isabelle_evaluate_to", "Errors do not stop the checking"),
    ("isabelle_enable_all_breakpoints", "Nothing\nre-arms in the background."),
    ("isabelle_debug_state", "frame 0 is the innermost"),
    ("isabelle_set_breakpoint", "The usual workflow:"),
])
async def test_moved_guidance_is_in_the_tool_description(tool, sentence):
    assert sentence in ((await mcp.get_tool(tool)).description or "")


async def test_root_entry_hint_is_in_the_launch_session_parameter():
    schema = (await mcp.get_tool("isabelle_launch")).parameters
    assert "`session NAME = BASE + …`" in schema["properties"]["session"]["description"]
