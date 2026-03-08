"""
Isabelle LSP MCP tools.

This package exports all available MCP tools for Isabelle interaction.
"""

from isa_lsp.tools.hover import hover_info
from isa_lsp.tools.completions import completions
from isa_lsp.tools.definition import declaration_location
from isa_lsp.tools.highlights import document_highlights
from isa_lsp.tools.diagnostics import diagnostic_messages
from isa_lsp.tools.goal import goal
from isa_lsp.tools.command_output import command_output
from isa_lsp.tools.preview import preview_document
from isa_lsp.tools.session import session_info, build_session

__all__ = [
    # Standard LSP tools
    "hover_info",
    "completions",
    "declaration_location",
    "document_highlights",
    "diagnostic_messages",
    # PIDE extension tools
    "goal",
    "command_output",
    "preview_document",
    # Session management tools
    "session_info",
    "build_session",
]
