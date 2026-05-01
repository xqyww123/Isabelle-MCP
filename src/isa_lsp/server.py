"""Isabelle LSP MCP Server — FastMCP entry point."""

import logging
import os
from collections.abc import AsyncGenerator
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_lsp_client: IsabelleLSPClient | None = None


@asynccontextmanager
async def server_lifespan(_app: Any) -> AsyncGenerator[None]:
    global _lsp_client
    logic = os.environ.get("ISABELLE_SESSION", "Main")
    _lsp_client = IsabelleLSPClient(logic=logic)
    try:
        yield
    finally:
        if _lsp_client.process is not None:
            await _lsp_client.shutdown()


mcp = FastMCP("Isabelle LSP", lifespan=server_lifespan)


async def _ensure_lsp_started() -> IsabelleLSPClient:
    if _lsp_client is None:
        raise IsabelleToolError("LSP client not initialized")
    if _lsp_client.process is None:
        await _lsp_client.start()
    return _lsp_client


@mcp.resource("instructions://isabelle-lsp")
async def get_instructions_resource() -> str:
    """Get user-facing instructions for using the Isabelle LSP MCP server."""
    return get_instructions()


@mcp.tool()
async def isabelle_hover(file_path: str, line: int, column: int) -> HoverInfo:
    """Get type and documentation for symbol at position.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        column: Column number (1-indexed)
    """
    return await hover_info(await _ensure_lsp_started(), file_path, line, column)


@mcp.tool()
async def isabelle_completions(
    file_path: str, line: int, column: int, max_completions: int = 50,
) -> CompletionsResult:
    """Get completion suggestions at position.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        column: Column number (1-indexed)
        max_completions: Maximum number of completions to return
    """
    return await completions(await _ensure_lsp_started(), file_path, line, column, max_completions)


@mcp.tool()
async def isabelle_definition(file_path: str, line: int, column: int) -> DeclarationLocation:
    """Find where a symbol is defined.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        column: Column number (1-indexed)
    """
    return await declaration_location(await _ensure_lsp_started(), file_path, line, column)


@mcp.tool()
async def isabelle_highlights(file_path: str, line: int, column: int) -> HighlightsResult:
    """Find all occurrences of symbol in document.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        column: Column number (1-indexed)
    """
    return await document_highlights(await _ensure_lsp_started(), file_path, line, column)


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
    """
    return await diagnostic_messages(
        await _ensure_lsp_started(), file_path, start_line, end_line, interactive
    )


@mcp.tool()
async def isabelle_goal(
    file_path: str, line: int, column: int | None = None,
) -> GoalState:
    """Get proof goals at position. **MOST IMPORTANT tool — use often!**

    Omitting column shows how a tactic transforms the proof state:
    - goals_before: State at line start
    - goals_after: State at line end

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        column: Column number (1-indexed), optional
    """
    return await goal(await _ensure_lsp_started(), file_path, line, column)


@mcp.tool()
async def isabelle_command_output(file_path: str, line: int) -> CommandOutputResult:
    """Get prover output messages for command at line.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
    """
    return await command_output(await _ensure_lsp_started(), file_path, line)


@mcp.tool()
async def isabelle_preview(file_path: str, line: int | None = None) -> PreviewResult:
    """Generate HTML preview of theory content.

    Args:
        file_path: Absolute path to .thy file
        line: Line number for context (1-indexed), optional
    """
    return await preview_document(await _ensure_lsp_started(), file_path, line)


@mcp.tool()
async def isabelle_session_info() -> SessionInfo:
    """Get information about current Isabelle session."""
    return await session_info(await _ensure_lsp_started())


@mcp.tool()
async def isabelle_build(session: str, clean: bool = False) -> BuildStatus:
    """Build an Isabelle session to generate heap images.

    Args:
        session: Session name to build (e.g., 'HOL', 'Main')
        clean: Clean build (remove old heap images)
    """
    return await build_session(await _ensure_lsp_started(), session, clean)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Isabelle LSP MCP Server")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--http", action="store_true", help="Run as HTTP server (shared across clients)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8371)
    args = parser.parse_args()

    if args.version:
        from isa_lsp import __version__
        print(f"isa-lsp version {__version__}")
        return

    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
