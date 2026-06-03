"""Pydantic models for MCP tool inputs and outputs.

All positions are 1-indexed (MCP convention).
"""

from pydantic import BaseModel, Field, model_validator


class HoverEntry(BaseModel):
    info: str = Field(description="Type signature and documentation")
    occurrences: list[int] = Field(description="1-indexed occurrence indices on the line")
    columns: list[int] = Field(description="1-indexed column positions of those occurrences")

    @model_validator(mode="after")
    def _check_parallel_lists(self) -> "HoverEntry":
        if len(self.occurrences) != len(self.columns):
            raise ValueError("occurrences and columns must have the same length")
        return self


class HoverInfo(BaseModel):
    symbol: str = Field(description="Queried symbol text")
    results: list[HoverEntry] = Field(default_factory=list, description="Hover results grouped by content")
    line_context: str = Field(description="Full source line for reference")
    diagnostics: list["DiagnosticMessage"] = Field(
        default_factory=list, description="Diagnostics on this line"
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


class Occurrence(BaseModel):
    line: int = Field(description="Line number (1-indexed)", ge=1)
    start_column: int = Field(description="Start column (1-indexed)", ge=1)
    end_column: int = Field(description="End column (1-indexed)", ge=1)


class LocalOccurrencesResult(BaseModel):
    symbol: str = Field(description="Symbol being looked up")
    occurrences: list[Occurrence] = Field(default_factory=list)
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


class CommandSpan(BaseModel):
    text: str = Field(description="Full source text of the Isar command (may span multiple lines)")
    start_line: int = Field(ge=1, description="Command start line (1-indexed)")
    start_column: int = Field(ge=1, description="Command start column (1-indexed)")
    end_line: int = Field(ge=1, description="Command end line (1-indexed)")
    end_column: int = Field(ge=1, description="Command end column (1-indexed, just past the last character)")


class GoalState(BaseModel):
    command: CommandSpan | None = Field(
        default=None,
        description=(
            "The Isar command enclosing the queried position — its full source text "
            "and range. None if there is no command at the position."
        ),
    )
    subgoals: list[str] = Field(
        default_factory=list,
        description=(
            "Open subgoals of the proof state after the command runs — one string "
            "per subgoal; empty list means no subgoals remain (proof finished here)."
        ),
    )
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
        description="Status of all loaded theories",
    )
    running_commands: list[RunningCommand] = Field(
        default_factory=list,
        description="Commands currently being executed",
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
