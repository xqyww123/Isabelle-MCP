"""
Edge cases and error handling tests.
"""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from isa_lsp.utils import IsabelleToolError


class TestFilePermissions:
    """Test handling of file permission errors."""

    @pytest.mark.asyncio
    async def test_unreadable_file(self, tmp_path, mock_lsp_client):
        """Test handling of unreadable file."""
        from isa_lsp.tools.hover import hover_info

        # Create a file and make it unreadable (Unix only)
        test_file = tmp_path / "unreadable.thy"
        test_file.write_text("content")

        try:
            test_file.chmod(0o000)

            # Should raise FileNotFoundError or PermissionError
            with pytest.raises((FileNotFoundError, PermissionError, IsabelleToolError)):
                await hover_info(mock_lsp_client, str(test_file), 1, 1)

        finally:
            # Restore permissions for cleanup
            test_file.chmod(0o644)


class TestConcurrency:
    """Test concurrent tool usage."""

    @pytest.mark.asyncio
    async def test_concurrent_hover_requests(self, mock_lsp_client, temp_theory_file):
        """Test multiple concurrent hover requests."""
        from isa_lsp.tools.hover import hover_info

        mock_lsp_client.hover_response = {"contents": "test"}

        # Make multiple concurrent requests
        tasks = [
            hover_info(mock_lsp_client, temp_theory_file, i, 1)
            for i in range(1, 6)
        ]

        results = await asyncio.gather(*tasks)

        assert len(results) == 5
        assert all(r is not None for r in results)

    @pytest.mark.asyncio
    async def test_concurrent_different_tools(self, mock_lsp_client, temp_theory_file):
        """Test concurrent requests to different tools."""
        from isa_lsp.tools.hover import hover_info
        from isa_lsp.tools.completions import completions
        from isa_lsp.tools.diagnostics import diagnostic_messages

        mock_lsp_client.hover_response = {"contents": "test"}
        mock_lsp_client.completion_response = {"items": []}
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []

        # Run different tools concurrently
        results = await asyncio.gather(
            hover_info(mock_lsp_client, temp_theory_file, 5, 15),
            completions(mock_lsp_client, temp_theory_file, 8, 1),
            diagnostic_messages(mock_lsp_client, temp_theory_file),
        )

        assert len(results) == 3
        assert all(r is not None for r in results)


class TestMemoryHandling:
    """Test memory handling with large data."""

    @pytest.mark.asyncio
    async def test_large_completion_list(self, mock_lsp_client, temp_theory_file):
        """Test handling of very large completion list."""
        from isa_lsp.tools.completions import completions

        # Create a very large completion list
        large_items = [
            {"label": f"item_{i}", "kind": 1, "detail": "x" * 1000}
            for i in range(1000)
        ]
        mock_lsp_client.completion_response = {"items": large_items}

        result = await completions(mock_lsp_client, temp_theory_file, 8, 1, max_completions=100)

        # Should limit to max_completions
        assert len(result.items) == 100

    @pytest.mark.asyncio
    async def test_large_diagnostic_list(self, mock_lsp_client, temp_theory_file):
        """Test handling of many diagnostics."""
        from isa_lsp.tools.diagnostics import diagnostic_messages

        # Create many diagnostics
        large_diags = [
            {
                "range": {
                    "start": {"line": i, "character": 0},
                    "end": {"line": i, "character": 10}
                },
                "severity": 1,
                "message": f"Error {i}"
            }
            for i in range(1000)
        ]
        mock_lsp_client.diagnostics_cache[temp_theory_file] = large_diags

        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)

        # Should handle all diagnostics
        assert len(result.items) == 1000

    @pytest.mark.asyncio
    async def test_very_long_lines(self, tmp_path, mock_lsp_client):
        """Test handling of files with very long lines."""
        from isa_lsp.tools.hover import hover_info

        # Create file with very long line
        test_file = tmp_path / "long_lines.thy"
        long_line = "x" * 100000
        test_file.write_text(f"{long_line}\n")

        mock_lsp_client.hover_response = {"contents": "test"}

        result = await hover_info(mock_lsp_client, str(test_file), 1, 50000)

        # Should handle without crashing
        assert result is not None


class TestInvalidInput:
    """Test handling of invalid input."""

    @pytest.mark.asyncio
    async def test_negative_line_number(self, mock_lsp_client, temp_theory_file):
        """Test tools with negative line number."""
        from isa_lsp.tools.hover import hover_info
        from pydantic import ValidationError

        # Pydantic should validate and reject
        with pytest.raises((ValidationError, ValueError, IsabelleToolError)):
            await hover_info(mock_lsp_client, temp_theory_file, -1, 1)

    @pytest.mark.asyncio
    async def test_zero_line_number(self, mock_lsp_client, temp_theory_file):
        """Test tools with zero line number."""
        from isa_lsp.tools.hover import hover_info
        from pydantic import ValidationError

        # Line numbers should be >= 1
        with pytest.raises((ValidationError, ValueError, IsabelleToolError)):
            await hover_info(mock_lsp_client, temp_theory_file, 0, 1)

    @pytest.mark.asyncio
    async def test_negative_column_number(self, mock_lsp_client, temp_theory_file):
        """Test tools with negative column number."""
        from isa_lsp.tools.hover import hover_info
        from pydantic import ValidationError

        with pytest.raises((ValidationError, ValueError, IsabelleToolError)):
            await hover_info(mock_lsp_client, temp_theory_file, 1, -1)

    @pytest.mark.asyncio
    async def test_none_file_path(self, mock_lsp_client):
        """Test tools with None file path."""
        from isa_lsp.tools.hover import hover_info

        with pytest.raises((TypeError, FileNotFoundError, IsabelleToolError)):
            await hover_info(mock_lsp_client, None, 1, 1)

    @pytest.mark.asyncio
    async def test_empty_file_path(self, mock_lsp_client):
        """Test tools with empty file path."""
        from isa_lsp.tools.hover import hover_info

        with pytest.raises((FileNotFoundError, IsabelleToolError)):
            await hover_info(mock_lsp_client, "", 1, 1)

    @pytest.mark.asyncio
    async def test_relative_file_path(self, mock_lsp_client):
        """Test tools with relative file path."""
        from isa_lsp.tools.hover import hover_info

        # Relative paths should work if they exist
        mock_lsp_client.hover_response = {"contents": "test"}

        # This will likely fail because relative path doesn't exist
        with pytest.raises((FileNotFoundError, IsabelleToolError)):
            await hover_info(mock_lsp_client, "./relative/path.thy", 1, 1)


class TestModelValidation:
    """Test Pydantic model validation."""

    def test_hover_info_invalid_data(self):
        """Test HoverInfo with invalid data."""
        from isa_lsp.models import HoverInfo
        from pydantic import ValidationError

        # Missing required fields
        with pytest.raises(ValidationError):
            HoverInfo()

        # Invalid field types
        with pytest.raises(ValidationError):
            HoverInfo(symbol=123, info="test", line_context="test")

    def test_location_invalid_line(self):
        """Test Location with invalid line number."""
        from isa_lsp.models import Location
        from pydantic import ValidationError

        # Line must be >= 1
        with pytest.raises(ValidationError):
            Location(file_path="/test.thy", line=0, column=1)

    def test_location_invalid_column(self):
        """Test Location with invalid column number."""
        from isa_lsp.models import Location
        from pydantic import ValidationError

        # Column must be >= 1
        with pytest.raises(ValidationError):
            Location(file_path="/test.thy", line=1, column=0)

    def test_diagnostic_message_invalid_severity(self):
        """Test DiagnosticMessage with invalid severity."""
        from isa_lsp.models import DiagnosticMessage

        # Should accept valid severity strings
        diag = DiagnosticMessage(
            severity="error",
            message="test",
            line=1,
            column=1
        )
        assert diag.severity == "error"

    def test_completion_item_invalid_kind(self):
        """Test CompletionItem with invalid kind."""
        from isa_lsp.models import CompletionItem

        # Should accept any string for kind
        item = CompletionItem(
            label="test",
            kind="unknown_kind",
            detail="test"
        )
        assert item.kind == "unknown_kind"


class TestRaceConditions:
    """Test potential race conditions."""

    @pytest.mark.asyncio
    async def test_concurrent_document_open(self, mock_lsp_client, temp_theory_file):
        """Test concurrent document open requests."""
        from isa_lsp.tools.hover import hover_info

        mock_lsp_client.hover_response = {"contents": "test"}

        # Multiple tools trying to open same document
        tasks = [
            hover_info(mock_lsp_client, temp_theory_file, i, 1)
            for i in range(1, 10)
        ]

        results = await asyncio.gather(*tasks)

        # All should succeed
        assert all(r is not None for r in results)
        # Document should be open only once
        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_diagnostics_cache_update_race(self, mock_lsp_client, temp_theory_file):
        """Test concurrent diagnostics cache updates."""
        from isa_lsp.tools.diagnostics import diagnostic_messages

        # Simulate concurrent cache updates
        async def update_cache():
            mock_lsp_client.diagnostics_cache[temp_theory_file] = [
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 1}
                    },
                    "severity": 1,
                    "message": "test"
                }
            ]
            return await diagnostic_messages(mock_lsp_client, temp_theory_file)

        tasks = [update_cache() for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # All should complete successfully
        assert all(r is not None for r in results)


class TestUnicodeHandling:
    """Test handling of Unicode characters."""

    @pytest.mark.asyncio
    async def test_hover_unicode_symbols(self, tmp_path, mock_lsp_client):
        """Test hover on Isabelle Unicode symbols."""
        from isa_lsp.tools.hover import hover_info

        test_file = tmp_path / "unicode.thy"
        test_file.write_text("lemma \"∀x. P x ⟹ Q x\"\n", encoding='utf-8')

        mock_lsp_client.hover_response = {
            "contents": "Universal quantifier: ∀"
        }

        result = await hover_info(mock_lsp_client, str(test_file), 1, 8)

        assert result is not None
        # Should handle Unicode in response
        if result.info:
            assert isinstance(result.info, str)

    @pytest.mark.asyncio
    async def test_completion_unicode_labels(self, mock_lsp_client, temp_theory_file):
        """Test completions with Unicode labels."""
        from isa_lsp.tools.completions import completions

        mock_lsp_client.completion_response = {
            "items": [
                {"label": "∀", "kind": 1, "detail": "Universal quantifier"},
                {"label": "∃", "kind": 1, "detail": "Existential quantifier"},
                {"label": "⟹", "kind": 1, "detail": "Implies"},
            ]
        }

        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)

        assert len(result.items) == 3
        # All labels should be properly handled
        assert all(item.label for item in result.items)


class TestEmptyResponses:
    """Test handling of empty or minimal responses."""

    @pytest.mark.asyncio
    async def test_hover_empty_contents(self, mock_lsp_client, temp_theory_file):
        """Test hover with empty contents."""
        from isa_lsp.tools.hover import hover_info

        mock_lsp_client.hover_response = {"contents": ""}

        result = await hover_info(mock_lsp_client, temp_theory_file, 5, 15)

        assert result is not None
        assert result.info == ""

    @pytest.mark.asyncio
    async def test_completions_single_item(self, mock_lsp_client, temp_theory_file):
        """Test completions with single item."""
        from isa_lsp.tools.completions import completions

        mock_lsp_client.completion_response = {
            "items": [{"label": "only_one", "kind": 1, "detail": ""}]
        }

        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)

        assert len(result.items) == 1
        assert result.items[0].label == "only_one"

    @pytest.mark.asyncio
    async def test_highlights_single_occurrence(self, mock_lsp_client, temp_theory_file):
        """Test highlights with single occurrence."""
        from isa_lsp.tools.highlights import document_highlights

        mock_lsp_client.highlights_response = [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 5}
                },
                "kind": 1
            }
        ]

        result = await document_highlights(mock_lsp_client, temp_theory_file, 1, 1)

        assert len(result.highlights) == 1
