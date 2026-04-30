"""
Isabelle LSP MCP Server.

This module implements the FastMCP server that provides Isabelle LSP tools
to AI agents via the Model Context Protocol.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from isa_lsp.instructions import get_instructions
from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import (
    BuildStatus,
    CommandOutputResult,
    CompletionsResult,
    DeclarationLocation,
    DiagnosticsResult,
    GoalState,
    HighlightsResult,
    HoverInfo,
    PreviewResult,
    SessionInfo,
)
from isa_lsp.tools import (
    build_session,
    command_output,
    completions,
    declaration_location,
    diagnostic_messages,
    document_highlights,
    goal,
    hover_info,
    preview_document,
    session_info,
)
from isa_lsp.utils import IsabelleToolError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global LSP client instance
_lsp_client: IsabelleLSPClient | None = None


@asynccontextmanager
async def server_lifespan(app: Any) -> AsyncIterator[None]:
    """Manage LSP client lifecycle.

    Args:
        app: FastMCP application instance (required by FastMCP lifespan protocol)
    """
    global _lsp_client

    # Default session (can be overridden via environment variable)
    import os
    logic = os.environ.get("ISABELLE_SESSION", "HOL")

    logger.info(f"Creating Isabelle LSP client with session: {logic}")

    # Create LSP client (but don't start it yet - lazy initialization)
    _lsp_client = IsabelleLSPClient(logic=logic)

    logger.info("Isabelle LSP client ready for lazy initialization")

    try:
        yield
    finally:
        # Cleanup on shutdown (only if started)
        if _lsp_client.process is not None:
            logger.info("Shutting down Isabelle LSP client")
            await _lsp_client.shutdown()
            logger.info("Isabelle LSP client shut down successfully")
        else:
            logger.info("Isabelle LSP client was never started, no cleanup needed")


# Create FastMCP server
mcp = FastMCP("Isabelle LSP", lifespan=server_lifespan)


# ============================================================================
# Helper Functions
# ============================================================================

async def _ensure_lsp_started() -> IsabelleLSPClient:
    """Ensure LSP client is started (lazy initialization)."""
    global _lsp_client

    if _lsp_client is None:
        raise IsabelleToolError("LSP client not initialized")

    # Start LSP client if not already started
    if _lsp_client.process is None:
        logger.info("Starting Isabelle LSP client (lazy initialization)")
        await _lsp_client.start()
        logger.info("Isabelle LSP client started successfully")

    return _lsp_client


# ============================================================================
# Resources
# ============================================================================

@mcp.resource("instructions://isabelle-lsp")
async def get_instructions_resource() -> str:
    """Get user-facing instructions for using the Isabelle LSP MCP server."""
    return get_instructions()


# ============================================================================
# Standard LSP Tools
# ============================================================================

@mcp.tool()
async def isabelle_hover(
    file_path: str,
    line: int,
    column: int,
) -> HoverInfo:
    """Get type and documentation for symbol at position.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        column: Column number (1-indexed)

    Returns:
        HoverInfo with symbol information
    """
    client = await _ensure_lsp_started()
    return await hover_info(client, file_path, line, column)


@mcp.tool()
async def isabelle_completions(
    file_path: str,
    line: int,
    column: int,
    max_completions: int = 50,
) -> CompletionsResult:
    """Get completion suggestions at position.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        column: Column number (1-indexed)
        max_completions: Maximum number of completions to return

    Returns:
        CompletionsResult with sorted completion items
    """
    client = await _ensure_lsp_started()

    return await completions(client, file_path, line, column, max_completions)


@mcp.tool()
async def isabelle_definition(
    file_path: str,
    line: int,
    column: int,
) -> DeclarationLocation:
    """Find where a symbol is defined.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        column: Column number (1-indexed)

    Returns:
        DeclarationLocation with symbol and definition locations
    """
    client = await _ensure_lsp_started()

    return await declaration_location(client, file_path, line, column)


@mcp.tool()
async def isabelle_highlights(
    file_path: str,
    line: int,
    column: int,
) -> HighlightsResult:
    """Find all occurrences of symbol in document.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        column: Column number (1-indexed)

    Returns:
        HighlightsResult with symbol and highlight locations
    """
    client = await _ensure_lsp_started()

    return await document_highlights(client, file_path, line, column)


@mcp.tool()
async def isabelle_diagnostics(
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    interactive: bool = False,
) -> DiagnosticsResult:
    """Get compiler diagnostics (errors, warnings) for file.

    Args:
        file_path: Absolute path to .thy file
        start_line: Filter diagnostics from this line (1-indexed), optional
        end_line: Filter diagnostics to this line (1-indexed), optional
        interactive: Return verbose PIDE markup (not implemented in MVP)

    Returns:
        DiagnosticsResult with diagnostics and status
    """
    client = await _ensure_lsp_started()
    return await diagnostic_messages(
        client, file_path, start_line, end_line, interactive
    )


# ============================================================================
# PIDE Extension Tools
# ============================================================================

@mcp.tool()
async def isabelle_goal(
    file_path: str,
    line: int,
    column: int | None = None,
) -> GoalState:
    """Get proof goals at position. **MOST IMPORTANT tool - use often!**

    Omitting column shows how a tactic transforms the proof state:
    - goals_before: State at line start
    - goals_after: State at line end

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        column: Column number (1-indexed), optional

    Returns:
        GoalState with goals and context
    """
    client = await _ensure_lsp_started()

    return await goal(client, file_path, line, column)


@mcp.tool()
async def isabelle_command_output(
    file_path: str,
    line: int,
) -> CommandOutputResult:
    """Get prover output messages for command at line.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)

    Returns:
        CommandOutputResult with messages
    """
    client = await _ensure_lsp_started()

    return await command_output(client, file_path, line)


@mcp.tool()
async def isabelle_preview(
    file_path: str,
    line: int | None = None,
) -> PreviewResult:
    """Generate HTML preview of theory content.

    Args:
        file_path: Absolute path to .thy file
        line: Line number for context (1-indexed), optional

    Returns:
        PreviewResult with HTML content
    """
    client = await _ensure_lsp_started()

    return await preview_document(client, file_path, line)


# ============================================================================
# Session Management Tools
# ============================================================================

@mcp.tool()
async def isabelle_session_info() -> SessionInfo:
    """Get information about current Isabelle session.

    Returns:
        SessionInfo with current session and available sessions
    """
    client = await _ensure_lsp_started()
    return await session_info(client)


@mcp.tool()
async def isabelle_build(
    session: str,
    clean: bool = False,
) -> BuildStatus:
    """Build an Isabelle session to generate heap images.

    Args:
        session: Session name to build (e.g., 'HOL', 'Main')
        clean: Clean build (remove old heap images)

    Returns:
        BuildStatus with success flag and build messages
    """
    client = await _ensure_lsp_started()

    return await build_session(client, session, clean)


# ============================================================================
# Server entry point
# ============================================================================

def main() -> None:
    """Run the MCP server."""
    import sys

    # Check if running in MCP mode or standalone
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        from isa_lsp import __version__
        print(f"isa-lsp version {__version__}")
        return

    # Run FastMCP server
    logger.info("Starting Isabelle LSP MCP server")
    mcp.run()


if __name__ == "__main__":
    main()
