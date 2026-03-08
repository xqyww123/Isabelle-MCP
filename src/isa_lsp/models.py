"""
Pydantic models for Isa-LSP MCP tool inputs and outputs.

All models follow lean-lsp-mcp patterns:
- Structured outputs (no bare lists)
- 1-indexed positions
- Consistent field naming
"""

from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


# ============================================================================
# Standard LSP Tool Models
# ============================================================================

class HoverInfo(BaseModel):
    """Hover information result."""
    symbol: str = Field(description="Symbol text at position")
    info: str = Field(description="Type signature and documentation")
    line_context: str = Field(description="Full source line for reference")
    diagnostics: List["DiagnosticMessage"] = Field(
        default_factory=list,
        description="Diagnostics at this position"
    )


class CompletionItem(BaseModel):
    """Single completion suggestion."""
    label: str = Field(description="Completion text")
    kind: str = Field(description="function | variable | keyword | constant | class | module")
    detail: str = Field(default="", description="Additional info (e.g., type)")
    documentation: Optional[str] = Field(None, description="Description")


class CompletionsResult(BaseModel):
    """Code completion result."""
    items: List[CompletionItem] = Field(default_factory=list)
    line_context: str = Field(description="Source line for reference")


class Location(BaseModel):
    """Source code location."""
    file_path: str = Field(description="Absolute path to file")
    line: int = Field(description="Line number (1-indexed)", ge=1)
    column: int = Field(description="Column number (1-indexed)", ge=1)


class DeclarationLocation(BaseModel):
    """Declaration location result."""
    symbol: str = Field(description="Symbol being queried")
    locations: List[Location] = Field(
        default_factory=list,
        description="Definition locations (may be multiple for overloaded symbols)"
    )


class Highlight(BaseModel):
    """Document highlight (occurrence of symbol)."""
    line: int = Field(description="Line number (1-indexed)", ge=1)
    start_column: int = Field(description="Start column (1-indexed)", ge=1)
    end_column: int = Field(description="End column (1-indexed)", ge=1)
    kind: str = Field(description="text | read | write")


class HighlightsResult(BaseModel):
    """Document highlights result."""
    symbol: str = Field(description="Symbol being highlighted")
    highlights: List[Highlight] = Field(default_factory=list)


class DiagnosticMessage(BaseModel):
    """Single diagnostic message."""
    severity: str = Field(description="error | warning | information | hint")
    message: str = Field(description="Diagnostic message text")
    line: int = Field(description="Line number (1-indexed)", ge=1)
    column: int = Field(description="Column number (1-indexed)", ge=1)
    end_line: Optional[int] = Field(None, description="End line (1-indexed)", ge=1)
    end_column: Optional[int] = Field(None, description="End column (1-indexed)", ge=1)


class DiagnosticsResult(BaseModel):
    """Diagnostics result."""
    success: bool = Field(True, description="True if the queried file/range has no errors")
    items: List[DiagnosticMessage] = Field(default_factory=list)
    processing_complete: bool = Field(description="Whether PIDE finished processing")
    failed_dependencies: List[str] = Field(
        default_factory=list,
        description="File paths of theories that failed to load"
    )


# ============================================================================
# PIDE Extension Tool Models
# ============================================================================

class GoalState(BaseModel):
    """Proof state result."""
    line_context: str = Field(description="Source line where goals were queried")

    # If column is provided:
    goals: Optional[List[str]] = Field(None, description="Goals at specific column")

    # If column is omitted:
    goals_before: Optional[List[str]] = Field(
        None, description="Goals at line start (before tactic)"
    )
    goals_after: Optional[List[str]] = Field(
        None, description="Goals at line end (after tactic)"
    )

    # Additional context:
    context: Optional[str] = Field(
        None, description="Local proof context (assumptions, fixes)"
    )


class OutputMessage(BaseModel):
    """Prover output message."""
    kind: str = Field(description="writeln | warning | error | information")
    message: str = Field(description="Message content")


class CommandOutputResult(BaseModel):
    """Command output result."""
    line_context: str = Field(description="Source line")
    messages: List[OutputMessage] = Field(default_factory=list)


class PreviewResult(BaseModel):
    """Document preview result."""
    html: str = Field(description="HTML preview of theory")
    line_context: Optional[str] = Field(None, description="Source line for context")


# ============================================================================
# Session Management Models
# ============================================================================

class BuildResult(BaseModel):
    """Session build result."""
    success: bool = Field(description="True if build succeeded")
    build_log: str = Field(description="Build output")
    session_name: str = Field(description="Session that was built")
    server_info: Optional[Dict[str, Any]] = Field(
        None, description="LSP server info after restart"
    )


class SessionInfo(BaseModel):
    """Current session information."""
    current_session: str = Field(description="Current logic/session name (e.g., HOL)")
    available_sessions: List[str] = Field(description="List of available sessions")


class BuildStatus(BaseModel):
    """Session build status result."""
    success: bool = Field(description="Whether build succeeded")
    messages: List[str] = Field(default_factory=list, description="Build output messages")
    session: str = Field(description="Session name that was built")


# ============================================================================
# Enable forward references
# ============================================================================

HoverInfo.model_rebuild()
