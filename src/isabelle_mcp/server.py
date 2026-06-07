"""Isabelle LSP MCP Server — FastMCP entry point."""

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from isabelle_mcp.evaluation import (
    cancel_evaluation,
    evaluate_to,
    evaluation_status,
    format_evaluation_result,
    resync_and_check_freshness,
    sync_file_locked,
)
from isabelle_mcp.file_watcher import FileWatcher
from isabelle_mcp.instructions import get_instructions
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import (
    DeclarationLocation,
    GoalState,
    HoverInfo,
    LocalOccurrencesResult,
    SessionInfo,
)
from isabelle_mcp.tools import (
    command_output,
    declaration_location,
    format_command_output,
    goal,
    hover_info,
    local_occurrences,
    session_info,
)
from isabelle_mcp.utils import IsabelleToolError, MCPLine

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_lsp_client: IsabelleLSPClient | None = None
_file_watcher: FileWatcher | None = None
_server_logic: str = "HOL"
_server_extra_args: list[str] = []


async def _file_change_sink(path: str) -> None:
    """Event-driven sync sink: the FileWatcher schedules this on every relevant edit.

    A no-op until the Isabelle process has been started by a tool call — the prover
    never auto-starts. ``sync_file_locked`` ignores paths that are not editor-opened
    documents (e.g. dependency files, which the server's own File_Watcher syncs).
    """
    client = _lsp_client
    if client is None or client.process is None:
        return
    try:
        await sync_file_locked(client, path)
    except Exception:
        logger.exception("Event-driven file sync failed for %s", path)


@asynccontextmanager
async def server_lifespan(_app: Any) -> AsyncGenerator[None]:
    global _lsp_client, _file_watcher
    # project_root stays None for now (shared HTTP server has no meaningful root) →
    # evaluation snapshots render absolute paths. Set a real per-agent root here with
    # the stdio-per-agent refactor.
    _lsp_client = IsabelleLSPClient(
        logic=_server_logic, extra_args=_server_extra_args, project_root=None,
    )
    _file_watcher = FileWatcher()
    _file_watcher.start()
    # Wire event-driven sync: the watcher (observer thread) schedules _file_change_sink
    # onto this event loop; open_document/close_document add/remove its directory watches.
    _file_watcher.set_sink(asyncio.get_running_loop(), _file_change_sink)
    _lsp_client.file_watcher = _file_watcher
    try:
        yield
    finally:
        _file_watcher.stop()
        if _lsp_client.process is not None:
            await _lsp_client.shutdown()


mcp = FastMCP(
    "Isabelle MCP",
    instructions=get_instructions(),
    lifespan=server_lifespan,
)


async def _ensure_lsp_started() -> IsabelleLSPClient:
    if _lsp_client is None:
        raise IsabelleToolError("LSP client not initialized")
    if _lsp_client.process is None:
        await _lsp_client.start()
    # Backstop sync at every tool-call start: Layer 2 (re-stat open docs and push the
    # changed ones) + Layer 3 (wait out the server's debounce if a dependency just
    # changed). Catches anything the event-driven watcher missed.
    await resync_and_check_freshness(_lsp_client)
    return _lsp_client


# ── Evaluation tools ──────────────────────────────────────────────────


@mcp.tool(output_schema=None)
async def isabelle_evaluate_to(
    file_path: str, line: int, after_text: str | None = None,
) -> ToolResult:
    """Start evaluating a theory file up to a location on a line.

    Returns a per-file snapshot — errors / warnings / running command lines. The
    result may indicate evaluation is still in progress; if so, call
    ``evaluation_status`` to update the progress.

    Args:
        file_path: Absolute path to .thy file
        line: Target line number (1-indexed). Use -1 for last line.
        after_text: Optional text snippet to stop at. Evaluation proceeds through
            the command ending at this snippet. The snippet is matched on token
            boundaries (ASCII and Unicode forms are equivalent), must BEGIN on
            ``line``, and may span onto following lines; its first occurrence is
            used. Without it (default), evaluation proceeds through the command on
            ``line``.
    """
    file_path = os.path.realpath(file_path)
    client = await _ensure_lsp_started()
    view = await evaluate_to(client, file_path, line, after_text)
    return ToolResult(content=[TextContent(
        type="text", text=format_evaluation_result(view, client.project_root),
    )])


@mcp.tool(output_schema=None)
async def isabelle_evaluation_status() -> ToolResult:
    """Check the progress of an ongoing evaluation.

    Returns the current per-file snapshot (errors / warnings / running) and execution
    position.
    """
    client = await _ensure_lsp_started()
    view = await evaluation_status(client)
    return ToolResult(content=[TextContent(
        type="text", text=format_evaluation_result(view, client.project_root),
    )])


@mcp.tool(output_schema=None)
async def isabelle_cancel_evaluation() -> ToolResult:
    """Cancel an ongoing evaluation.

    Stops Isabelle from processing further.  Already-processed results
    remain valid for querying.
    """
    client = await _ensure_lsp_started()
    view = await cancel_evaluation(client)
    return ToolResult(content=[TextContent(
        type="text", text=format_evaluation_result(view, client.project_root),
    )])


# ── Query tools (require prior evaluation) ────────────────────────────


@mcp.tool()
async def isabelle_hover(file_path: str, line: int, symbol: str) -> HoverInfo:
    """Get type and documentation for a symbol on a line.

    Finds all occurrences of the symbol on the line (up to 10), queries each,
    and deduplicates results. Accepts both ASCII and Unicode symbol forms.

    Auto-starts evaluation if the line has not been evaluated yet.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        symbol: Symbol text to look up (e.g. "Suc", "my_const", "⟹")
    """
    return await hover_info(
        await _ensure_lsp_started(), os.path.realpath(file_path), MCPLine(line), symbol,
    )


@mcp.tool()
async def isabelle_definition(file_path: str, line: int, symbol: str) -> DeclarationLocation:
    """Find where a symbol is defined.

    Finds all occurrences of the symbol on the line (up to 10), queries each,
    and deduplicates locations. Accepts both ASCII and Unicode symbol forms.

    Auto-starts evaluation if the line has not been evaluated yet.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        symbol: Symbol text to look up (e.g. "my_const", "List.map")
    """
    return await declaration_location(
        await _ensure_lsp_started(), os.path.realpath(file_path), MCPLine(line), symbol,
    )


@mcp.tool()
async def isabelle_local_occurrences(file_path: str, line: int, symbol: str) -> LocalOccurrencesResult:
    """Find every occurrence of a *locally-defined* entity within this file.

    Given a symbol on a line, resolves the entity there and returns all places it
    appears in the SAME file — its definition site and its uses. Useful to see
    where a constant, abbreviation, or lemma defined in this theory is used.

    Scope is the current file only, and only entities defined in this file resolve:
    references to global constants from imported theories, and plain free/bound
    variables, return no occurrences.

    Auto-starts evaluation if the line has not been evaluated yet.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        symbol: Symbol text to look up (e.g. "my_const", "add_one"), ASCII or Unicode.
    """
    return await local_occurrences(
        await _ensure_lsp_started(), os.path.realpath(file_path), MCPLine(line), symbol,
    )


@mcp.tool()
async def isabelle_goal(
    file_path: str, line: int, after_text: str | None = None,
) -> GoalState:
    """Get the Isar command at a position and the proof state after it executes.

    Returns the command enclosing the position — its full source text and range —
    together with the subgoals remaining after that command runs. Auto-starts
    evaluation if the line has not been evaluated yet.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        after_text: Optional text on the line; the command right after it is used.
            Without it, the command at the end of the line is used.
    """
    file_path = os.path.realpath(file_path)
    lsp = await _ensure_lsp_started()
    return await goal(lsp, file_path, MCPLine(line), after_text)


@mcp.tool(output_schema=None)
async def isabelle_command_output(
    file_path: str, line: int, after_text: str | None = None,
) -> ToolResult:
    """Get the Isar command at a position and the output messages it produced.

    Returns the command enclosing the position — its full source text and range —
    together with the prover output it emitted (normal/tracing/warning/error/
    information/state messages). Auto-starts evaluation if the line has not been
    evaluated yet.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        after_text: Optional text on the line; the command right after it is used.
            Without it, the command at the end of the line is used.
    """
    result = await command_output(
        await _ensure_lsp_started(), os.path.realpath(file_path), MCPLine(line), after_text,
    )
    return ToolResult(
        content=[TextContent(type="text", text=format_command_output(result, line))],
    )


@mcp.tool()
async def isabelle_session_info() -> SessionInfo:
    """Get information about current Isabelle session."""
    return await session_info(await _ensure_lsp_started())


def main() -> None:
    global _server_logic, _server_extra_args
    import argparse
    import sys

    if "--version" in sys.argv:
        from isabelle_mcp import __version__
        print(f"isabelle-mcp version {__version__}")
        return

    parser = argparse.ArgumentParser(
        description="Isabelle MCP Server",
        usage="%(prog)s -s SESSION [options] [-- ISABELLE_ARGS...]",
    )
    parser.add_argument(
        "-s", "--session", required=True,
        help="Isabelle session/logic name (e.g. HOL, HOL-Analysis)",
    )
    parser.add_argument("--http", action="store_true", help="Run as HTTP server (shared across clients)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8371)

    argv = sys.argv[1:]
    if "--" in argv:
        idx = argv.index("--")
        own_argv, extra = argv[:idx], argv[idx + 1:]
    else:
        own_argv, extra = argv, []
    args = parser.parse_args(own_argv)

    _server_logic = args.session
    _server_extra_args = extra

    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
