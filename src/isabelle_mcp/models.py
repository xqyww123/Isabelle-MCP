"""Pydantic models for MCP tool inputs and outputs.

All positions are 1-indexed (MCP convention).
"""

from dataclasses import dataclass, field

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


class CommandSpan(BaseModel):
    text: str = Field(description="Full source text of the Isar command (may span multiple lines)")
    start_line: int = Field(ge=1, description="Command start line (1-indexed)")
    start_column: int = Field(ge=1, description="Command start column (1-indexed)")
    end_line: int = Field(ge=1, description="Command end line (1-indexed)")
    end_column: int = Field(ge=1, description="Command end column (1-indexed, just past the last character)")

    @classmethod
    def from_lsp(cls, result: "tuple[str, dict] | None") -> "CommandSpan | None":
        """Build from a PIDE/command_at_position result (source, 0-indexed LSP range)."""
        if result is None:
            return None
        source, rng = result
        start, end = rng.get("start", {}), rng.get("end", {})
        return cls(
            text=source,
            start_line=int(start.get("line", 0)) + 1,
            start_column=int(start.get("character", 0)) + 1,
            end_line=int(end.get("line", 0)) + 1,
            end_column=int(end.get("character", 0)) + 1,
        )


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
    kind: str = Field(description="normal | tracing | warning | error | information | state")
    message: str = Field(description="Message content")


class CommandOutputResult(BaseModel):
    command: CommandSpan | None = Field(
        default=None,
        description=(
            "The Isar command enclosing the queried position — its full source text "
            "and range. None if there is no command at the position."
        ),
    )
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


@dataclass
class FileSnapshot:
    """Per-file problem snapshot for an evaluation result.

    Two flavours:
      - decoration source (``lined=True``): ``errors``/``warnings``/``running`` hold
        1-indexed ``(start_line, end_line)`` spans (errors = line-deduped union of
        text_overview_error and background_bad).
      - theory_status fallback (``lined=False``): spans are empty; the ``*_count``
        fields hold theory_status counts; ``state`` is "clean" / "in_progress".
    """

    file_path: str
    lined: bool
    state: str  # "clean" | "in_progress" | "problems"
    errors: list[tuple[int, int]] = field(default_factory=list)
    warnings: list[tuple[int, int]] = field(default_factory=list)
    running: list[tuple[int, int]] = field(default_factory=list)
    # Unprocessed spans of the target file within the evaluated prefix [0, dest]:
    # commands not yet finished (forked-but-queued proofs, or lines the frontier has
    # not closed). Surfaced so an in_progress snapshot never renders a bare "clean"
    # while work remains. Only the evaluation target carries these (dest-clipped).
    pending: list[tuple[int, int]] = field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    running_count: int = 0
    pending_count: int = 0


@dataclass
class EvaluationView:
    """Internal structured evaluation result.

    Rendered to the agent-facing string by ``format_evaluation_result``; the core
    evaluation functions return this so unit tests can assert on structured fields.
    """

    status: str  # complete | in_progress | no_evaluation | cancelled
    destination_line: int | None = None
    message: str = ""
    files: list[FileSnapshot] = field(default_factory=list)
    running_commands: list[RunningCommand] = field(default_factory=list)
    # Set when the target file is precompiled into the running session's heap
    # (edits to it are ignored by Isabelle); rendered as a prominent warning.
    heap_warning: str | None = None


class SessionInfo(BaseModel):
    current_session: str = Field(description="Current logic/session name (e.g., HOL)")
    version: str | None = Field(
        default=None, description="Isabelle server version reported at initialize (None if unknown)"
    )


# Needed because models reference DiagnosticMessage via forward ref.
HoverInfo.model_rebuild()
