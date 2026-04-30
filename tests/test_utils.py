"""
Unit tests for utility modules.
"""

import pytest

from isa_lsp.utils.errors import IsabelleToolError, check_pide_response
from isa_lsp.utils.formatters import (
    extract_symbol_from_range,
    parse_goals_from_html,
    strip_html_tags,
)
from isa_lsp.utils.positions import lsp_to_mcp_position, mcp_to_lsp_position
from isa_lsp.utils.uri_utils import file_path_to_uri, uri_to_file_path


class TestErrors:
    """Test error utilities."""

    def test_isabelle_tool_error(self):
        """Test IsabelleToolError exception."""
        error = IsabelleToolError("Test error")
        assert str(error) == "Test error"
        assert isinstance(error, Exception)

    def test_check_pide_response_success(self):
        """Test check_pide_response with valid response."""
        response = {"result": "success"}
        assert check_pide_response(response, "test_operation") == response

    def test_check_pide_response_none_not_allowed(self):
        """Test check_pide_response with None when not allowed."""
        with pytest.raises(IsabelleToolError, match="PIDE timeout"):
            check_pide_response(None, "test_operation", allow_none=False)

    def test_check_pide_response_none_allowed(self):
        """Test check_pide_response with None when allowed."""
        assert check_pide_response(None, "test_operation", allow_none=True) is None

    def test_check_pide_response_error(self):
        """Test check_pide_response with error response."""
        response = {"error": {"message": "Test error message"}}
        with pytest.raises(IsabelleToolError, match="Test error message"):
            check_pide_response(response, "test_operation")


class TestURIUtils:
    """Test URI utilities."""

    def test_file_path_to_uri_unix(self):
        """Test file path to URI conversion on Unix."""
        path = "/home/user/test.thy"
        uri = file_path_to_uri(path)
        assert uri.startswith("file://")
        assert "/home/user/test.thy" in uri

    def test_uri_to_file_path_unix(self):
        """Test URI to file path conversion on Unix."""
        uri = "file:///home/user/test.thy"
        path = uri_to_file_path(uri)
        assert path == "/home/user/test.thy"

    def test_uri_to_file_path_invalid(self):
        """Test URI to file path with invalid URI."""
        with pytest.raises(ValueError, match="Invalid file URI"):
            uri_to_file_path("http://example.com/test.thy")

    def test_roundtrip_conversion(self):
        """Test roundtrip file path <-> URI conversion."""
        original_path = "/home/user/Theory.thy"
        uri = file_path_to_uri(original_path)
        converted_path = uri_to_file_path(uri)
        # Normalize to absolute path for comparison
        from pathlib import Path
        assert Path(converted_path).resolve() == Path(original_path).resolve()


class TestPositions:
    """Test position conversion utilities."""

    def test_mcp_to_lsp_position(self):
        """Test MCP to LSP position conversion."""
        # MCP: 1-indexed, LSP: 0-indexed
        lsp_line, lsp_col = mcp_to_lsp_position(1, 1)
        assert lsp_line == 0
        assert lsp_col == 0

        lsp_line, lsp_col = mcp_to_lsp_position(10, 5)
        assert lsp_line == 9
        assert lsp_col == 4

    def test_lsp_to_mcp_position(self):
        """Test LSP to MCP position conversion."""
        # LSP: 0-indexed, MCP: 1-indexed
        mcp_line, mcp_col = lsp_to_mcp_position(0, 0)
        assert mcp_line == 1
        assert mcp_col == 1

        mcp_line, mcp_col = lsp_to_mcp_position(9, 4)
        assert mcp_line == 10
        assert mcp_col == 5

    def test_roundtrip_position_conversion(self):
        """Test roundtrip position conversion."""
        original_line, original_col = 42, 17
        lsp_line, lsp_col = mcp_to_lsp_position(original_line, original_col)
        mcp_line, mcp_col = lsp_to_mcp_position(lsp_line, lsp_col)
        assert mcp_line == original_line
        assert mcp_col == original_col


class TestFormatters:
    """Test formatter utilities."""

    def test_strip_html_tags(self):
        """Test HTML tag stripping."""
        html = "<p>Hello <b>world</b></p>"
        text = strip_html_tags(html)
        assert text == "Hello world"

        html = "<div><span>Test</span> content</div>"
        text = strip_html_tags(html)
        assert text == "Test content"

    def test_strip_html_tags_with_entities(self):
        """Test HTML tag stripping with entities."""
        html = "&lt;foo&gt; &amp; &quot;bar&quot;"
        text = strip_html_tags(html)
        assert text == "<foo> & \"bar\""

    def test_parse_goals_from_html_no_goals(self):
        """Test parsing goals from HTML with no goals."""
        html = "<p>No goals</p>"
        goals = parse_goals_from_html(html)
        assert goals == []

    def test_parse_goals_from_html_with_goals(self):
        """Test parsing goals from HTML with goals."""
        html = """
        <div>
        1. P ⟹ Q
        2. Q ⟹ R
        </div>
        """
        goals = parse_goals_from_html(html)
        assert len(goals) == 2
        assert "P ⟹ Q" in goals[0]
        assert "Q ⟹ R" in goals[1]

    def test_extract_symbol_from_range(self):
        """Test symbol extraction from text range."""
        text = "lemma test_lemma: \"P ⟹ Q\""
        # Extract "test_lemma"
        symbol = extract_symbol_from_range(text, 6, 16)
        assert "test_lemma" in symbol


class TestGetLineFromFile:
    """Test get_line_from_file utility."""

    def test_get_line_from_file(self, tmp_path):
        """Test reading a specific line from file."""
        from isa_lsp.utils import get_line_from_file

        # Create test file
        test_file = tmp_path / "test.thy"
        test_file.write_text("line 1\nline 2\nline 3\n")

        # Test reading lines
        assert get_line_from_file(str(test_file), 1) == "line 1"
        assert get_line_from_file(str(test_file), 2) == "line 2"
        assert get_line_from_file(str(test_file), 3) == "line 3"

    def test_get_line_from_file_out_of_range(self, tmp_path):
        """Test reading line out of range."""
        from isa_lsp.utils import get_line_from_file

        test_file = tmp_path / "test.thy"
        test_file.write_text("line 1\n")

        # Should return empty string for out of range
        assert get_line_from_file(str(test_file), 10) == ""

    def test_get_line_from_file_invalid_path(self):
        """Test reading from non-existent file."""
        from isa_lsp.utils import get_line_from_file

        # get_line_from_file returns empty string for non-existent files
        result = get_line_from_file("/nonexistent/file.thy", 1)
        assert result == ""
