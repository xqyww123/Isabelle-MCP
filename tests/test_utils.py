"""Tests for utility modules."""

from pathlib import Path

import pytest

from isabelle_mcp.utils.core import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPColumn,
    MCPLine,
    check_pide_response,
    file_path_to_uri,
    lsp_to_mcp_position,
    mcp_to_lsp_position,
    uri_to_file_path,
)
from isabelle_mcp.utils.formatters import (
    get_line_from_file,
    parse_command_output_html,
    parse_goals_from_html,
    strip_html_tags,
)


class TestErrors:
    def test_isabelle_tool_error(self):
        error = IsabelleToolError("Test error")
        assert str(error) == "Test error"

    def test_check_pide_response_success(self):
        response = {"result": "success"}
        assert check_pide_response(response, "test_operation") == response

    def test_check_pide_response_none_not_allowed(self):
        with pytest.raises(IsabelleToolError, match="PIDE timeout"):
            check_pide_response(None, "test_operation", allow_none=False)

    def test_check_pide_response_none_allowed(self):
        assert check_pide_response(None, "test_operation", allow_none=True) is None

    def test_check_pide_response_error(self):
        response = {"error": {"message": "Test error message"}}
        with pytest.raises(IsabelleToolError, match="Test error message"):
            check_pide_response(response, "test_operation")

    def test_check_pide_response_empty_error(self):
        with pytest.raises(IsabelleToolError, match="Unknown error"):
            check_pide_response({"error": {}}, "test_op")

    def test_check_pide_response_with_code(self):
        response = {"error": {"code": -32600, "message": "Invalid Request"}}
        with pytest.raises(IsabelleToolError, match="Invalid Request"):
            check_pide_response(response, "test_op")


class TestURIUtils:
    def test_file_path_to_uri_unix(self):
        uri = file_path_to_uri("/home/user/test.thy")
        assert uri.startswith("file://")
        assert "test.thy" in uri

    def test_uri_to_file_path_unix(self):
        assert uri_to_file_path("file:///home/user/test.thy") == "/home/user/test.thy"

    def test_uri_to_file_path_invalid(self):
        with pytest.raises(ValueError, match="Invalid file URI"):
            uri_to_file_path("http://example.com/test.thy")

    def test_roundtrip_conversion(self):
        original = "/home/user/Theory.thy"
        converted = uri_to_file_path(file_path_to_uri(original))
        assert Path(converted).resolve() == Path(original).resolve()

    def test_uri_with_spaces(self):
        uri = file_path_to_uri("/home/user/my documents/test.thy")
        assert uri.startswith("file://")

    def test_uri_encoded_chars(self):
        path = uri_to_file_path("file:///home/user/my%20documents/test.thy")
        assert "my documents" in path

    def test_complex_roundtrip(self):
        original = "/home/user/my-project/src/Theory.thy"
        assert Path(uri_to_file_path(file_path_to_uri(original))).resolve() == Path(original).resolve()


class TestPositions:
    def test_mcp_to_lsp(self):
        assert mcp_to_lsp_position(MCPLine(1), MCPColumn(1)) == (0, 0)
        assert mcp_to_lsp_position(MCPLine(10), MCPColumn(5)) == (9, 4)

    def test_lsp_to_mcp(self):
        assert lsp_to_mcp_position(LSPLine(0), LSPCharacter(0)) == (1, 1)
        assert lsp_to_mcp_position(LSPLine(9), LSPCharacter(4)) == (10, 5)

    def test_roundtrip(self):
        for line, col in [(1, 1), (10, 5), (100, 50), (1000, 500)]:
            assert lsp_to_mcp_position(
                *mcp_to_lsp_position(MCPLine(line), MCPColumn(col))
            ) == (line, col)

    def test_large_numbers(self):
        assert mcp_to_lsp_position(MCPLine(10000), MCPColumn(5000)) == (9999, 4999)
        assert lsp_to_mcp_position(LSPLine(9999), LSPCharacter(4999)) == (10000, 5000)

    def test_type_safety_methods(self):
        mcp_line = MCPLine(10)
        lsp_line = mcp_line.to_lsp()
        assert lsp_line == 9
        assert isinstance(lsp_line, LSPLine)
        assert lsp_line.to_mcp() == 10
        assert isinstance(lsp_line.to_mcp(), MCPLine)


class TestFormatters:
    def test_strip_html_tags(self):
        assert strip_html_tags("<p>Hello <b>world</b></p>") == "Hello world"
        assert strip_html_tags("<div><span>Test</span> content</div>") == "Test content"

    def test_strip_html_entities(self):
        assert strip_html_tags("&lt;foo&gt; &amp; &quot;bar&quot;") == '<foo> & "bar"'

    def test_strip_html_nested(self):
        assert strip_html_tags("<div><p><span><b>Text</b></span></p></div>") == "Text"

    def test_strip_html_with_attributes(self):
        text = strip_html_tags('<div class="foo" id="bar">Content</div>')
        assert text == "Content"
        assert "class" not in text

    def test_strip_html_self_closing(self):
        text = strip_html_tags("Before<br/>After<img src='test.png'/>")
        assert "Before" in text
        assert "After" in text

    def test_parse_goals_no_goals(self):
        for html in ["<p>No goals</p>", "<div>no goals</div>", "<div>NO GOALS</div>"]:
            assert parse_goals_from_html(html) == []

    def test_parse_goals_numbered(self):
        html = "<div>1. P ⟹ Q\n2. Q ⟹ R</div>"
        goals = parse_goals_from_html(html)
        assert len(goals) == 2
        assert "P ⟹ Q" in goals[0]
        assert "Q ⟹ R" in goals[1]

    def test_parse_goals_multiline_goal(self):
        html = "<pre>goal (1 subgoal):\n 1. first line\n    second line</pre>"
        assert parse_goals_from_html(html) == ["first line\nsecond line"]

    def test_parse_goals_universal(self):
        html = "<div>1. ⋀x y. P x ⟹ Q y ⟹ R (f x y)</div>"
        goals = parse_goals_from_html(html)
        assert len(goals) >= 1

    def test_parse_command_output(self):
        html = '<div class="writeln">Output</div><div class="warning">Warn</div>'
        msgs = parse_command_output_html(html)
        assert len(msgs) == 2
        assert msgs[0] == {'kind': 'writeln', 'text': 'Output'}
        assert msgs[1] == {'kind': 'warning', 'text': 'Warn'}

    def test_parse_command_output_multiple_css_classes(self):
        html = '<div class="message warning">Warn</div><div class="foo error bar">Err</div>'
        msgs = parse_command_output_html(html)
        assert msgs == [
            {'kind': 'warning', 'text': 'Warn'},
            {'kind': 'error', 'text': 'Err'},
        ]

    def test_parse_command_output_isabelle2024_writeln_message(self):
        html = (
            '<pre class="source"><span class="writeln_message">'
            '<span class="block">val<span class="break"> </span>'
            '<span class="block">it</span><span class="break"> </span>=</span>'
            '<span class="break"> </span>64:<span class="break"> </span>'
            '<span class="block">int</span></span></pre>'
        )
        assert parse_command_output_html(html) == [
            {'kind': 'writeln', 'text': 'val it = 64: int'}
        ]

    def test_parse_command_output_isabelle2024_error_message(self):
        html = (
            '<pre class="source"><span class="error_message">'
            'Undefined fact: &quot;fib.simps&quot;<span class="position">⌂</span>'
            '</span></pre>'
        )
        assert parse_command_output_html(html) == [
            {'kind': 'error', 'text': 'Undefined fact: "fib.simps"'}
        ]

    def test_parse_command_output_isabelle2024_state_message(self):
        html = (
            '<pre class="source"><span class="state_message">'
            '<span class="block">proof</span> (prove)\n'
            '<span class="block">goal</span> (1 subgoal):\n'
            '<span class="subgoal"> 1. P ⟹ Q</span>'
            '</span></pre>'
        )
        assert parse_command_output_html(html) == [
            {'kind': 'information', 'text': 'proof (prove) goal (1 subgoal): 1. P ⟹ Q'}
        ]

    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            (
                '<pre class="source"><span class="error_message">'
                'Undefined fact: &quot;fib.simps&quot;<span class="position">⌂</span>'
                '</span></pre>',
                [{'kind': 'error', 'text': 'Undefined fact: "fib.simps"'}],
            ),
            (
                '<pre class="source"><span class="writeln_message"><span class="block">'
                '<span class="block"><span class="block"><a '
                'href="file:/home/qiyuan/Current/MLML/contrib/Isabelle2024/src/HOL/HOL.thy#100">'
                '<span class="block">True</span></a></span></span></span></span></pre>',
                [{'kind': 'writeln', 'text': 'True'}],
            ),
            (
                '<pre class="source"><span class="error_message">Bad context for command '
                '&quot;<span class="keyword1">apply</span>&quot;'
                '<span class="position">⌂</span> -- using reset state</span></pre>',
                [{'kind': 'error', 'text': 'Bad context for command "apply" -- using reset state'}],
            ),
            (
                '<pre class="source"><span class="writeln_message"><span class="block">'
                '<span class="block">val<span class="break"> </span><span class="block">it</span>'
                '<span class="break"> </span>=</span><span class="break"> </span>'
                '&quot;&quot;:<span class="break"> </span><span class="block">string</span>'
                '</span></span></pre>',
                [{'kind': 'writeln', 'text': 'val it = "": string'}],
            ),
            (
                '<pre class="source"><span class="writeln_message"><span class="block">'
                '<span class="block">val<span class="break"> </span><span class="block">it</span>'
                '<span class="break"> </span>=</span><span class="break"> </span>'
                '64:<span class="break"> </span><span class="block">int</span>'
                '</span></span></pre>',
                [{'kind': 'writeln', 'text': 'val it = 64: int'}],
            ),
            ('<pre class="source"/>', []),
        ],
    )
    def test_parse_command_output_real_isabelle2024_scratch_samples(self, html, expected):
        assert parse_command_output_html(html) == expected

    def test_parse_command_output_empty(self):
        assert parse_command_output_html("<div></div>") == []


class TestGetLineFromFile:
    def test_basic(self, tmp_path):
        f = tmp_path / "test.thy"
        f.write_text("line 1\nline 2\nline 3\n")
        assert get_line_from_file(str(f), MCPLine(1)) == "line 1"
        assert get_line_from_file(str(f), MCPLine(2)) == "line 2"
        assert get_line_from_file(str(f), MCPLine(3)) == "line 3"

    def test_out_of_range(self, tmp_path):
        f = tmp_path / "test.thy"
        f.write_text("line 1\n")
        assert get_line_from_file(str(f), MCPLine(10)) == ""

    def test_nonexistent_file(self):
        assert get_line_from_file("/nonexistent/file.thy", MCPLine(1)) == ""

    def test_unicode(self, tmp_path):
        f = tmp_path / "unicode.thy"
        f.write_text('lemma test: "∀x. P x ⟹ Q x"\n', encoding='utf-8')
        line = get_line_from_file(str(f), MCPLine(1))
        assert "∀" in line
        assert "⟹" in line

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.thy"
        f.write_text("")
        assert get_line_from_file(str(f), MCPLine(1)) == ""

    def test_single_line_no_newline(self, tmp_path):
        f = tmp_path / "test.thy"
        f.write_text("single line")
        assert get_line_from_file(str(f), MCPLine(1)) == "single line"

    def test_very_long_line(self, tmp_path):
        f = tmp_path / "long.thy"
        long_line = "x" * 10000
        f.write_text(f"{long_line}\n")
        assert get_line_from_file(str(f), MCPLine(1)) == long_line
