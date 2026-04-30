"""
Unit tests for diagnostics tool.
"""

import pytest

from isa_lsp.tools.diagnostics import diagnostic_messages


class TestDiagnosticsTool:
    """Test diagnostic_messages tool."""

    @pytest.mark.asyncio
    async def test_diagnostics_basic(self, mock_lsp_client, temp_theory_file, sample_diagnostics):
        """Test basic diagnostics functionality."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = sample_diagnostics

        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)

        assert len(result.items) == 2
        assert result.items[0].severity == "error"
        assert result.items[1].severity == "warning"
        assert result.success is False  # Has errors

    @pytest.mark.asyncio
    async def test_diagnostics_no_errors(self, mock_lsp_client, temp_theory_file):
        """Test diagnostics with no errors."""
        # Only warnings
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 10}
                },
                "severity": 2,  # Warning
                "message": "Unused variable"
            }
        ]

        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)

        assert len(result.items) == 1
        assert result.success is True  # No errors, only warnings

    @pytest.mark.asyncio
    async def test_diagnostics_empty(self, mock_lsp_client, temp_theory_file):
        """Test diagnostics with no issues."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []

        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)

        assert result.items == []
        assert result.success is True
        assert result.processing_complete is False  # Default

    @pytest.mark.asyncio
    async def test_diagnostics_line_filter(self, mock_lsp_client, temp_theory_file):
        """Test diagnostics with line range filter."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {
                "range": {
                    "start": {"line": 4, "character": 0},  # Line 5 in 1-indexed
                    "end": {"line": 4, "character": 10}
                },
                "severity": 1,
                "message": "Error on line 5"
            },
            {
                "range": {
                    "start": {"line": 9, "character": 0},  # Line 10
                    "end": {"line": 9, "character": 10}
                },
                "severity": 1,
                "message": "Error on line 10"
            },
            {
                "range": {
                    "start": {"line": 14, "character": 0},  # Line 15
                    "end": {"line": 14, "character": 10}
                },
                "severity": 1,
                "message": "Error on line 15"
            }
        ]

        # Filter lines 5-10
        result = await diagnostic_messages(
            mock_lsp_client, temp_theory_file,
            start_line=5, end_line=10
        )

        assert len(result.items) == 2  # Only lines 5 and 10

    @pytest.mark.asyncio
    async def test_diagnostics_start_line_only(self, mock_lsp_client, temp_theory_file):
        """Test diagnostics with only start_line filter."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {
                "range": {
                    "start": {"line": 4, "character": 0},
                    "end": {"line": 4, "character": 10}
                },
                "severity": 1,
                "message": "Line 5"
            },
            {
                "range": {
                    "start": {"line": 9, "character": 0},
                    "end": {"line": 9, "character": 10}
                },
                "severity": 1,
                "message": "Line 10"
            }
        ]

        result = await diagnostic_messages(
            mock_lsp_client, temp_theory_file,
            start_line=10
        )

        assert len(result.items) == 1  # Only line 10

    @pytest.mark.asyncio
    async def test_diagnostics_end_line_only(self, mock_lsp_client, temp_theory_file):
        """Test diagnostics with only end_line filter."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {
                "range": {
                    "start": {"line": 4, "character": 0},
                    "end": {"line": 4, "character": 10}
                },
                "severity": 1,
                "message": "Line 5"
            },
            {
                "range": {
                    "start": {"line": 9, "character": 0},
                    "end": {"line": 9, "character": 10}
                },
                "severity": 1,
                "message": "Line 10"
            }
        ]

        result = await diagnostic_messages(
            mock_lsp_client, temp_theory_file,
            end_line=5
        )

        assert len(result.items) == 1  # Only line 5

    @pytest.mark.asyncio
    async def test_diagnostics_severity_mapping(self, mock_lsp_client, temp_theory_file):
        """Test LSP severity to string mapping."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
             "severity": 1, "message": "Error"},
            {"range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 1}},
             "severity": 2, "message": "Warning"},
            {"range": {"start": {"line": 2, "character": 0}, "end": {"line": 2, "character": 1}},
             "severity": 3, "message": "Info"},
            {"range": {"start": {"line": 3, "character": 0}, "end": {"line": 3, "character": 1}},
             "severity": 4, "message": "Hint"},
        ]

        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)

        severities = [item.severity for item in result.items]
        assert "error" in severities
        assert "warning" in severities
        assert "information" in severities
        assert "hint" in severities

    @pytest.mark.asyncio
    async def test_diagnostics_unknown_severity(self, mock_lsp_client, temp_theory_file):
        """Test diagnostics with unknown severity."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 10}
                },
                "severity": 999,  # Unknown
                "message": "Unknown severity"
            }
        ]

        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)

        # Should default to "error"
        assert result.items[0].severity == "error"

    @pytest.mark.asyncio
    async def test_diagnostics_position_conversion(self, mock_lsp_client, temp_theory_file):
        """Test position conversion LSP to MCP."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {
                "range": {
                    "start": {"line": 0, "character": 0},  # LSP 0-indexed
                    "end": {"line": 0, "character": 10}
                },
                "severity": 1,
                "message": "Test"
            }
        ]

        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)

        # Should convert to MCP 1-indexed
        assert result.items[0].line == 1
        assert result.items[0].column == 1
        assert result.items[0].end_line == 1
        assert result.items[0].end_column == 11

    @pytest.mark.asyncio
    async def test_diagnostics_processing_complete(self, mock_lsp_client, temp_theory_file):
        """Test processing_complete flag."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []
        mock_lsp_client.processing_status[temp_theory_file] = True

        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)

        assert result.processing_complete is True

    @pytest.mark.asyncio
    async def test_diagnostics_auto_open(self, mock_lsp_client, temp_theory_file):
        """Test that diagnostics auto-opens document."""
        assert temp_theory_file not in mock_lsp_client.open_documents

        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)

        assert temp_theory_file in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_diagnostics_interactive_mode(self, mock_lsp_client, temp_theory_file):
        """Test interactive mode parameter (not implemented in MVP)."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []

        # interactive=True should not crash, but doesn't do anything in MVP
        result = await diagnostic_messages(
            mock_lsp_client, temp_theory_file,
            interactive=True
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_diagnostics_multiline_range(self, mock_lsp_client, temp_theory_file):
        """Test diagnostics with multiline range."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {
                "range": {
                    "start": {"line": 5, "character": 10},
                    "end": {"line": 8, "character": 20}  # Spans multiple lines
                },
                "severity": 1,
                "message": "Multiline error"
            }
        ]

        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)

        assert len(result.items) == 1
        assert result.items[0].line == 6  # Start line
        assert result.items[0].end_line == 9  # End line

    @pytest.mark.asyncio
    async def test_diagnostics_file_not_found(self, mock_lsp_client):
        """Test diagnostics with non-existent file."""
        with pytest.raises(FileNotFoundError):
            await diagnostic_messages(mock_lsp_client, "/nonexistent/file.thy")

    @pytest.mark.asyncio
    async def test_diagnostics_success_calculation(self, mock_lsp_client, temp_theory_file):
        """Test success flag calculation."""
        # Case 1: No diagnostics = success
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []
        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)
        assert result.success is True

        # Case 2: Only warnings = success
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
             "severity": 2, "message": "Warning"}
        ]
        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)
        assert result.success is True

        # Case 3: Has errors = not success
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
             "severity": 1, "message": "Error"}
        ]
        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_diagnostics_failed_dependencies(self, mock_lsp_client, temp_theory_file):
        """Test failed_dependencies field (not implemented in MVP)."""
        mock_lsp_client.diagnostics_cache[temp_theory_file] = []

        result = await diagnostic_messages(mock_lsp_client, temp_theory_file)

        # Should be empty list in MVP
        assert result.failed_dependencies == []
