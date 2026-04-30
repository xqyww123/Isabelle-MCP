"""Tests for utility modules."""

from pathlib import Path

import pytest

from isa_lsp.utils.core import (
    IsabelleToolError,
    check_pide_response,
    file_path_to_uri,
    lsp_to_mcp_position,
    mcp_to_lsp_position,
    uri_to_file_path,
)
from isa_lsp.utils.formatters import (
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
        assert mcp_to_lsp_position(1, 1) == (0, 0)
        assert mcp_to_lsp_position(10, 5) == (9, 4)

    def test_lsp_to_mcp(self):
        assert lsp_to_mcp_position(0, 0) == (1, 1)
        assert lsp_to_mcp_position(9, 4) == (10, 5)

    def test_roundtrip(self):
        for line, col in [(1, 1), (10, 5), (100, 50), (1000, 500)]:
            assert lsp_to_mcp_position(*mcp_to_lsp_position(line, col)) == (line, col)

    def test_large_numbers(self):
        assert mcp_to_lsp_position(10000, 5000) == (9999, 4999)
        assert lsp_to_mcp_position(9999, 4999) == (10000, 5000)


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

    def test_parse_command_output_empty(self):
        assert parse_command_output_html("<div></div>") == []


class TestGetLineFromFile:
    def test_basic(self, tmp_path):
        f = tmp_path / "test.thy"
        f.write_text("line 1\nline 2\nline 3\n")
        assert get_line_from_file(str(f), 1) == "line 1"
        assert get_line_from_file(str(f), 2) == "line 2"
        assert get_line_from_file(str(f), 3) == "line 3"

    def test_out_of_range(self, tmp_path):
        f = tmp_path / "test.thy"
        f.write_text("line 1\n")
        assert get_line_from_file(str(f), 10) == ""

    def test_nonexistent_file(self):
        assert get_line_from_file("/nonexistent/file.thy", 1) == ""

    def test_unicode(self, tmp_path):
        f = tmp_path / "unicode.thy"
        f.write_text('lemma test: "∀x. P x ⟹ Q x"\n', encoding='utf-8')
        line = get_line_from_file(str(f), 1)
        assert "∀" in line
        assert "⟹" in line

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.thy"
        f.write_text("")
        assert get_line_from_file(str(f), 1) == ""

    def test_single_line_no_newline(self, tmp_path):
        f = tmp_path / "test.thy"
        f.write_text("single line")
        assert get_line_from_file(str(f), 1) == "single line"

    def test_very_long_line(self, tmp_path):
        f = tmp_path / "long.thy"
        long_line = "x" * 10000
        f.write_text(f"{long_line}\n")
        assert get_line_from_file(str(f), 1) == long_line
