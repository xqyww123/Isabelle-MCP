"""Isabelle LSP MCP Server — FastMCP entry point."""

import anyio
import asyncio
import contextlib
import contextvars
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from pydantic import BaseModel

from isabelle_mcp import debugger
from isabelle_mcp.evaluation import (
    _evaluation_state_lock,
    cancel_evaluation,
    evaluate_to,
    evaluation_footer,
    evaluation_status,
    format_evaluation_result,
    resync_and_check_freshness,
    sync_file_locked,
)
from isabelle_mcp.file_watcher import FileWatcher
from isabelle_mcp.instructions import get_instructions
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.models import BreakpointRef, LinePosition
from isabelle_mcp.tools import (
    command_output,
    command_status,
    declaration_location,
    find_theorems,
    format_command_output,
    format_command_status,
    goal,
    hover_info,
    local_occurrences,
    session_info,
)
from isabelle_mcp.unicode_guard import drain_warnings
from isabelle_mcp.utils import (
    CATASTROPHE_MESSAGE,
    IsabelleCatastrophe,
    IsabelleToolError,
    MCPLine,
)
from isabelle_mcp.utils.formatters import model_to_yaml

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

_lsp_client: IsabelleLSPClient | None = None
_file_watcher: FileWatcher | None = None
_server_extra_args: list[str] = []

# isabelle_launch's three replies (D-B19, approved verbatim): the reply itself
# distinguishes a fresh start, a no-op and a restart, so the tool description
# does not have to.
LAUNCH_STARTED = "Started Isabelle session '{session}' ({version}, debug {debug})."
LAUNCH_NOOP = (
    "Isabelle session '{session}' is already running ({version}, "
    "debug {debug}). Nothing changed. Terminate the session first if you "
    "want to restart."
)
LAUNCH_RESTARTED = (
    "Restarted the Isabelle prover: session '{session}' ({version}, "
    "debug {debug}) replaces '{old_session}' (debug {old_debug}). "
    "Any evaluation that was in progress is discarded."
)


def _onoff(debug: bool) -> str:
    return "on" if debug else "off"


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
            await _lsp_client.teardown("The MCP server is shutting down.")


mcp = FastMCP(
    "Isabelle MCP",
    instructions=get_instructions(),
    lifespan=server_lifespan,
)


# The evaluation footer of the call in flight, computed in _ensure_lsp_started
# and appended by the middleware once the tool has produced its result. A
# ContextVar, not a module global: tool calls can overlap, and a footer belongs
# to the call that computed it. Set only by the tools that display it.
_pending_footer: contextvars.ContextVar[str] = contextvars.ContextVar(
    "isabelle_mcp_pending_footer", default="",
)


def _append_block(result: ToolResult, text: str) -> None:
    """Append one extra text block, led by a blank line: clients that join
    a result's blocks without a separator (Claude Code does) still get a
    paragraph break between the tool's own output and the extra block."""
    result.content = [
        *result.content, TextContent(type="text", text="\n\n" + text),
    ]


class UnicodeWarningMiddleware(Middleware):
    """Append queued debugger notices, unicode-conversion warnings and the
    evaluation footer to the tool response.

    The unicode guard (``unicode_guard.sanitize_read``) runs on the push paths
    and queues a warning per affected file; this middleware drains the queue
    after each successful tool call and appends the warning — with the
    instruction to emit Isabelle ASCII — as an extra text block. On a tool
    error both queues are left intact for the next call.

    Debugger notices (design section 6.3) ride the same mechanism and go
    first: they are result-adjacent one-liners about asynchronous debugger
    events, delivered exactly once, on the next tool call of any kind — so
    the hit bookkeeping is synced here, not only in the debugger tools.

    The footer goes last, below the warning: it is a fixed-format line the agent
    learns to skim, and burying a rare, actionable warning underneath a constant
    one would be worse than the reverse.
    """

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext,
    ) -> ToolResult:
        _pending_footer.set("")
        result = await call_next(context)
        # Task-augmented calls (SEP-1686) return CreateTaskResult, which has no
        # content list — leave the queue for the next regular call.
        if not isinstance(result, ToolResult):
            return result
        client = _lsp_client
        if client is not None and client.process is not None and client.debug:
            debugger.registry.sync_hits(client)
            try:
                await debugger.reconcile_dirty(client)
            except Exception:
                # Reconciliation must never fail a tool call that
                # succeeded. reconcile_dirty re-marks whatever it had not
                # yet verified, so the next pass retries exactly that.
                logger.exception("breakpoint reconciliation failed")
        notices = debugger.registry.drain_notices()
        if notices is not None:
            _append_block(result, notices)
        warning = drain_warnings()
        if warning is not None:
            _append_block(result, warning)
        footer = _pending_footer.get()
        if footer:
            _append_block(result, footer)
        return result


class CatastropheMiddleware(Middleware):
    """The one handler for IsabelleCatastrophe, at the tool boundary.

    Any tool may raise it from anywhere (a cancellation out of budget, a prover
    that stopped answering, a broken invariant). Here, and only here: log the
    reason, tear the prover down through the same helper isabelle_terminate
    uses -- under the evaluation-state lock, shielded from a cancelled tool
    call, bounded by the teardown's own segments -- and answer with the one
    fixed sentence. The agent must launch again.
    """

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext,
    ) -> ToolResult:
        try:
            return await call_next(context)
        except IsabelleCatastrophe as exc:
            logger.error("catastrophe: %s; terminating the Isabelle session",
                         exc.reason, exc_info=True)
            client = _lsp_client
            if client is not None:
                with anyio.CancelScope(shield=True):
                    async with _evaluation_state_lock:
                        await client.teardown(
                            "The Isabelle session was terminated after an internal failure.")
            return ToolResult(content=[TextContent(type="text", text=CATASTROPHE_MESSAGE)])


# Order: the catastrophe handler is outermost, so a catastrophe skips the
# footer/notice decoration of a successful call.
mcp.add_middleware(CatastropheMiddleware())
mcp.add_middleware(UnicodeWarningMiddleware())


def _yaml_result(model: BaseModel) -> ToolResult:
    """The MCP boundary for model-shaped results: one YAML text block.

    The tool functions keep returning their models internally (unit tests
    assert on fields); only the presentation is text — same convention as the
    narrative tools, so agents read one format everywhere.
    """
    return ToolResult(
        content=[TextContent(type="text", text=model_to_yaml(model))],
    )


async def _ensure_lsp_started(
    *, footer: bool = False, cancel_tool: bool = False,
) -> IsabelleLSPClient:
    if _lsp_client is None:
        raise IsabelleToolError("LSP client not initialized")
    if _lsp_client.process is None:
        raise IsabelleToolError(
            "No Isabelle session is running. Call isabelle_launch(session=...) "
            "first to start one (ask the user if the session is unclear).",
        )
    # The tool entry: the open-document sync, one flush request (the server
    # re-reads every dependency file and names an assigned version), this
    # call's entry record, the unified close. The cancel tool skips the flush
    # and the close (see resync_and_check_freshness).
    await resync_and_check_freshness(_lsp_client, cancel_tool=cancel_tool)
    if footer:
        # Only the tools that DISPLAY the footer compute it. The computation can
        # end an evaluation (see evaluation_footer), and a tool that does not show
        # the result has no business making that transition on its own path —
        # isabelle_evaluation_status would then answer "No evaluation in
        # progress." instead of reporting the completion it just observed.
        #
        # This also fixes the footer's place in the order: it runs before the
        # guard, so a completion it observes is stamped before the guard reads
        # the evaluation state — the guard's wait-on-this-run and busy-with-
        # another-file branches must never judge against a run that is over.
        _pending_footer.set(await evaluation_footer(_lsp_client))
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
        or "some sessions in the dependency chain"
    return IsabelleToolError(
        f"Heap images cannot be verified as up-to-date "
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


@mcp.tool(output_schema=None)
async def isabelle_launch(
    session: str = "Main", session_dirs: list[str] | None = None,
    debug: bool = False,
) -> ToolResult:
    """Start (or restart) the Isabelle prover with the given session/logic.

    **Must be called before any evaluation or query tool** — the prover does not
    auto-start.

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
            project, use its base session (the parent in its ROOT entry,
            `session NAME = BASE + …`), not the project's own session.
            The "Main" fallback precompiles very little. You need not check
            whether the session is built — launch checks automatically and
            errors with the exact build command if it is not.
        session_dirs: Extra ``-d`` session search directories for non-builtin
            sessions (Isabelle reads their ROOT/ROOTS to discover the session).
            Defaults to the server's working directory when that directory is itself
            a session root (contains ROOT/ROOTS), otherwise none. Built-in sessions
            (HOL, HOL-Analysis, …) need no session dirs.
        debug: Launch with ML debugger instrumentation (`-o ML_debugger=true`):
            newly compiled ML code gets breakable sites; code precompiled into
            the heap is unaffected, and no heap is invalidated. Off by default
            because instrumentation slows compiled ML.
    """
    if _lsp_client is None:
        raise IsabelleToolError("LSP client not initialized")
    async with _evaluation_state_lock:
        replaced: tuple[str, bool] | None = None
        if _lsp_client.process is not None:
            alive = _lsp_client.process.returncode is None
            if alive and _lsp_client.logic == session and _lsp_client.debug == debug:
                return ToolResult(content=[TextContent(type="text", text=(
                    LAUNCH_NOOP.format(
                        session=session,
                        version=_lsp_client.isabelle_version,
                        debug=_onoff(debug),
                    )))])
            if alive:
                # Any identity change (session or debug) restarts the prover.
                # The one asset a restart silently destroys is a live
                # breakpoint hit — refuse then, with the same refusal
                # evaluate_to uses (section 6B).
                refusal = debugger.hits_live_refusal(_lsp_client)
                if refusal is not None:
                    raise IsabelleToolError(refusal)
                replaced = (_lsp_client.logic, _lsp_client.debug)
            # Tear down the survivor — or a crashed server (the process
            # object lingers with a returncode): start anew. The same teardown
            # isabelle_terminate and the catastrophe handler run.
            await _lsp_client.teardown()
        _lsp_client.session_dirs = (
            session_dirs if session_dirs is not None else _default_session_dirs()
        )
        _lsp_client.logic = session
        _lsp_client.debug = debug
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
        if replaced is not None:
            text = LAUNCH_RESTARTED.format(
                session=session, version=_lsp_client.isabelle_version,
                debug=_onoff(debug), old_session=replaced[0],
                old_debug=_onoff(replaced[1]),
            )
        else:
            text = LAUNCH_STARTED.format(
                session=session, version=_lsp_client.isabelle_version,
                debug=_onoff(debug),
            )
        return ToolResult(content=[TextContent(type="text", text=text)])


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
        # the same teardown the catastrophe handler runs
        # (client.file_watcher is the module's _file_watcher, server startup)
        await _lsp_client.teardown()
    return ToolResult(content=[TextContent(
        type="text", text="Isabelle session terminated.",
    )])


# ── Evaluation tools ──────────────────────────────────────────────────


@mcp.tool(output_schema=None)
async def isabelle_evaluate_to(
    file_path: str, line: int, after_text: str | None = None,
) -> ToolResult:
    """Start evaluating a theory file up to a location on a line.

    **Errors do not stop the checking — they slow it down.** Isabelle still checks
    every command up to your target when earlier ones fail. But many errors can
    make the evaluation crawl: watch it using isabelle_evaluation_status, and when
    errors pile up, cancel it with isabelle_cancel_evaluation.

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
    """Check the current evaluation state."""
    client = await _ensure_lsp_started()
    view = await evaluation_status(client)
    # No "call isabelle_evaluation_status" here: this IS that tool.
    text = format_evaluation_result(
        view, client.project_root, call_to_action=False,
    )
    # The paused-at-a-breakpoint section leads whenever hits are live
    # (section 6.1): polling must never misread a stop as a hang.
    paused = debugger.paused_section(client)
    if paused is not None:
        text = paused + "\n\n" + text
    return ToolResult(content=[TextContent(type="text", text=text)])


@mcp.tool(output_schema=None)
async def isabelle_cancel_evaluation() -> ToolResult:
    """Cancel an ongoing evaluation.

    Stops Isabelle from processing further.  Results before the first
    unfinished command remain valid for querying.  Other tool calls wait
    while a cancellation is in progress.
    """
    client = await _ensure_lsp_started(cancel_tool=True)
    view = await cancel_evaluation(client)
    return ToolResult(content=[TextContent(
        type="text", text=format_evaluation_result(view, client.project_root),
    )])


# ── Query tools (require prior evaluation) ────────────────────────────


@mcp.tool(output_schema=None)
async def isabelle_hover(file_path: str, line: int, symbol: str) -> ToolResult:
    """Get type and documentation for a symbol on a line.

    Finds all occurrences of the symbol on the line (up to 10), queries each,
    and deduplicates results. Accepts both ASCII and Unicode symbol forms.

    Queries never evaluate: evaluate up to the line with isabelle_evaluate_to
    first. Requires a launched session (see isabelle_launch).

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        symbol: Symbol text to look up (e.g. "Suc", "my_const", "⟹")
    """
    return _yaml_result(await hover_info(
        await _ensure_lsp_started(footer=True), os.path.realpath(file_path),
        MCPLine(line), symbol,
    ))


@mcp.tool(output_schema=None)
async def isabelle_definition(file_path: str, line: int, symbol: str) -> ToolResult:
    """Find where a symbol is defined.

    Finds all occurrences of the symbol on the line (up to 10), queries each,
    and deduplicates locations. Accepts both ASCII and Unicode symbol forms.

    Queries never evaluate: evaluate up to the line with isabelle_evaluate_to
    first. Requires a launched session (see isabelle_launch).

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        symbol: Symbol text to look up (e.g. "my_const", "List.map")
    """
    return _yaml_result(await declaration_location(
        await _ensure_lsp_started(footer=True), os.path.realpath(file_path),
        MCPLine(line), symbol,
    ))


@mcp.tool(output_schema=None)
async def isabelle_local_occurrences(file_path: str, line: int, symbol: str) -> ToolResult:
    """Find every occurrence of a *locally-defined* entity within this file.

    Given a symbol on a line, resolves the entity there and returns all places it
    appears in the SAME file — its definition site and its uses. Useful to see
    where a constant, abbreviation, or lemma defined in this theory is used.

    Scope is the current file only, and only entities defined in this file resolve:
    references to global constants from imported theories, and plain free/bound
    variables, return no occurrences.

    Queries never evaluate: evaluate up to the line with isabelle_evaluate_to
    first. Requires a launched session (see isabelle_launch).

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        symbol: Symbol text to look up (e.g. "my_const", "add_one"), ASCII or Unicode.
    """
    return _yaml_result(await local_occurrences(
        await _ensure_lsp_started(footer=True), os.path.realpath(file_path),
        MCPLine(line), symbol,
    ))


@mcp.tool(output_schema=None)
async def isabelle_goal(
    file_path: str, line: int, after_text: str | None = None,
) -> ToolResult:
    """Get the Isar command at a position and the proof state after it executes.

    Returns the command enclosing the position — its full source text and range —
    together with the subgoals remaining after that command runs. Queries never
    evaluate: evaluate up to the line with isabelle_evaluate_to first. Requires
    a launched session (see isabelle_launch).

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        after_text: Optional text on the line; the command right after it is used.
            Without it, the command at the end of the line is used.
    """
    file_path = os.path.realpath(file_path)
    lsp = await _ensure_lsp_started(footer=True)
    return _yaml_result(await goal(lsp, file_path, MCPLine(line), after_text))


@mcp.tool(output_schema=None)
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
) -> ToolResult:
    """Search the theorem database, like Isabelle's ``find_theorems``.

    The search runs in the proof/theory context at the given position (resolved
    like isabelle_goal: ``line`` + optional ``after_text``). Criteria are combined
    conjunctively; each returns matching theorems as name + statement. Queries
    never evaluate: evaluate up to the line with isabelle_evaluate_to first.
    Requires a launched session.

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
    lsp = await _ensure_lsp_started(footer=True)
    return _yaml_result(await find_theorems(
        lsp, file_path, MCPLine(line), after_text,
        names=names, exclude_names=exclude_names,
        intro=intro, elim=elim, dest=dest, solves=solves,
        patterns=patterns, exclude_patterns=exclude_patterns,
        simp=simp, exclude_simp=exclude_simp,
        limit=limit, allow_duplicates=allow_duplicates,
    ))


@mcp.tool(output_schema=None)
async def isabelle_command_output(
    file_path: str, line: int, after_text: str | None = None,
) -> ToolResult:
    """Get the Isar command at a position and the output messages it produced.

    Returns the command enclosing the position — its full source text and range —
    together with the prover output it emitted (normal/tracing/warning/error/
    information/state messages). Queries never evaluate: evaluate up to the line
    with isabelle_evaluate_to first. Requires a launched session
    (see isabelle_launch).

    Args:
        file_path: Absolute path to .thy file
        line: Line number (1-indexed)
        after_text: Optional text on the line; the command right after it is used.
            Without it, the command at the end of the line is used.
    """
    result = await command_output(
        await _ensure_lsp_started(footer=True), os.path.realpath(file_path),
        MCPLine(line), after_text,
    )
    return ToolResult(
        content=[TextContent(type="text", text=format_command_output(result, line))],
    )


@mcp.tool(output_schema=None)
async def isabelle_command_status(positions: list[LinePosition]) -> ToolResult:
    """Ask what state the command(s) covering each of several lines are in.

    Answers one line per requested position, in the order asked, with one of:
    `processed`, `running for Ns`, `not evaluated`,
    `cancelled, re-evaluate to get a result`, `unknown, retry in a few seconds`,
    `no command`, `file not open`.

    A line may hold several commands — `lemma foo: "P" by auto` is two. When they
    agree the shared state is reported; only when they differ is a per-command
    breakdown printed. A command spanning several lines is reported for every line
    it covers, so asking about a line in the middle of a proof reports that proof's
    command.

    Args:
        positions: The positions to ask about, each a file_path and a 1-indexed line.
    """
    client = await _ensure_lsp_started(footer=True)
    result = await command_status(client, positions)
    return ToolResult(
        content=[
            TextContent(
                type="text", text=format_command_status(result, client.project_root),
            ),
        ],
    )


@mcp.tool(output_schema=None)
async def isabelle_session_info() -> ToolResult:
    """Get information about current Isabelle session."""
    return _yaml_result(await session_info(await _ensure_lsp_started()))


# ── ML debugger tools (docs/archive/DEBUGGER_DESIGN.md section 4) ─────
#
# All are text results; the sentences and the orchestration live in
# debugger.py, the pure text assembly in utils/formatters.py. They require a
# session launched with debug=true and fail fast otherwise. The abort tool
# of section 4.13 is implemented (debugger.abort_eval_at_breakpoint) but NOT
# registered — user decision 2026-08-18: an agent's own eval call blocks, so
# the tool only serves parallel tool-call clients; expose it when one exists.


def _text_result(text: str) -> ToolResult:
    return ToolResult(content=[TextContent(type="text", text=text)])


@mcp.tool(output_schema=None)
async def isabelle_set_breakpoint(
    file_path: str, line: int, at_text: str | None = None,
) -> ToolResult:
    """Register a breakpoint and enable its breakable site.

    A breakable site exists only in ML code the prover has already compiled
    with debugging on, at statement boundaries the compiler chooses — use
    isabelle_list_breakable_sites to discover where breakpoints can go.
    After the breakpoint is set, it is hit the next time the armed code is
    executed. If the target command has already been evaluated, insert a
    space before it to force a re-evaluation so the command runs again and
    hits the breakpoint.

    The usual workflow: evaluate up to the end of the ML block that defines the
    code, or the `ML_file` command that loads it, then set the breakpoint and
    evaluate onward so the code runs and hits. This tool never evaluates: the
    defining block must be evaluated before the site exists. If the code to hit
    has already been evaluated, insert a space before it and re-evaluate to run
    it again. After an edit at or before the defining block, its breakpoints
    stop working: evaluate up to that block again, call
    `isabelle_enable_all_breakpoints`, then evaluate onward.

    Args:
        file_path: Absolute path to the .thy or .ML file
        line: Line number (1-indexed) of the breakable site
        at_text: Optional text snippet on the line. The breakable site at or
            nearest before its first occurrence is used (execution stops
            before that code runs). Without at_text, the first site on the
            line.
    """
    client = await _ensure_lsp_started()
    return _text_result(
        await debugger.set_breakpoint(client, file_path, line, at_text))


@mcp.tool(output_schema=None)
async def isabelle_del_breakpoints(
    breakpoints: list[BreakpointRef],
) -> ToolResult:
    """Disable and remove breakpoints.

    Args:
        breakpoints: The breakpoints to delete, as printed by
            isabelle_list_breakpoints.
    """
    client = await _ensure_lsp_started()
    refs = [(b.file_path, b.line, b.at_text) for b in breakpoints]
    return _text_result(await debugger.del_breakpoints(client, refs))


@mcp.tool(output_schema=None)
async def isabelle_list_breakpoints(file_path: str | None = None) -> ToolResult:
    """List the registered breakpoints. (For where breakpoints CAN go, see
    isabelle_list_breakable_sites.)

    Args:
        file_path: Absolute path; restricts the listing to one file. Omit for
            the whole registry.
    """
    client = await _ensure_lsp_started()
    return _text_result(debugger.list_breakpoints(client, file_path))


@mcp.tool(output_schema=None)
async def isabelle_list_breakable_sites(
    file_path: str, start_line: int | None = None, end_line: int | None = None,
) -> ToolResult:
    """List the breakable sites — the places where a breakpoint can be set —
    in a file's evaluated ML code. This tool never evaluates: lines beyond the
    evaluated part of the file are reported as not evaluated.

    Do not guess breakpoint positions from the source — lines you would
    expect to be breakable often are not. Call this first, then set
    breakpoints at the listed sites.

    Args:
        file_path: Absolute path to the .thy or .ML file
        start_line: First line (1-indexed) of the range. Default: whole file.
        end_line: Last line (1-indexed, inclusive). Default: whole file.
    """
    client = await _ensure_lsp_started()
    return _text_result(await debugger.list_breakable_sites(
        client, file_path, start_line, end_line))


@mcp.tool(output_schema=None)
async def isabelle_enable_all_breakpoints(
    file_path: str | None = None,
) -> ToolResult:
    """Enable all no-longer-working breakpoints.

    When the code of a breakpoint is recompiled, when the breakpoint is
    disabled, or when the prover is relaunched, the breakpoint may not
    work. This tool re-arms these no-longer-working breakpoints if their
    code has been evaluated. It never evaluates: a breakpoint whose code has
    not been evaluated stays pending, tagged `not evaluated yet`.

    Breakpoints stop working when their code is recompiled or the prover is
    relaunched; `isabelle_enable_all_breakpoints` re-arms them. Nothing
    re-arms in the background.

    Args:
        file_path: Restrict to breakpoints in this file. Omit for all
            breakpoints.
    """
    client = await _ensure_lsp_started()
    return _text_result(
        await debugger.enable_all_breakpoints(client, file_path))


@mcp.tool(output_schema=None)
async def isabelle_disable_all_breakpoints(
    file_path: str | None = None,
) -> ToolResult:
    """Disable all breakpoints, so an evaluation runs undisturbed.

    The breakpoints stay set but not working; isabelle_enable_all_breakpoints
    restores them.

    Args:
        file_path: Restrict to breakpoints in this file. Omit for all
            breakpoints.
    """
    client = await _ensure_lsp_started()
    return _text_result(
        await debugger.disable_all_breakpoints(client, file_path))


@mcp.tool(output_schema=None)
async def isabelle_debug_state() -> ToolResult:
    """Report all live hits — threads stopped in the debugger — with their
    call stacks. Callable at any time.

    A hit is one occasion of a thread halting; its hit_id is what the
    breakpoint tools take (thread names are shown as information, never an
    input). The frame numbers in the call stack are the frame parameter of
    isabelle_eval_at_breakpoint / isabelle_locals_at_breakpoint.

    A **hit** is one occasion of execution halting at a breakpoint, named
    `h1`, `h2`, … Inspect it with `isabelle_debug_state`,
    `isabelle_locals_at_breakpoint` and `isabelle_eval_at_breakpoint`; resume
    with `isabelle_continue_breakpoint` or `isabelle_step_at_breakpoint`.
    Resuming ends the hit — stopping again is a new hit with a new id. A
    **frame** is one entry of a hit's call stack; frame 0 is the innermost,
    top of the stack; outer frames follow.
    """
    client = await _ensure_lsp_started()
    return _text_result(debugger.debug_state(client))


@mcp.tool(output_schema=None)
async def isabelle_eval_at_breakpoint(
    expr: str, hit_id: str | None = None, frame: int = 0,
    timeout: float = debugger.EVAL_DEFAULT_TIMEOUT,
) -> ToolResult:
    """Evaluate an Isabelle/ML expression in the context of a hit.

    The expression can access the local variables in the context.
    Antiquotations work. The evaluation is not affected by breakpoints
    and never re-enters the debugger.

    Args:
        expr: Isabelle/ML expression to evaluate
        hit_id: The hit to evaluate at (e.g. "h1", as shown by
            isabelle_debug_state). May be omitted when exactly one hit is
            live.
        frame: Stack frame index from the call stack (frame 0 is the
            innermost, top of the stack; outer frames follow).
        timeout: Seconds before the evaluation is cut off.
    """
    client = await _ensure_lsp_started()
    return _text_result(await debugger.eval_at_breakpoint(
        client, expr, hit_id, frame, timeout))


@mcp.tool(output_schema=None)
async def isabelle_locals_at_breakpoint(
    hit_id: str | None = None, frame: int = 0,
    timeout: float = debugger.EVAL_DEFAULT_TIMEOUT,
) -> ToolResult:
    """Print all local variables in the context of a hit, with their types
    and values — the cheap first look before isabelle_eval_at_breakpoint.

    Args:
        hit_id: The hit to inspect (e.g. "h1", as shown by
            isabelle_debug_state). May be omitted when exactly one hit is
            live.
        frame: Stack frame index from the call stack (frame 0 is the
            innermost, top of the stack; outer frames follow).
        timeout: Seconds before the listing is cut off.
    """
    client = await _ensure_lsp_started()
    return _text_result(await debugger.locals_at_breakpoint(
        client, hit_id, frame, timeout))


@mcp.tool(output_schema=None)
async def isabelle_continue_breakpoint(hit_id: str | None = None) -> ToolResult:
    """Resume execution from a hit.

    Breakpoints stay armed — execution stops again at the next hit.

    Args:
        hit_id: Hit to resume. Omit to resume ALL live hits.
    """
    client = await _ensure_lsp_started()
    return _text_result(await debugger.continue_breakpoint(client, hit_id))


@mcp.tool(output_schema=None)
async def isabelle_step_at_breakpoint(
    mode: str, hit_id: str | None = None,
) -> ToolResult:
    """Single-step a hit's thread.

    `step` runs to the next breakable site, entering calls; `step_over`
    stays at the same or shallower stack depth; `step_out` runs until a
    shallower depth. Stepping only stops in ML compiled with debugging on:
    when execution leaves that region, stepping ends and the program simply
    runs on — to completion, or to the next armed breakpoint.

    Args:
        mode: "step", "step_over" or "step_out".
        hit_id: Hit to step. May be omitted when exactly one hit is live.
    """
    client = await _ensure_lsp_started()
    return _text_result(
        await debugger.step_at_breakpoint(client, mode, hit_id))


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
        "       %(prog)s install [--name NAME] [--isabelle-bin BIN] [--claude] [--codex]"
        " [--no-skills]\n"
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
