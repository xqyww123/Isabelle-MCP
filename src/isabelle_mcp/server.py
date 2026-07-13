"""Isabelle LSP MCP Server — FastMCP entry point."""

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from isabelle_mcp.evaluation import (
    _evaluation_state_lock,
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
    FindTheoremsResult,
    GoalState,
    HoverInfo,
    LocalOccurrencesResult,
    SessionInfo,
)
from isabelle_mcp.tools import (
    command_output,
    declaration_location,
    find_theorems,
    format_command_output,
    goal,
    hover_info,
    local_occurrences,
    session_info,
)
from isabelle_mcp.unicode_guard import drain_warnings
from isabelle_mcp.utils import IsabelleToolError, MCPLine

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_lsp_client: IsabelleLSPClient | None = None
_file_watcher: FileWatcher | None = None
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
    # Per-agent stdio server: project_root is this process's cwd (each agent launches
    # the server from its project dir), so evaluation snapshots render paths relative
    # to it. The session/logic is chosen at run time via isabelle_launch — the prover
    # is NOT started here.
    _lsp_client = IsabelleLSPClient(
        extra_args=_server_extra_args, project_root=os.path.realpath(os.getcwd()),
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


class UnicodeWarningMiddleware(Middleware):
    """Append queued unicode-conversion warnings to the next tool response.

    The unicode guard (``unicode_guard.sanitize_read``) runs on the push paths
    and queues a warning per affected file; this middleware drains the queue
    after each successful tool call and appends the warning — with the
    instruction to emit Isabelle ASCII — as an extra text block. On a tool
    error the queue is left intact for the next call.
    """

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext,
    ) -> ToolResult:
        result = await call_next(context)
        # Task-augmented calls (SEP-1686) return CreateTaskResult, which has no
        # content list — leave the queue for the next regular call.
        if not isinstance(result, ToolResult):
            return result
        warning = drain_warnings()
        if warning is not None:
            result.content = [
                *result.content, TextContent(type="text", text=warning),
            ]
        return result


mcp.add_middleware(UnicodeWarningMiddleware())


async def _ensure_lsp_started() -> IsabelleLSPClient:
    if _lsp_client is None:
        raise IsabelleToolError("LSP client not initialized")
    if _lsp_client.process is None:
        raise IsabelleToolError(
            "No Isabelle session is running. Call isabelle_launch(session=...) "
            "first to start one (ask the user if the session is unclear).",
        )
    # Backstop sync at every tool-call start: Layer 2 (re-stat open docs and push the
    # changed ones) + Layer 3 (wait out the server's debounce if a dependency just
    # changed). Catches anything the event-driven watcher missed.
    await resync_and_check_freshness(_lsp_client)
    return _lsp_client


def _default_session_dirs() -> list[str]:
    """Default ``-d`` dirs when the agent doesn't pass any: the server's cwd, but
    ONLY if it is a session-root dir (has ROOT/ROOTS). ``isabelle vscode_server``
    rejects a ``-d`` dir lacking ROOT/ROOTS ("Bad session root directory"), so a
    blind ``-d $cwd`` would break for scratch/non-project cwds. Built-in sessions
    (HOL, …) need no ``-d`` at all.
    """
    cwd = os.path.realpath(os.getcwd())
    if os.path.exists(os.path.join(cwd, "ROOT")) or os.path.exists(os.path.join(cwd, "ROOTS")):
        return [cwd]
    return []


# Isabelle reports any prover that dies before the PIDE initialization handshake
# with a fixed sentinel: "Session startup failed / standard_output terminated /
# Return code: 127 (COMMAND NOT FOUND)". That 127 is Process_Result.startup_failure,
# a placeholder that REPLACES the prover's real exit code — it does NOT mean a shell
# command was missing. Detect it so launch can rewrite it into a meaningful error
# rather than leaking the misleading text to the agent.
_STARTUP_FAILURE_MARKERS = ("Session startup failed", "COMMAND NOT FOUND")


def _is_startup_failure(exc: BaseException) -> bool:
    """True when *exc* carries Isabelle's pre-handshake startup-failure sentinel."""
    msg = str(exc)
    return any(m in msg for m in _STARTUP_FAILURE_MARKERS)


def _heap_unbuilt_error(
    client: IsabelleLSPClient, *, require_unfinished: bool
) -> IsabelleToolError | None:
    """Actionable "rebuild the heap" error when the launch-time build probe found the
    session's heap chain missing/outdated; ``None`` when the heap looks fine or the
    verdict does not apply.

    The probe verdict (``isabelle build -n -b``) is the only reliable signal for
    "is this session built": a heap file merely existing is not enough, because
    loading validates the whole ancestor chain's consistency, not just presence.

    ``require_unfinished`` gates the strict form used on the server-startup failure
    path: only substitute this message when the probe positively named unfinished
    (hence *defined*) sessions, so an undefined-session error — which leaves
    ``unfinished_sessions`` empty — keeps its own clear wording instead of being
    mis-reported as a stale heap.
    """
    if client.heap_built is not False:
        return None
    if any(a in ("-R", "-A") for a in client.extra_args):
        # -R/-A run the logic on its *requirements* heaps; the session itself need
        # not be built, so the probe's verdict does not apply.
        logger.warning("heap freshness gate bypassed: -R/-A in extra args")
        return None
    if require_unfinished and not client.unfinished_sessions:
        return None
    names = ", ".join(client.unfinished_sessions) \
        or "some session(s) in the dependency chain"
    return IsabelleToolError(
        f"Heap image(s) cannot be verified as up-to-date "
        f"(outdated, missing, or no build record) for: {names}. "
        f"Rebuild first ({client.build_hint()}) and call "
        f"isabelle_launch again — the MCP server never builds sessions itself."
    )


def _startup_failure_error(
    client: IsabelleLSPClient, orig: BaseException
) -> IsabelleToolError:
    """Rewrite Isabelle's opaque startup-failure sentinel into an actionable error.

    Used when the prover died before initialization but the build probe did not
    pin the cause to a specific unfinished session (so ``_heap_unbuilt_error`` did
    not fire). Names the likely causes instead of leaking "Return code: 127
    (COMMAND NOT FOUND)", which reads like a missing shell command but is not.
    """
    return IsabelleToolError(
        f"Isabelle failed to start the prover for session {client.logic!r}: the "
        f"prover process terminated before initialization. (Isabelle reports this "
        f"as the generic sentinel 'Return code: 127 (COMMAND NOT FOUND)'; it does "
        f"NOT mean a shell command was missing.) The usual cause is a missing, "
        f"outdated, or incompatible heap image somewhere in the session's "
        f"dependency chain; it can also be the prover being killed (e.g. out of "
        f"memory). Verify and rebuild the session heap with "
        f"`{client.build_hint()}`, then relaunch. Underlying prover message: {orig}"
    )


# ── Session management ────────────────────────────────────────────────


@mcp.tool()
async def isabelle_launch(
    session: str = "Main", session_dirs: list[str] | None = None,
) -> SessionInfo:
    """Start (or restart) the Isabelle prover with the given session/logic.

    **Must be called before any evaluation or query tool** — the prover does not
    auto-start. Returns the running session name and server version.

    Calling it again with the same session is a no-op; with a different session it
    restarts the prover (any in-progress evaluation is discarded).

    No need to check whether the session is built — launch checks
    automatically: when its heap image (or any heap in its dependency chain)
    is missing or outdated, or the session name is undefined, it fails fast
    (~5s) instead of building implicitly. Run the `isabelle build -b ...`
    command from the error yourself, then relaunch — this server never builds
    sessions.

    Args:
        session: Isabelle session/logic name, e.g. "HOL-Analysis", "Minilang".
            Pick the one that fits the work (ask the user if unclear) — a
            session only provides precompiled theories; anything else still
            loads, just slowly. Precompiled theories cannot be edited, so the
            session must NOT contain the theories you will work on — for a
            project, use its base session, not the project's own session.
            The "Main" fallback precompiles very little. You need not check
            whether the session is built — launch checks automatically and
            errors with the exact build command if it is not.
        session_dirs: Extra ``-d`` session search directories for non-builtin
            sessions (Isabelle reads their ROOT/ROOTS to discover the session).
            Defaults to the server's working directory when that directory is itself
            a session root (contains ROOT/ROOTS), otherwise none. Built-in sessions
            (HOL, HOL-Analysis, …) need no session dirs.
    """
    if _lsp_client is None:
        raise IsabelleToolError("LSP client not initialized")
    async with _evaluation_state_lock:
        if _lsp_client.process is not None:
            alive = _lsp_client.process.returncode is None
            if alive and _lsp_client.logic == session:
                return await session_info(_lsp_client)
            # Switching sessions — or recovering from a crashed server (the
            # process object lingers with a returncode): tear down, start anew.
            await _lsp_client.shutdown()
            _lsp_client.process = None
        _lsp_client.session_dirs = (
            session_dirs if session_dirs is not None else _default_session_dirs()
        )
        _lsp_client.logic = session
        # Probe the build status and enumerate the heap's source files (for
        # precompiled-theory warnings) in parallel with the server start.
        enum_task = asyncio.create_task(_lsp_client.enumerate_heap_sources())
        try:
            try:
                # Fails fast (~4s) on a missing heap (pre-handshake type-1
                # message surfaced by _surface_server_message) or an undefined
                # session name (JSON-RPC error reply to `initialize`).
                await _lsp_client.start()
            except IsabelleToolError as start_exc:
                # start() failed. Wait for the concurrent build probe so we can
                # replace an opaque prover error with an actionable one. The
                # prover dies before its PIDE handshake for a whole class of
                # reasons (missing/outdated/incompatible heap anywhere in the
                # chain, OOM, ...) and Isabelle collapses them all into the same
                # misleading "Return code: 127 (COMMAND NOT FOUND)". Prefer the
                # precise "rebuild the heap" verdict when the probe positively
                # names unfinished (hence defined) sessions; otherwise, if this
                # is that startup-failure sentinel, rewrite it into a generic
                # startup-failure message. Any other error (e.g. an undefined
                # session) keeps its own clear wording.
                with contextlib.suppress(BaseException):
                    await enum_task
                heap_err = _heap_unbuilt_error(
                    _lsp_client, require_unfinished=True
                )
                if heap_err is not None:
                    raise heap_err from start_exc
                if _is_startup_failure(start_exc):
                    raise _startup_failure_error(_lsp_client, start_exc) \
                        from start_exc
                raise
            # start() succeeded. Raises when the probe itself could not run
            # (fail-closed); then reject a loaded-but-stale/unbuilt heap.
            await enum_task
            heap_err = _heap_unbuilt_error(_lsp_client, require_unfinished=False)
            if heap_err is not None:
                raise heap_err
        except BaseException:
            # Cancellation-safe cleanup: the synchronous part runs first, so
            # even a CancelledError caught here cannot leave a half-started
            # server behind that the next same-session launch would mistake
            # for a healthy one.
            enum_task.cancel()
            _lsp_client.kill()
            with contextlib.suppress(BaseException):
                await enum_task  # retrieve its result/exception (no orphans)
            with contextlib.suppress(BaseException):
                await _lsp_client.reap()
            _lsp_client.process = None
            raise
        return await session_info(_lsp_client)


@mcp.tool(output_schema=None)
async def isabelle_terminate() -> ToolResult:
    """Terminate the running Isabelle prover.

    The MCP server itself stays up; you can start a fresh prover (e.g. a different
    session) with isabelle_launch afterwards.
    """
    if _lsp_client is None or _lsp_client.process is None:
        return ToolResult(content=[TextContent(
            type="text", text="No Isabelle session is running.",
        )])
    async with _evaluation_state_lock:
        await _lsp_client.shutdown()
        _lsp_client.process = None
        if _file_watcher is not None:
            _file_watcher.clear_watches()
    return ToolResult(content=[TextContent(
        type="text", text="Isabelle session terminated.",
    )])


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

    Auto-evaluates the line first if needed; requires a launched session
    (see isabelle_launch).

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

    Auto-evaluates the line first if needed; requires a launched session
    (see isabelle_launch).

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

    Auto-evaluates the line first if needed; requires a launched session
    (see isabelle_launch).

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
    together with the subgoals remaining after that command runs. Auto-evaluates
    the line first if needed; requires a launched session (see isabelle_launch).

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        after_text: Optional text on the line; the command right after it is used.
            Without it, the command at the end of the line is used.
    """
    file_path = os.path.realpath(file_path)
    lsp = await _ensure_lsp_started()
    return await goal(lsp, file_path, MCPLine(line), after_text)


@mcp.tool()
async def isabelle_find_theorems(
    file_path: str,
    line: int,
    after_text: str | None = None,
    names: list[str] | None = None,
    exclude_names: list[str] | None = None,
    intro: bool | None = None,
    elim: bool | None = None,
    dest: bool | None = None,
    solves: bool | None = None,
    patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    simp: list[str] | None = None,
    exclude_simp: list[str] | None = None,
    limit: int | None = None,
    allow_duplicates: bool = False,
) -> FindTheoremsResult:
    """Search the theorem database, like Isabelle's ``find_theorems``.

    The search runs in the proof/theory context at the given position (resolved
    like isabelle_goal: ``line`` + optional ``after_text``). Criteria are combined
    conjunctively; each returns matching theorems as name + statement. Auto-
    evaluates the line first if needed; requires a launched session.

    IMPORTANT — position matters: the goal-relative criteria (``intro``/``elim``/
    ``dest``/``solves`` and bare ``patterns`` that use schematic ``_`` against the
    current goal) only make sense when the caret is INSIDE an open proof. Using
    ``intro``/``elim``/``dest``/``solves`` at a theory-level caret (no goal) is an
    error and is surfaced as such. Name/pattern searches work in any context.

    Args:
        file_path: Absolute path to .thy file.
        line: Line number (1-indexed) — the context to search in.
        after_text: Optional text on the line; the command right after it is the
            context. Without it, the command at the end of the line is used.
        names: Each restricts to facts whose name CONTAINS the string (a substring
            match, with ``*`` as a wildcard — e.g. "add" also matches "padd_0");
            multiple names are AND-ed.
        exclude_names: Like ``names`` but excludes matches (``-name:``).
        intro/elim/dest/solves: Tri-state — True = must be such a rule (or, for
            ``solves``, must solve the current goal); False = must NOT be; None =
            don't care.
        patterns: Term patterns the theorem must match, e.g. "_ + _ = _ + _"
            (ASCII notation; ``_`` is a wildcard). AND-ed.
        exclude_patterns: Like ``patterns`` but excluded.
        simp: Each is a simp-rule LHS pattern the theorem (as a simp rule) must
            match. exclude_simp excludes.
        exclude_simp: Like ``simp`` but excluded.
        limit: Max theorems to return (default ~40, Isabelle's find_theorems_limit).
        allow_duplicates: Keep alpha-equivalent duplicates (default removes them).
    """
    file_path = os.path.realpath(file_path)
    lsp = await _ensure_lsp_started()
    return await find_theorems(
        lsp, file_path, MCPLine(line), after_text,
        names=names, exclude_names=exclude_names,
        intro=intro, elim=elim, dest=dest, solves=solves,
        patterns=patterns, exclude_patterns=exclude_patterns,
        simp=simp, exclude_simp=exclude_simp,
        limit=limit, allow_duplicates=allow_duplicates,
    )


@mcp.tool(output_schema=None)
async def isabelle_command_output(
    file_path: str, line: int, after_text: str | None = None,
) -> ToolResult:
    """Get the Isar command at a position and the output messages it produced.

    Returns the command enclosing the position — its full source text and range —
    together with the prover output it emitted (normal/tracing/warning/error/
    information/state messages). Auto-evaluates the line first if needed; requires
    a launched session (see isabelle_launch).

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
    global _server_extra_args
    import argparse
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "install":
        from isabelle_mcp.install import main as install_main
        raise SystemExit(install_main(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "uninstall":
        from isabelle_mcp.install import uninstall_main
        raise SystemExit(uninstall_main(sys.argv[2:]))

    if "--version" in sys.argv:
        from isabelle_mcp import __version__
        print(f"isabelle-mcp version {__version__}")
        return

    parser = argparse.ArgumentParser(
        description="Isabelle MCP Server (stdio; one dedicated server per agent)",
        usage="%(prog)s [-- ISABELLE_ARGS...]\n"
        "       %(prog)s install [--name NAME] [--isabelle-bin BIN] [--claude] [--codex]\n"
        "       %(prog)s uninstall",
    )

    argv = sys.argv[1:]
    if "--" in argv:
        idx = argv.index("--")
        own_argv, extra = argv[:idx], argv[idx + 1:]
    else:
        own_argv, extra = argv, []
    parser.parse_args(own_argv)  # reject unknown flags
    _server_extra_args = extra
    mcp.run()


if __name__ == "__main__":
    main()
