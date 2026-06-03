import pytest

from isabelle_mcp.models import CommandOutputResult, CommandSpan, OutputMessage
from isabelle_mcp.tools.command_output import command_output, format_command_output
from isabelle_mcp.utils import IsabelleToolError, MCPLine

# (source, LSP 0-indexed range, output HTML) for a "by simp" command on the 9th line.
RANGE = {"start": {"line": 8, "character": 2}, "end": {"line": 8, "character": 9}}
OUT = ("by simp", RANGE, '<div class="writeln">Success</div>')


class TestCommandOutputTool:
    @pytest.mark.asyncio
    async def test_command_and_messages(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.output_at_position_response = OUT
        result = await command_output(mock_lsp_client, temp_theory_file, MCPLine(9))
        assert result.command is not None
        assert result.command.text == "by simp"
        assert result.command.start_line == 9       # lsp 8 -> 1-indexed 9
        assert result.command.start_column == 3      # lsp char 2 -> 1-indexed 3
        assert result.command.end_column == 10       # lsp char 9 -> 1-indexed 10
        assert len(result.messages) == 1
        assert result.messages[0].kind == "normal"   # writeln CSS -> normal
        assert result.messages[0].message == "Success"

    @pytest.mark.asyncio
    async def test_with_after_text(self, mock_lsp_client, temp_theory_file):
        # Line 9 is "  by (simp add: my_const_def)"
        mock_lsp_client.output_at_position_response = (
            "by simp", RANGE, '<div class="tracing">trace</div>',
        )
        result = await command_output(
            mock_lsp_client, temp_theory_file, MCPLine(9), after_text="by",
        )
        assert result.command is not None
        assert result.command.text == "by simp"
        assert result.messages[0].kind == "tracing"  # tracing CSS -> tracing

    @pytest.mark.asyncio
    async def test_no_command(self, mock_lsp_client, temp_theory_file):
        # output_at_position returns None (blank line, comment, or past last command).
        mock_lsp_client.output_at_position_response = None
        result = await command_output(mock_lsp_client, temp_theory_file, MCPLine(9))
        assert result.command is None
        assert result.messages == []

    @pytest.mark.asyncio
    async def test_after_text_not_found(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="not found on line"):
            await command_output(
                mock_lsp_client, temp_theory_file, MCPLine(9), after_text="no_such_text",
            )

    @pytest.mark.asyncio
    async def test_invalid_line(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="line must be >= 1"):
            await command_output(mock_lsp_client, temp_theory_file, MCPLine(0))


def _span(text, sl, el):
    return CommandSpan(text=text, start_line=sl, start_column=1, end_line=el, end_column=2)


class TestFormatCommandOutput:
    def test_single_line_with_messages(self):
        r = CommandOutputResult(
            command=_span('lemma foo: "1 + (1::nat) = 2"', 5, 5),
            messages=[
                OutputMessage(kind="state", message="proof (prove) goal (1 subgoal): 1. 1 + 1 = 2"),
                OutputMessage(kind="information", message="Auto solve_direct: ..."),
            ],
        )
        assert format_command_output(r, 5) == (
            '[line 5]\n'
            'lemma foo: "1 + (1::nat) = 2"\n'
            '\n'
            '[state] proof (prove) goal (1 subgoal): 1. 1 + 1 = 2\n'
            '[information] Auto solve_direct: ...'
        )

    def test_multiline_command(self):
        r = CommandOutputResult(
            command=_span('lemma bar:\n  "2 + 2 = (4::nat)"', 13, 14),
            messages=[OutputMessage(kind="state", message="proof (prove) goal (1 subgoal): 1. 2 + 2 = 4")],
        )
        assert format_command_output(r, 13) == (
            '[line 13-14]\n'
            'lemma bar:\n'
            '  "2 + 2 = (4::nat)"\n'
            '\n'
            '[state] proof (prove) goal (1 subgoal): 1. 2 + 2 = 4'
        )

    def test_no_output(self):
        r = CommandOutputResult(command=_span('definition bar where "bar = 0"', 8, 8), messages=[])
        assert format_command_output(r, 8) == (
            '[line 8]\n'
            'definition bar where "bar = 0"\n'
            '\n'
            '(no output)'
        )

    def test_no_command(self):
        r = CommandOutputResult(command=None, messages=[])
        assert format_command_output(r, 7) == "No command at line 7."

    def test_note_prefixed(self):
        r = CommandOutputResult(
            command=_span("apply auto", 10, 10),
            messages=[OutputMessage(kind="state", message="...")],
            note="This line is still being executed (forked proof). Output may be incomplete.",
        )
        assert format_command_output(r, 10) == (
            '[note] This line is still being executed (forked proof). Output may be incomplete.\n'
            '\n'
            '[line 10]\n'
            'apply auto\n'
            '\n'
            '[state] ...'
        )
