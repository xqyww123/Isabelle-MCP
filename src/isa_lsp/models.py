"""Pydantic models for MCP tool inputs and outputs.

All positions are 1-indexed (MCP convention).
"""

from pydantic import BaseModel, Field


class HoverInfo(BaseModel):
    symbol: str = Field(description="Symbol text at position")
    info: str = Field(description="Type signature and documentation")
    line_context: str = Field(description="Full source line for reference")
    diagnostics: list["DiagnosticMessage"] = Field(
        default_factory=list, description="Diagnostics at this position"
    )
    note: str | None = Field(default=None, description="Warning note (e.g. line still running)")


class Location(BaseModel):
    file_path: str = Field(description="Absolute path to file")
    line: int = Field(description="Line number (1-indexed)", ge=1)
    column: int = Field(description="Column number (1-indexed)", ge=1)


class DeclarationLocation(BaseModel):
    symbol: str = Field(description="Symbol being queried")
    locations: list[Location] = Field(
        default_factory=list,
        description="Definition locations (may be multiple for overloaded symbols)",
    )
    note: str | None = Field(default=None, description="Warning note (e.g. line still running)")


class Highlight(BaseModel):
    line: int = Field(description="Line number (1-indexed)", ge=1)
    start_column: int = Field(description="Start column (1-indexed)", ge=1)
    end_column: int = Field(description="End column (1-indexed)", ge=1)
    kind: str = Field(description="text | read | write")


class HighlightsResult(BaseModel):
    symbol: str = Field(description="Symbol being highlighted")
    highlights: list[Highlight] = Field(default_factory=list)
    note: str | None = Field(default=None, description="Warning note (e.g. line still running)")


class DiagnosticMessage(BaseModel):
    severity: str = Field(description="error | warning | information | hint")
    message: str = Field(description="Diagnostic message text")
    line: int = Field(description="Line number (1-indexed)", ge=1)
    column: int = Field(description="Column number (1-indexed)", ge=1)
    end_line: int | None = Field(None, description="End line (1-indexed)", ge=1)
    end_column: int | None = Field(None, description="End column (1-indexed)", ge=1)


class DiagnosticsResult(BaseModel):
    success: bool = Field(True, description="True if the queried file/range has no errors")
    items: list[DiagnosticMessage] = Field(default_factory=list)
    processing_complete: bool = Field(description="Whether PIDE finished processing")
    failed_dependencies: list[str] = Field(
        default_factory=list, description="File paths of theories that failed to load"
    )
    note: str | None = Field(default=None, description="Warning note (e.g. line still running)")


class GoalState(BaseModel):
    line_context: str = Field(description="Source line where goals were queried")
    goals: list[str] | None = Field(default=None, description="Goals at specific column")
    goals_before: list[str] | None = Field(default=None, description="Goals at line start (before tactic)")
    goals_after: list[str] | None = Field(default=None, description="Goals at line end (after tactic)")
    context: str | None = Field(default=None, description="Local proof context (assumptions, fixes)")
    note: str | None = Field(default=None, description="Warning note (e.g. line still running)")


class OutputMessage(BaseModel):
    kind: str = Field(description="writeln | warning | error | information")
    message: str = Field(description="Message content")


class CommandOutputResult(BaseModel):
    line_context: str = Field(description="Source line")
    messages: list[OutputMessage] = Field(default_factory=list)
    note: str | None = Field(default=None, description="Warning note (e.g. line still running)")


class TheoryStatus(BaseModel):
    node_name: str = Field(description="File path from PIDE/theory_status")
    theory_name: str = Field(description="Qualified theory name (e.g. Test.A)")
    external: bool = Field(description="True if auto-loaded (not explicitly opened)")
    imports: list[str] = Field(default_factory=list, description="Imported theory names")
    ok: bool = Field(description="True if no errors")
    total: int = Field(description="Total number of commands")
    unprocessed: int = Field(description="Commands not yet processed")
    running: int = Field(description="Commands currently executing")
    warned: int = Field(default=0, description="Commands with warnings")
    failed: int = Field(default=0, description="Commands that failed")
    finished: int = Field(default=0, description="Commands that finished")
    canceled: bool = Field(default=False, description="True if execution was canceled")
    consolidated: bool = Field(default=False, description="True if fully processed")
    percentage: int = Field(default=0, description="Processing progress (0-100)")


class RunningCommand(BaseModel):
    file_path: str = Field(description="Absolute path to the file")
    start_line: int = Field(description="Start line (1-indexed)", ge=1)
    end_line: int = Field(description="End line (1-indexed)", ge=1)
    text: str = Field(description="Command source text")
    elapsed_seconds: float = Field(description="Seconds since command started running")


class EvaluationResult(BaseModel):
    status: str = Field(
        description="complete | in_progress | no_evaluation | cancelled",
    )
    errors: list[DiagnosticMessage] = Field(
        default_factory=list, description="New errors since last check",
    )
    theories: list[TheoryStatus] = Field(
        default_factory=list,
        description="Status of all loaded theories (from PIDE/theory_status)",
    )
    running_commands: list[RunningCommand] = Field(
        default_factory=list,
        description="Commands currently being executed (from opened files only)",
    )
    destination_line: int | None = Field(
        default=None, description="Target line for evaluation (1-indexed)",
    )
    message: str = Field(default="", description="Human-readable status message")


class SessionInfo(BaseModel):
    current_session: str = Field(description="Current logic/session name (e.g., HOL)")


# Needed because models reference DiagnosticMessage/TheoryStatus/RunningCommand via forward ref.
HoverInfo.model_rebuild()
EvaluationResult.model_rebuild()
