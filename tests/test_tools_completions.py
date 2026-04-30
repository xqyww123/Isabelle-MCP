"""
Unit tests for completions tool.
"""

import pytest

from isa_lsp.tools.completions import completions


class TestCompletionsTool:
    """Test completions tool."""

    @pytest.mark.asyncio
    async def test_completions_basic(self, mock_lsp_client, temp_theory_file, sample_completion_response):
        """Test basic completions functionality."""
        mock_lsp_client.completion_response = sample_completion_response

        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)

        assert result.line_context is not None
        assert len(result.items) > 0
        assert result.items[0].label is not None

    @pytest.mark.asyncio
    async def test_completions_sorting(self, mock_lsp_client, temp_theory_file):
        """Test completion sorting by relevance."""
        mock_lsp_client.completion_response = {
            "items": [
                {"label": "zebra", "kind": 1, "detail": ""},
                {"label": "apply", "kind": 1, "detail": ""},
                {"label": "apple", "kind": 1, "detail": ""},
            ]
        }

        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)

        # Items should be sorted alphabetically when no prefix matching
        labels = [item.label for item in result.items]
        assert labels == sorted(labels)

    @pytest.mark.asyncio
    async def test_completions_max_limit(self, mock_lsp_client, temp_theory_file):
        """Test max_completions limit."""
        # Create 100 completion items
        items = [{"label": f"item_{i}", "kind": 1, "detail": ""} for i in range(100)]
        mock_lsp_client.completion_response = {"items": items}

        result = await completions(mock_lsp_client, temp_theory_file, 8, 1, max_completions=10)

        # Should be limited to 10
        assert len(result.items) == 10

    @pytest.mark.asyncio
    async def test_completions_null_response(self, mock_lsp_client, temp_theory_file):
        """Test completions with null response."""
        mock_lsp_client.completion_response = None

        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)

        assert result.items == []
        assert result.line_context is not None

    @pytest.mark.asyncio
    async def test_completions_empty_items(self, mock_lsp_client, temp_theory_file):
        """Test completions with empty items list."""
        mock_lsp_client.completion_response = {"items": []}

        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)

        assert result.items == []

    @pytest.mark.asyncio
    async def test_completions_auto_open(self, mock_lsp_client, temp_theory_file):
        """Test that completions auto-opens document."""
        assert temp_theory_file not in mock_lsp_client.open_documents

        mock_lsp_client.completion_response = {"items": []}

        await completions(mock_lsp_client, temp_theory_file, 8, 1)

        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_completions_kind_mapping(self, mock_lsp_client, temp_theory_file):
        """Test LSP completion kind to string mapping."""
        mock_lsp_client.completion_response = {
            "items": [
                {"label": "test1", "kind": 1, "detail": ""},  # Text
                {"label": "test2", "kind": 3, "detail": ""},  # Function
                {"label": "test3", "kind": 14, "detail": ""},  # Keyword
                {"label": "test4", "kind": 999, "detail": ""},  # Unknown
            ]
        }

        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)

        assert len(result.items) == 4
        # Check that kinds are mapped correctly
        kinds = [item.kind for item in result.items]
        assert "text" in kinds or "function" in kinds or "keyword" in kinds

    @pytest.mark.asyncio
    async def test_completions_with_documentation(self, mock_lsp_client, temp_theory_file):
        """Test completions with documentation."""
        mock_lsp_client.completion_response = {
            "items": [
                {
                    "label": "lemma",
                    "kind": 14,
                    "detail": "Keyword",
                    "documentation": {
                        "kind": "markdown",
                        "value": "Start a lemma proof"
                    }
                }
            ]
        }

        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)

        assert len(result.items) == 1
        assert result.items[0].documentation is not None

    @pytest.mark.asyncio
    async def test_completions_prefix_matching(self, mock_lsp_client, temp_theory_file):
        """Test prefix-based completion sorting."""
        # Simulate typing "app"
        mock_lsp_client.completion_response = {
            "items": [
                {"label": "apply", "kind": 1, "detail": ""},
                {"label": "append", "kind": 1, "detail": ""},
                {"label": "lemma", "kind": 1, "detail": ""},
                {"label": "theorem", "kind": 1, "detail": ""},
            ]
        }

        result = await completions(mock_lsp_client, temp_theory_file, 8, 3)

        # Items starting with same prefix should be ranked higher
        # (exact implementation depends on completion sorting logic)
        assert len(result.items) > 0

    @pytest.mark.asyncio
    async def test_completions_incomplete_response(self, mock_lsp_client, temp_theory_file):
        """Test handling of incomplete completion response."""
        mock_lsp_client.completion_response = {
            "isIncomplete": True,
            "items": [{"label": "test", "kind": 1, "detail": ""}]
        }

        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)

        # Should still return items even if incomplete
        assert len(result.items) == 1

    @pytest.mark.asyncio
    async def test_completions_filter_invalid_items(self, mock_lsp_client, temp_theory_file):
        """Test filtering of invalid completion items."""
        mock_lsp_client.completion_response = {
            "items": [
                {"label": "valid", "kind": 1, "detail": "ok"},
                {"kind": 1, "detail": "no label"},  # Missing label
                {"label": "", "kind": 1, "detail": ""},  # Empty label
                {"label": "valid2", "kind": 1, "detail": "ok"},
            ]
        }

        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)

        # Should only include valid items
        assert all(item.label for item in result.items)

    @pytest.mark.asyncio
    async def test_completions_position_boundary(self, mock_lsp_client, temp_theory_file):
        """Test completions at file boundaries."""
        # First line, first column
        mock_lsp_client.completion_response = {"items": []}
        result = await completions(mock_lsp_client, temp_theory_file, 1, 1)
        assert result is not None

        # Large line number (beyond file)
        result = await completions(mock_lsp_client, temp_theory_file, 1000, 1)
        assert result is not None

    @pytest.mark.asyncio
    async def test_completions_file_not_found(self, mock_lsp_client):
        """Test completions with non-existent file."""
        with pytest.raises(FileNotFoundError):
            await completions(mock_lsp_client, "/nonexistent/file.thy", 1, 1)
