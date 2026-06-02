"""Isabelle LSP MCP Server — FastMCP entry point."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from isabelle_mcp.evaluation import (
    cancel_evaluation,
    evaluate_to,
    evaluation_status,
)
from isabelle_mcp.file_watcher import FileWatcher
from isabelle_mcp.instructions import get_instructions
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import (
    CommandOutputResult,
    DeclarationLocation,
    DiagnosticsResult,
    EvaluationResult,
    GoalState,
    HoverInfo,
    LocalOccurrencesResult,
    SessionInfo,
)
from isabelle_mcp.tools import (
    command_output,
    declaration_location,
    diagnostic_messages,
    goal,
    hover_info,
    local_occurrences,
    session_info,
)
from isabelle_mcp.utils import IsabelleToolError, MCPColumn, MCPLine  # MCPColumn still used by other tools

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_lsp_client: IsabelleLSPClient | None = None
_file_watcher: FileWatcher | None = None
_server_logic: str = "HOL"
_server_extra_args: list[str] = []


@asynccontextmanager
async def server_lifespan(_app: Any) -> AsyncGenerator[None]:
    global _lsp_client, _file_watcher
    _lsp_client = IsabelleLSPClient(logic=_server_logic, extra_args=_server_extra_args)
    _file_watcher = FileWatcher()
    _file_watcher.start()
    try:
        yield
    finally:
        _file_watcher.stop()
        if _lsp_client.process is not None:
            await _lsp_client.shutdown()


mcp = FastMCP("Isabelle MCP", lifespan=server_lifespan)


async def _ensure_lsp_started() -> IsabelleLSPClient:
    if _lsp_client is None:
        raise IsabelleToolError("LSP client not initialized")
    if _lsp_client.process is None:
        await _lsp_client.start()
    if _file_watcher is not None:
        dirty = _file_watcher.pop_dirty_files()
        if dirty:
            await _lsp_client.sync_dirty_files(dirty)
    return _lsp_client


@mcp.custom_route("/notify-file-change", methods=["POST"])
async def notify_file_change(request: Request) -> JSONResponse:
    data = await request.json()
    file_path = data.get("file_path", "")
    if file_path and _file_watcher is not None:
        _file_watcher.notify_file_changed(file_path)
        logger.info("Hook notified file change: %s", file_path)
    return JSONResponse({"ok": True})


@mcp.resource("instructions://isabelle-mcp")
async def get_instructions_resource() -> str:
    """Get user-facing instructions for using the Isabelle MCP server."""
    return get_instructions()


# ── Evaluation tools ──────────────────────────────────────────────────


@mcp.tool()
async def isabelle_evaluate_to(file_path: str, line: int, column: int = 0) -> EvaluationResult:
    """Start evaluating a theory file up to a specific location.

    The result may indicate evaluation is still in progress.
    If so, call ``evaluation_status`` to update the progress.

    Args:
        file_path: Absolute path to .thy file
        line: Target line number (1-indexed). Use -1 for last line.
        column: Target column (1-indexed). 0 (default) means start of line. -1 means end of line.
    """
    return await evaluate_to(await _ensure_lsp_started(), file_path, line, column)


@mcp.tool()
async def isabelle_evaluation_status() -> EvaluationResult:
    """Check the progress of an ongoing evaluation.
    Returns new errors (since the last check) and the current execution position.
    """
    return await evaluation_status(await _ensure_lsp_started())


@mcp.tool()
async def isabelle_cancel_evaluation() -> EvaluationResult:
    """Cancel an ongoing evaluation.

    Stops Isabelle from processing further.  Already-processed results
    remain valid for querying.
    """
    return await cancel_evaluation(await _ensure_lsp_started())


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
    return await hover_info(await _ensure_lsp_started(), file_path, MCPLine(line), symbol)


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
    return await declaration_location(await _ensure_lsp_started(), file_path, MCPLine(line), symbol)


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
    return await local_occurrences(await _ensure_lsp_started(), file_path, MCPLine(line), symbol)


@mcp.tool()
async def isabelle_diagnostics(
    file_path: str,
    start_line: int,
    end_line: int,
) -> DiagnosticsResult:
    """Get compiler diagnostics (errors, warnings) for a line range.

    Auto-starts evaluation if the range has not been evaluated yet.
    Use negative indices to count from the end: -1 = last line.

    Args:
        file_path: Absolute path to .thy file
        start_line: Start line (1-indexed, or negative from end)
        end_line: End line (1-indexed, or negative from end)
    """
    return await diagnostic_messages(
        await _ensure_lsp_started(), file_path, start_line, end_line
    )


@mcp.tool()
async def isabelle_goal(
    file_path: str, line: int, column: int | None = None,
) -> GoalState:
    """Get proof goals at position. **MOST IMPORTANT tool — use often!**

    Auto-starts evaluation if the line has not been evaluated yet.

    Omitting column shows how a tactic transforms the proof state:
    - goals_before: State at line start
    - goals_after: State at line end

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        column: Column number (1-indexed), optional
    """
    lsp = await _ensure_lsp_started()
    return await goal(
        lsp, file_path, MCPLine(line),
        MCPColumn(column) if column is not None else None,
    )


@mcp.tool()
async def isabelle_command_output(file_path: str, line: int) -> CommandOutputResult:
    """Get prover output messages for command at line.

    Auto-starts evaluation if the line has not been evaluated yet.

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
    """
    return await command_output(await _ensure_lsp_started(), file_path, MCPLine(line))


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
