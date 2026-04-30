"""
Unit tests for Pydantic models.
"""

import pytest
from pydantic import ValidationError

from isa_lsp.models import (
    BuildStatus,
    CommandOutputResult,
    CompletionItem,
    CompletionsResult,
    DeclarationLocation,
    DiagnosticMessage,
    DiagnosticsResult,
    GoalState,
    Highlight,
    HoverInfo,
    Location,
    OutputMessage,
    PreviewResult,
    SessionInfo,
)


class TestHoverInfo:
    """Test HoverInfo model."""

    def test_hover_info_basic(self):
        """Test basic HoverInfo creation."""
        hover = HoverInfo(
            symbol="test_lemma",
            info="Type: bool",
            line_context="lemma test_lemma:",
        )
        assert hover.symbol == "test_lemma"
        assert hover.info == "Type: bool"
        assert hover.line_context == "lemma test_lemma:"
        assert hover.diagnostics == []

    def test_hover_info_with_diagnostics(self):
        """Test HoverInfo with diagnostics."""
        diag = DiagnosticMessage(
            severity="error",
            message="Type error",
            line=1,
            column=1,
        )
        hover = HoverInfo(
            symbol="test",
            info="Info",
            line_context="test",
            diagnostics=[diag],
        )
        assert len(hover.diagnostics) == 1
        assert hover.diagnostics[0].severity == "error"


class TestCompletionItem:
    """Test CompletionItem model."""

    def test_completion_item_basic(self):
        """Test basic CompletionItem creation."""
        item = CompletionItem(
            label="lemma",
            kind="keyword",
            detail="Isabelle keyword",
        )
        assert item.label == "lemma"
        assert item.kind == "keyword"
        assert item.detail == "Isabelle keyword"
        assert item.documentation is None

    def test_completion_item_with_documentation(self):
        """Test CompletionItem with documentation."""
        item = CompletionItem(
            label="apply",
            kind="keyword",
            detail="Apply tactic",
            documentation="Apply a proof method",
        )
        assert item.documentation == "Apply a proof method"


class TestCompletionsResult:
    """Test CompletionsResult model."""

    def test_completions_result_empty(self):
        """Test empty CompletionsResult."""
        result = CompletionsResult(
            line_context="test",
            items=[],
        )
        assert result.items == []
        assert result.line_context == "test"

    def test_completions_result_with_items(self):
        """Test CompletionsResult with items."""
        items = [
            CompletionItem(label="lemma", kind="keyword", detail="Keyword"),
            CompletionItem(label="theorem", kind="keyword", detail="Keyword"),
        ]
        result = CompletionsResult(
            line_context="test",
            items=items,
        )
        assert len(result.items) == 2


class TestLocation:
    """Test Location model."""

    def test_location_basic(self):
        """Test basic Location creation."""
        loc = Location(
            file_path="/path/to/file.thy",
            line=10,
            column=5,
        )
        assert loc.file_path == "/path/to/file.thy"
        assert loc.line == 10
        assert loc.column == 5

    def test_location_validation_line(self):
        """Test Location validation for line number."""
        with pytest.raises(ValidationError):
            Location(file_path="/test.thy", line=0, column=1)

    def test_location_validation_column(self):
        """Test Location validation for column number."""
        with pytest.raises(ValidationError):
            Location(file_path="/test.thy", line=1, column=0)


class TestDeclarationLocation:
    """Test DeclarationLocation model."""

    def test_declaration_location_empty(self):
        """Test DeclarationLocation with no locations."""
        decl = DeclarationLocation(
            symbol="test",
            locations=[],
        )
        assert decl.symbol == "test"
        assert decl.locations == []

    def test_declaration_location_with_locations(self):
        """Test DeclarationLocation with multiple locations."""
        locs = [
            Location(file_path="/test1.thy", line=1, column=1),
            Location(file_path="/test2.thy", line=10, column=5),
        ]
        decl = DeclarationLocation(
            symbol="my_lemma",
            locations=locs,
        )
        assert len(decl.locations) == 2


class TestHighlight:
    """Test Highlight model."""

    def test_highlight_basic(self):
        """Test basic Highlight creation."""
        h = Highlight(
            line=5,
            start_column=10,
            end_column=20,
            kind="text",
        )
        assert h.line == 5
        assert h.start_column == 10
        assert h.end_column == 20
        assert h.kind == "text"

    def test_highlight_validation_kind(self):
        """Test Highlight validation for kind."""
        # Valid kinds
        for kind in ["text", "read", "write"]:
            h = Highlight(line=1, start_column=1, end_column=2, kind=kind)
            assert h.kind == kind


class TestDiagnosticMessage:
    """Test DiagnosticMessage model."""

    def test_diagnostic_message_basic(self):
        """Test basic DiagnosticMessage creation."""
        diag = DiagnosticMessage(
            severity="error",
            message="Type mismatch",
            line=10,
            column=5,
        )
        assert diag.severity == "error"
        assert diag.message == "Type mismatch"
        assert diag.line == 10
        assert diag.column == 5

    def test_diagnostic_message_with_end_position(self):
        """Test DiagnosticMessage with end position."""
        diag = DiagnosticMessage(
            severity="warning",
            message="Unused variable",
            line=5,
            column=10,
            end_line=5,
            end_column=15,
        )
        assert diag.end_line == 5
        assert diag.end_column == 15


class TestDiagnosticsResult:
    """Test DiagnosticsResult model."""

    def test_diagnostics_result_success(self):
        """Test DiagnosticsResult with success."""
        result = DiagnosticsResult(
            success=True,
            items=[],
            processing_complete=True,
            failed_dependencies=[],
        )
        assert result.success is True
        assert result.processing_complete is True
        assert len(result.items) == 0

    def test_diagnostics_result_with_errors(self):
        """Test DiagnosticsResult with errors."""
        errors = [
            DiagnosticMessage(
                severity="error",
                message="Error 1",
                line=1,
                column=1,
            ),
        ]
        result = DiagnosticsResult(
            success=False,
            items=errors,
            processing_complete=True,
            failed_dependencies=[],
        )
        assert result.success is False
        assert len(result.items) == 1


class TestGoalState:
    """Test GoalState model."""

    def test_goal_state_with_goals(self):
        """Test GoalState with goals at specific position."""
        state = GoalState(
            line_context="apply auto",
            goals=["P ⟹ Q", "Q ⟹ R"],
        )
        assert len(state.goals) == 2
        assert state.goals_before is None
        assert state.goals_after is None

    def test_goal_state_before_after(self):
        """Test GoalState with before/after goals."""
        state = GoalState(
            line_context="apply auto",
            goals_before=["P ⟹ Q", "Q ⟹ R"],
            goals_after=["R"],
        )
        assert state.goals is None
        assert len(state.goals_before) == 2
        assert len(state.goals_after) == 1


class TestOutputMessage:
    """Test OutputMessage model."""

    def test_output_message_basic(self):
        """Test basic OutputMessage creation."""
        msg = OutputMessage(
            kind="writeln",
            message="Output message",
        )
        assert msg.kind == "writeln"
        assert msg.message == "Output message"


class TestCommandOutputResult:
    """Test CommandOutputResult model."""

    def test_command_output_result_empty(self):
        """Test empty CommandOutputResult."""
        result = CommandOutputResult(
            line_context="lemma test:",
            messages=[],
        )
        assert result.line_context == "lemma test:"
        assert len(result.messages) == 0

    def test_command_output_result_with_messages(self):
        """Test CommandOutputResult with messages."""
        msgs = [
            OutputMessage(kind="writeln", message="Message 1"),
            OutputMessage(kind="warning", message="Warning"),
        ]
        result = CommandOutputResult(
            line_context="test",
            messages=msgs,
        )
        assert len(result.messages) == 2


class TestPreviewResult:
    """Test PreviewResult model."""

    def test_preview_result_basic(self):
        """Test basic PreviewResult creation."""
        result = PreviewResult(
            html="<html><body>Test</body></html>",
        )
        assert result.html == "<html><body>Test</body></html>"
        assert result.line_context is None

    def test_preview_result_with_context(self):
        """Test PreviewResult with line context."""
        result = PreviewResult(
            html="<html></html>",
            line_context="theory Test",
        )
        assert result.line_context == "theory Test"


class TestSessionInfo:
    """Test SessionInfo model."""

    def test_session_info_basic(self):
        """Test basic SessionInfo creation."""
        info = SessionInfo(
            current_session="HOL",
            available_sessions=["Pure", "HOL", "Main"],
        )
        assert info.current_session == "HOL"
        assert len(info.available_sessions) == 3
        assert "HOL" in info.available_sessions


class TestBuildStatus:
    """Test BuildStatus model."""

    def test_build_status_success(self):
        """Test successful BuildStatus."""
        status = BuildStatus(
            success=True,
            messages=["Building...", "Success"],
            session="HOL",
        )
        assert status.success is True
        assert status.session == "HOL"
        assert len(status.messages) == 2

    def test_build_status_failure(self):
        """Test failed BuildStatus."""
        status = BuildStatus(
            success=False,
            messages=["Building...", "Error: failed"],
            session="HOL",
        )
        assert status.success is False
        assert "Error" in status.messages[1]
