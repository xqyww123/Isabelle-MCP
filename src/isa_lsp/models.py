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


class CompletionItem(BaseModel):
    label: str = Field(description="Completion text")
    kind: str = Field(description="function | variable | keyword | constant | class | module")
    detail: str = Field(default="", description="Additional info (e.g., type)")
    documentation: str | None = Field(None, description="Description")
    insert_text: str = Field(default="", description="Text to insert (may differ from label)")


class CompletionsResult(BaseModel):
    items: list[CompletionItem] = Field(default_factory=list)
    line_context: str = Field(description="Source line for reference")


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


class Highlight(BaseModel):
    line: int = Field(description="Line number (1-indexed)", ge=1)
    start_column: int = Field(description="Start column (1-indexed)", ge=1)
    end_column: int = Field(description="End column (1-indexed)", ge=1)
    kind: str = Field(description="text | read | write")


class HighlightsResult(BaseModel):
    symbol: str = Field(description="Symbol being highlighted")
    highlights: list[Highlight] = Field(default_factory=list)


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


class GoalState(BaseModel):
    line_context: str = Field(description="Source line where goals were queried")
    goals: list[str] | None = Field(default=None, description="Goals at specific column")
    goals_before: list[str] | None = Field(default=None, description="Goals at line start (before tactic)")
    goals_after: list[str] | None = Field(default=None, description="Goals at line end (after tactic)")
    context: str | None = Field(default=None, description="Local proof context (assumptions, fixes)")


class OutputMessage(BaseModel):
    kind: str = Field(description="writeln | warning | error | information")
    message: str = Field(description="Message content")


class CommandOutputResult(BaseModel):
    line_context: str = Field(description="Source line")
    messages: list[OutputMessage] = Field(default_factory=list)


class PreviewResult(BaseModel):
    html: str = Field(description="HTML preview of theory")
    line_context: str | None = Field(None, description="Source line for context")


class SessionInfo(BaseModel):
    current_session: str = Field(description="Current logic/session name (e.g., HOL)")
    available_sessions: list[str] = Field(description="List of available sessions")


class BuildStatus(BaseModel):
    success: bool = Field(description="Whether build succeeded")
    messages: list[str] = Field(default_factory=list, description="Build output messages")
    session: str = Field(description="Session name that was built")


# Needed because HoverInfo references DiagnosticMessage via forward ref.
HoverInfo.model_rebuild()
