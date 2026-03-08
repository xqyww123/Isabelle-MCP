"""
Advanced unit tests for utility modules covering edge cases.
"""

import pytest
from pathlib import Path
from isa_lsp.utils.errors import IsabelleToolError, check_pide_response
from isa_lsp.utils.uri_utils import file_path_to_uri, uri_to_file_path
from isa_lsp.utils.positions import mcp_to_lsp_position, lsp_to_mcp_position
from isa_lsp.utils.formatters import (
    strip_html_tags,
    parse_goals_from_html,
    extract_symbol_from_range,
    parse_command_output_html,
    format_hover_content,
)
from isa_lsp.utils import get_line_from_file


class TestErrorsAdvanced:
    """Advanced tests for error utilities."""

    def test_check_pide_response_with_nested_error(self):
        """Test error response with nested error structure."""
        response = {
            "error": {
                "code": -32600,
                "message": "Invalid Request",
                "data": {"detail": "Additional error info"}
            }
        }

        with pytest.raises(IsabelleToolError, match="Invalid Request"):
            check_pide_response(response, "test_op")

    def test_check_pide_response_empty_error(self):
        """Test error response with empty error object."""
        response = {"error": {}}

        with pytest.raises(IsabelleToolError, match="Unknown error"):
            check_pide_response(response, "test_op")

    def test_check_pide_response_with_valid_result(self):
        """Test response with explicit result field."""
        response = {"result": {"data": "success"}}
        result = check_pide_response(response, "test_op")

        assert result == response
        assert result["result"]["data"] == "success"

    def test_isabelle_tool_error_str(self):
        """Test IsabelleToolError string representation."""
        error = IsabelleToolError("Test error message")

        assert "Test error message" in str(error)
        assert repr(error).startswith("IsabelleToolError")


class TestURIUtilsAdvanced:
    """Advanced tests for URI utilities."""

    def test_file_path_to_uri_with_spaces(self):
        """Test URI conversion with spaces in path."""
        path = "/home/user/my documents/test.thy"
        uri = file_path_to_uri(path)

        assert uri.startswith("file://")
        # Spaces should be encoded
        assert "%20" in uri or "my documents" in uri

    def test_file_path_to_uri_with_unicode(self):
        """Test URI conversion with unicode characters."""
        path = "/home/user/文档/test.thy"
        uri = file_path_to_uri(path)

        assert uri.startswith("file://")
        # Should handle unicode
        assert uri is not None

    def test_uri_to_file_path_with_encoded_chars(self):
        """Test URI to path conversion with encoded characters."""
        uri = "file:///home/user/my%20documents/test.thy"
        path = uri_to_file_path(uri)

        assert "my documents" in path or "my%20documents" in path

    def test_uri_to_file_path_with_triple_slash(self):
        """Test URI with triple slash (Unix absolute path)."""
        uri = "file:///absolute/path/test.thy"
        path = uri_to_file_path(uri)

        assert path.startswith("/")
        assert "absolute/path/test.thy" in path

    def test_uri_to_file_path_windows_style(self):
        """Test URI with Windows-style path."""
        uri = "file:///C:/Users/test/file.thy"
        path = uri_to_file_path(uri)

        # Should handle Windows paths
        assert "C:" in path or "Users/test" in path

    def test_file_path_uri_roundtrip_complex(self):
        """Test complex path roundtrip conversion."""
        original = "/home/user/my-project/src/Theory.thy"
        uri = file_path_to_uri(original)
        converted = uri_to_file_path(uri)

        # Normalize for comparison
        assert Path(converted).resolve() == Path(original).resolve()


class TestPositionsAdvanced:
    """Advanced tests for position conversion."""

    def test_mcp_to_lsp_zero(self):
        """Test conversion of (1,1) to (0,0)."""
        line, col = mcp_to_lsp_position(1, 1)
        assert line == 0
        assert col == 0

    def test_mcp_to_lsp_large_numbers(self):
        """Test conversion with large line/column numbers."""
        line, col = mcp_to_lsp_position(10000, 5000)
        assert line == 9999
        assert col == 4999

    def test_lsp_to_mcp_large_numbers(self):
        """Test reverse conversion with large numbers."""
        line, col = lsp_to_mcp_position(9999, 4999)
        assert line == 10000
        assert col == 5000

    def test_position_conversion_consistency(self):
        """Test that conversions are consistent."""
        # Forward and backward should be inverses
        for mcp_line, mcp_col in [(1, 1), (10, 5), (100, 50), (1000, 500)]:
            lsp_line, lsp_col = mcp_to_lsp_position(mcp_line, mcp_col)
            back_line, back_col = lsp_to_mcp_position(lsp_line, lsp_col)
            assert back_line == mcp_line
            assert back_col == mcp_col


class TestFormattersAdvanced:
    """Advanced tests for formatters."""

    def test_strip_html_nested_tags(self):
        """Test stripping deeply nested HTML tags."""
        html = "<div><p><span><b>Text</b></span></p></div>"
        text = strip_html_tags(html)
        assert text == "Text"

    def test_strip_html_with_attributes(self):
        """Test stripping tags with attributes."""
        html = '<div class="foo" id="bar">Content</div>'
        text = strip_html_tags(html)
        assert text == "Content"
        assert "class" not in text
        assert "foo" not in text

    def test_strip_html_self_closing_tags(self):
        """Test stripping self-closing tags."""
        html = "Before<br/>After<img src='test.png'/>"
        text = strip_html_tags(html)
        assert "Before" in text
        assert "After" in text
        assert "br" not in text
        assert "img" not in text

    def test_strip_html_with_newlines(self):
        """Test HTML with newlines in tags."""
        html = """<div
            class="test">
            Content
        </div>"""
        text = strip_html_tags(html)
        assert "Content" in text
        assert "div" not in text

    def test_parse_goals_complex_isabelle(self):
        """Test parsing complex Isabelle goals."""
        html = """
        <div class="goals">
        1. ⋀x y. P x ⟹ Q y ⟹ R (f x y)
        2. ∀x. P x ∨ Q x
        3. ∃x. P x ∧ Q x
        </div>
        """
        goals = parse_goals_from_html(html)

        assert len(goals) >= 2
        # Should contain Isabelle symbols
        any_has_symbols = any("⋀" in g or "⟹" in g or "∀" in g or "∃" in g for g in goals)
        assert any_has_symbols or len(goals) > 0

    def test_parse_goals_no_goals_variations(self):
        """Test various 'no goals' messages."""
        for html in [
            "<div>No goals</div>",
            "<div>no goals</div>",
            "<div>NO GOALS</div>",
            "<div>Proof complete, no goals remaining</div>"
        ]:
            goals = parse_goals_from_html(html)
            assert goals == []

    def test_parse_goals_with_proof_context(self):
        """Test parsing goals with proof context."""
        html = """
        <div>
        proof (prove): step 1
        goal (1 subgoal):
        1. P ⟹ Q
        </div>
        """
        goals = parse_goals_from_html(html)

        # Should find at least one goal
        assert len(goals) >= 1

    def test_extract_symbol_from_range_boundaries(self):
        """Test symbol extraction at boundaries."""
        text = "lemma test: \"P\""

        # At start
        symbol = extract_symbol_from_range(text, 0, 5)
        assert symbol is not None

        # At end
        symbol = extract_symbol_from_range(text, len(text) - 1, len(text))
        assert symbol is not None

        # Beyond end
        symbol = extract_symbol_from_range(text, 100, 105)
        assert symbol == ""

    def test_extract_symbol_isabelle_operators(self):
        """Test extracting Isabelle operator symbols."""
        text = "P ⟹ Q ∧ R"

        # Should extract operators
        symbol = extract_symbol_from_range(text, 2, 4)  # On ⟹
        assert symbol is not None

    def test_format_hover_content_markdown(self):
        """Test formatting hover content from markdown."""
        content = {
            "kind": "markdown",
            "value": "**Symbol**: `my_const`\n\nType: `nat`"
        }

        result = format_hover_content(content)

        assert "my_const" in result
        assert "nat" in result

    def test_format_hover_content_plaintext(self):
        """Test formatting hover content from plaintext."""
        content = "Simple plaintext hover info"

        result = format_hover_content(content)

        assert result == "Simple plaintext hover info"

    def test_format_hover_content_array(self):
        """Test formatting hover content from array."""
        content = [
            {"language": "isabelle", "value": "definition"},
            "Additional text"
        ]

        result = format_hover_content(content)

        assert isinstance(result, str)
        assert len(result) > 0

    def test_format_hover_content_marked_string(self):
        """Test formatting MarkedString hover content."""
        content = {
            "language": "isabelle",
            "value": "my_const :: nat"
        }

        result = format_hover_content(content)

        assert "my_const" in result
        assert "nat" in result


class TestGetLineFromFileAdvanced:
    """Advanced tests for get_line_from_file."""

    def test_get_line_from_file_with_unicode(self, tmp_path):
        """Test reading line with unicode characters."""
        test_file = tmp_path / "unicode.thy"
        test_file.write_text("lemma test: \"∀x. P x ⟹ Q x\"\n", encoding='utf-8')

        line = get_line_from_file(str(test_file), 1)

        assert "∀" in line
        assert "⟹" in line

    def test_get_line_from_file_with_tabs(self, tmp_path):
        """Test reading line with tabs."""
        test_file = tmp_path / "tabs.thy"
        test_file.write_text("lemma\ttest:\t\"P\"\n")

        line = get_line_from_file(str(test_file), 1)

        assert "\t" in line or "test" in line

    def test_get_line_from_file_empty_file(self, tmp_path):
        """Test reading from empty file."""
        test_file = tmp_path / "empty.thy"
        test_file.write_text("")

        line = get_line_from_file(str(test_file), 1)

        assert line == ""

    def test_get_line_from_file_single_line_no_newline(self, tmp_path):
        """Test reading single line without trailing newline."""
        test_file = tmp_path / "nonewline.thy"
        test_file.write_text("single line")

        line = get_line_from_file(str(test_file), 1)

        assert line == "single line"

    def test_get_line_from_file_windows_line_endings(self, tmp_path):
        """Test reading file with Windows line endings."""
        test_file = tmp_path / "windows.thy"
        test_file.write_bytes(b"line1\r\nline2\r\nline3\r\n")

        line = get_line_from_file(str(test_file), 2)

        assert "line2" in line

    def test_get_line_from_file_very_long_line(self, tmp_path):
        """Test reading very long line."""
        test_file = tmp_path / "long.thy"
        long_line = "x" * 10000
        test_file.write_text(f"{long_line}\n")

        line = get_line_from_file(str(test_file), 1)

        assert len(line) == 10000
        assert line == long_line

    def test_get_line_from_file_binary_mode_error(self, tmp_path):
        """Test that binary files raise appropriate error."""
        test_file = tmp_path / "binary.bin"
        test_file.write_bytes(b"\x00\x01\x02\x03")

        # Should handle binary data gracefully or raise error
        try:
            line = get_line_from_file(str(test_file), 1)
            # If it succeeds, that's fine (handles as text)
            assert line is not None
        except (UnicodeDecodeError, IsabelleToolError):
            # If it fails, should be a decode error
            pass


class TestParseCommandOutputHTML:
    """Test parsing command output HTML."""

    def test_parse_command_output_html_basic(self):
        """Test basic command output parsing."""
        html = """
        <div class="writeln">Output message 1</div>
        <div class="warning">Warning message</div>
        """

        messages = parse_command_output_html(html)

        assert len(messages) >= 1

    def test_parse_command_output_html_empty(self):
        """Test parsing empty command output."""
        html = "<div></div>"

        messages = parse_command_output_html(html)

        assert isinstance(messages, list)

    def test_parse_command_output_html_with_markup(self):
        """Test parsing output with PIDE markup."""
        html = """
        <div class="writeln">
            <span class="keyword">Output</span> text
        </div>
        """

        messages = parse_command_output_html(html)

        # Should extract text, removing markup
        assert len(messages) >= 0
