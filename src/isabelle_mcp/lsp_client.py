"""LSP client for Isabelle vscode_server — JSON-RPC 2.0 over stdin/stdout."""

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar

from isabelle_mcp.models import RunningCommand
from isabelle_mcp.processing import (
    ProcessingTracker,
    clip_line_range,
    note_edit_sent,
    parse_decoration_ranges,
)
from isabelle_mcp.unicode_guard import record_warning, sanitize_read
from isabelle_mcp.component import ensure_component
from isabelle_mcp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    file_path_to_uri,
    parse_goals_from_html,
    set_symbols_text,
    uri_to_file_path,
)

logger = logging.getLogger(__name__)

JsonDict = dict[str, Any]


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the subprocess's whole process group.

    Isabelle tools are bash wrappers around a java child. Killing only the
    wrapper leaves the child alive holding the stdout/stderr pipes open, and
    asyncio resolves ``process.wait()`` only once every pipe disconnects — so
    a plain ``proc.kill()`` makes that wait hang forever. Requires the process
    to have been spawned with ``start_new_session=True`` (it is then its own
    group leader); falls back to killing just the wrapper otherwise.
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

# (full version string, major year) of the `isabelle` on PATH — probed once and
# cached for the process, since PATH (hence the binary) is fixed within a process.
# None until the first probe.
_isabelle_version_cache: tuple[str, int | None] | None = None

# stderr lines matching this surface at WARNING (not DEBUG) so server-side failures
# — e.g. a swallowed serialization exception — are visible early instead of buried.
_STDERR_ERROR_RE = re.compile(
    r"\b(error|exception|fail(?:ed|ure)?|bad json|uncaught|cannot)\b|unknown isabelle tool",
    re.IGNORECASE,
)

# Raw wire dump: when ISABELLE_MCP_DUMP names a file, every JSON-RPC frame in/out of
# the vscode_server is appended there as one JSON line per frame. Default off so
# the shared/live server is unaffected.
_DUMP_PATH: str | None = os.environ.get("ISABELLE_MCP_DUMP") or None


def _wire_dump(direction: str, message: JsonDict) -> None:
    if _DUMP_PATH is None:
        return
    try:
        with open(_DUMP_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "dir": direction, "msg": message},
                                ensure_ascii=False) + "\n")
    except OSError:
        pass


def _canon(file_path: str) -> str:
    """Canonical absolute path (resolves symlinks and ``..``) for stable keying.

    All ``open_documents`` keys, URIs, watch directories, and stat comparisons use
    this form so a symlinked/relative path and its real path never desync.
    """
    return os.path.realpath(file_path)


StatSig = tuple[int, int, int, int]


def _stat_sig(file_path: str) -> StatSig | None:
    """Change-signature of a file: ``(st_ino, st_size, st_mtime_ns, st_ctime_ns)``.

    Compared with ``!=`` (never ``>``): mtime is non-monotonic and tamperable, so
    any differing field means "possibly changed" — content comparison is the final
    gate. Returns ``None`` when the file cannot be stat'd (e.g. it was deleted).
    """
    try:
        st = os.stat(file_path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _stat_sigs(paths: list[str]) -> dict[str, StatSig | None]:
    """Batch :func:`_stat_sig` — runnable off the event loop via ``to_thread``."""
    return {p: _stat_sig(p) for p in paths}


def _detect_isabelle_version() -> tuple[str, int | None]:
    """Probe the `isabelle` on PATH: ``(full version string, major year)``.

    Runs ``isabelle version`` once and caches the result for the process. Returns
    ``("unknown", None)`` when the version cannot be determined.
    """
    global _isabelle_version_cache
    if _isabelle_version_cache is None:
        ver = "unknown"
        try:
            out = subprocess.run(
                ["isabelle", "version"],
                capture_output=True, text=True, timeout=30, check=False,
            ).stdout.strip()
            ver = out.splitlines()[0].strip() if out else "unknown"
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Could not detect Isabelle version: %s", exc)
        match = re.search(r"Isabelle(\d{4})", ver)
        _isabelle_version_cache = (ver or "unknown", int(match.group(1)) if match else None)
    return _isabelle_version_cache


def isabelle_version() -> str:
    """Full version string of the `isabelle` on PATH, e.g. ``"Isabelle2025-2"``."""
    return _detect_isabelle_version()[0]


def isabelle_year() -> int | None:
    """Major year (e.g. ``2025``) of the `isabelle` on PATH; ``None`` if unknown."""
    return _detect_isabelle_version()[1]


def unicode_symbols_option() -> str:
    """Return the version-correct vscode_server option for unicode symbol output.

    Isabelle2025 renamed ``vscode_unicode_symbols`` to ``vscode_unicode_symbols_output``
    (passing the old name aborts the 2025 server at startup). Falls back to the
    pre-2025 name when the version cannot be detected.
    """
    year = isabelle_year()
    return "vscode_unicode_symbols_output" if year is not None and year >= 2025 \
        else "vscode_unicode_symbols"


def read_vscode_load_delay(default: float = 0.5) -> float:
    """Return the server's ``vscode_load_delay`` (its File_Watcher debounce, seconds).

    Read via ``isabelle options -g vscode_load_delay`` so the dependency-freshness
    wait (Layer 3) tracks any ``-o vscode_load_delay=…`` override instead of a
    hardcoded constant. Falls back to *default* if the option cannot be read.
    """
    try:
        out = subprocess.run(
            ["isabelle", "options", "-g", "vscode_load_delay"],
            capture_output=True, text=True, timeout=30, check=False,
        ).stdout.strip()
        return float(out)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.warning("Could not read vscode_load_delay (%s); using %.2f", exc, default)
        return default


@dataclass
class DocumentState:
    file_path: str
    uri: str
    version: int
    content: str
    language_id: str = "isabelle"
    # Last on-disk signature we synced to the server. ``None`` forces a re-read on
    # the next stat backstop (used after force_interrupt mutates the model only).
    stat_sig: StatSig | None = None


@dataclass
class DiagnosticCache:
    diagnostics: dict[str, list[dict]] = field(default_factory=dict)
    last_update: dict[str, float] = field(default_factory=dict)


class IsabelleLSPClient:
    """Manages the lifecycle of `isabelle vscode_server` and JSON-RPC 2.0 communication."""

    STALL_TIMEOUT: ClassVar[float] = 120.0
    PROGRESS_CHECK_INTERVAL: ClassVar[float] = 5.0
    STATE_OUTPUT_GRACE: ClassVar[float] = 10.0
    # find_theorems can legitimately scan a large theorem DB for much longer than a
    # goal panel takes to (not) produce state output, so it gets its own, longer
    # grace before a silent server is treated as "no result".
    FIND_THEOREMS_OUTPUT_GRACE: ClassVar[float] = 30.0

    def __init__(
        self,
        logic: str = "HOL",
        session_dirs: list[str] | None = None,
        verbose: bool = False,
        extra_args: list[str] | None = None,
        project_root: str | None = None,
    ):
        self.logic = logic
        self.session_dirs = session_dirs or []
        self.verbose = verbose
        self.extra_args = extra_args or []
        # Real paths of every source file precompiled into the running logic's
        # heap chain (filled by enumerate_heap_sources at launch). Such files
        # cannot be edited: PIDE ignores their changes and never reprocesses
        # them, so tools warn instead of silently wedging.
        self.heap_sources: set[str] = set()
        # Build-status verdict from the launch-time `isabelle build -n -b` probe:
        # None = not probed, True = whole chain built and current, False = some
        # session unbuilt/outdated (named in unfinished_sessions when known).
        self.heap_built: bool | None = None
        self.unfinished_sessions: list[str] = []
        # Base directory for relativizing displayed paths. ``None`` (the current
        # placeholder) → renderers show absolute paths. A real per-agent root will
        # be set with the stdio-per-agent refactor.
        self.project_root = project_root

        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()

        self.request_id = 0
        self.pending_requests: dict[int, asyncio.Future[Any]] = {}

        self.open_documents: dict[str, DocumentState] = {}
        self.diagnostic_cache = DiagnosticCache()
        self._first_diagnostic_event: dict[str, asyncio.Event] = {}

        # Optional FileWatcher (set by the server). open_document/close_document
        # register/deregister the file's parent directory for event-driven sync.
        self.file_watcher: Any = None

        # Dependency-freshness (Layer 3): last-seen stat signatures of server-owned
        # dependency files (external imports + .ML blobs), keyed by node_name.
        self._dep_stat_sigs: dict[str, StatSig | None] = {}
        # The server File_Watcher's debounce; refreshed from options at start().
        self.vscode_load_delay: float = 0.5

        # Caret lock: serializes the entire goal/dynamic_output query cycle.
        # The Isabelle caret is global — see docs/ARCHITECTURE.md §7.3.
        self._caret_lock = asyncio.Lock()
        self._state_init_waiters: list[asyncio.Future[tuple[int, str]]] = []

        # PIDE find_theorems (async Query_Operation, serialized by _caret_lock).
        # The output notification carries the rendered HTML; the status notification
        # signals completion. Each query carries a monotonic token, echoed back in the
        # output/status notifications, so a straggler from a previous (aborted) query
        # is ignored rather than corrupting the current query's buffer/waiter. The
        # buffer is reset to None at each query START so a `finished` arriving with no
        # output (caret not on a command / command removed) resolves to None.
        self._find_theorems_waiters: list[asyncio.Future[str | None]] = []
        self._find_theorems_output: str | None = None
        self._find_theorems_seq: int = 0
        self._find_theorems_token: str = ""

        # PIDE dynamic output
        self._dynamic_output_waiters: list[tuple[tuple[str, int, int], asyncio.Future[str]]] = []
        self._dynamic_output_cache_by_position: dict[tuple[str, int, int], str] = {}

        # PIDE preview
        self._preview_lock = asyncio.Lock()
        self._preview_waiters: dict[tuple[str, int], asyncio.Future[JsonDict]] = {}

        # PIDE processing status (from PIDE/decoration)
        self._processing_trackers: dict[str, ProcessingTracker] = {}

        # Server activity tracking for progress monitoring
        self._last_server_activity: float = 0.0

        self.server_capabilities: dict[str, Any] = {}
        self.isabelle_version: str = ""
        self.start_time: float = 0.0

        # Pre-handshake server-reported errors (type-1 log/show messages). With
        # `vscode_server -n`, a missing heap image wedges the server before it
        # ever answers `initialize`; the only signal is one such message, so it
        # is surfaced on the pending request instead of a blind timeout.
        self.startup_errors: list[str] = []
        self._handshake_done: bool = False

    # ── Progress monitoring ────────────────────────────────────────────

    async def _wait_with_progress(
        self,
        future: asyncio.Future[Any],
        stall_timeout: float | None = None,
    ) -> Any:
        """Wait for a future, raising IsabelleToolError if Isabelle stalls or crashes.

        Progress is detected by any incoming server message. If no message
        arrives for stall_timeout seconds, assumes Isabelle is stuck.
        """
        if stall_timeout is None:
            stall_timeout = self.STALL_TIMEOUT
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), timeout=self.PROGRESS_CHECK_INTERVAL,
                )
            except asyncio.TimeoutError:
                if future.done():
                    return future.result()
                self._check_server_health(stall_timeout)

    def _check_server_health(self, stall_timeout: float) -> None:
        """Raise IsabelleToolError if the Isabelle process appears dead or stalled."""
        if self.process is not None and self.process.returncode is not None:
            raise IsabelleToolError(
                f"Isabelle process died (exit code {self.process.returncode})"
            )
        if self._last_server_activity > 0:
            elapsed = time.time() - self._last_server_activity
            if elapsed > stall_timeout:
                raise IsabelleToolError(
                    f"Isabelle appears stalled — no server activity for {elapsed:.0f}s"
                )

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        # Re-entry guard: a live process must be shut down before re-starting, else
        # the old reader/stderr tasks leak and two read-loops race on one stdout.
        if self.process is not None and self.process.returncode is None:
            return
        # Reset before anything that can raise, so a failed start never leaves a
        # stale handshake flag or another session's startup errors behind.
        self._handshake_done = False
        self.startup_errors = []
        # `isabelle mcp_server` is provided by the Scala component we ship. Registering it is
        # cheap and idempotent (a single file read once it is in place), and it must happen here
        # rather than at import: another install can evict our registration at any moment, and a
        # memoised process would then silently spawn the *other* install's jar and ML prelude.
        ensure_component()
        cmd = [
            # -n: never build the session heap implicitly — a missing heap would
            # otherwise turn launch into a silent, hour-scale build that looks
            # like a hang. Build status is checked explicitly at launch instead.
            "isabelle", "mcp_server", "-n", "-l", self.logic,
            "-o", "vscode_pide_extensions",
            "-o", unicode_symbols_option(),
            "-o", "vscode_caret_perspective=1",
            # Keep the proof state OUT of command output by default — it is the job of
            # isabelle_goal (the state panel works regardless of this option). Override
            # with a later "-o editor_output_state=true" in extra_args to include it.
            "-o", "editor_output_state=false",
        ]
        # Isabelle2025 routes the state/dynamic panels through Pretty_Text_Panel, which
        # by default emits plain text + decorations — but that path is broken upstream
        # (`decorations.map(_.json)` eta-expands `Decoration.json(file)` into a lambda →
        # "Bad JSON value", so the state panel silently emits nothing and isabelle_goal
        # returns []). Force the HTML branch, which parse_goals_from_html already consumes.
        # The option does not exist pre-2025 (passing it aborts the server), so gate it.
        if (isabelle_year() or 0) >= 2025:
            cmd += ["-o", "vscode_html_output=true"]
        for d in self.session_dirs:
            cmd.extend(["-d", d])
        if self.verbose:
            cmd.append("-v")
        cmd.extend(self.extra_args)

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Own process group, so kill() can take down the whole
                # bash-wrapper + java tree (see _kill_process_tree).
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise IsabelleToolError(
                "isabelle command not found. Is Isabelle installed and in PATH?"
            ) from exc

        self.start_time = time.time()
        self._last_server_activity = self.start_time
        self.vscode_load_delay = read_vscode_load_delay()
        self.reader_task = asyncio.create_task(self._read_loop())
        self.stderr_task = asyncio.create_task(self._drain_stderr())
        await self.initialize()
        self._handshake_done = True
        # The LSP handshake omits serverInfo.version, so fall back to the cached
        # `isabelle version` probe (the same module-level detector that drives
        # unicode_symbols_option / the state-panel protocol choice). It resolves
        # the same `isabelle` on PATH that launched this session, so it matches.
        if self.isabelle_version in ("", "unknown"):
            self.isabelle_version = isabelle_version()
        await self._seed_symbols()

    @staticmethod
    def parse_build_sources(listing: str) -> set[str]:
        """Parse ``isabelle build -n -l`` output into a set of real paths.

        File lines are two-space-indented absolute paths grouped under
        ``Session ...`` headers. A listing without any header means the command
        really failed (e.g. undefined session) → empty set. The exit code is
        NOT a failure signal: any out-of-date session in the chain yields
        exit 1 while still printing the complete listing.
        """
        if "Session " not in listing:
            return set()
        return {
            os.path.realpath(line.strip())
            for line in listing.splitlines()
            if line.startswith("  ")
        }

    @staticmethod
    def parse_unfinished_sessions(listing: str) -> list[str]:
        """Session names from the ``Unfinished session(s): A, B`` line that
        ``isabelle build -n -v`` prints when something in the chain is unbuilt
        or not up-to-date. No such line → empty list (callers fall back to a
        generic message)."""
        for line in listing.splitlines():
            if line.startswith("Unfinished session(s):"):
                names = line.split(":", 1)[1]
                return [n.strip() for n in names.split(",") if n.strip()]
        return []

    def build_hint(self) -> str:
        """The exact command that builds the launched session (and its whole
        dependency chain). Options must precede the session name — Isabelle's
        option parsing stops at the first positional argument."""
        parts = ["isabelle", "build", "-b"]
        for d in self.session_dirs:
            parts += ["-d", shlex.quote(d)]
        parts.append(shlex.quote(self.logic))
        return " ".join(parts)

    async def enumerate_heap_sources(self) -> None:
        """Probe the current logic via ``isabelle build -n -b -v -l``: fills
        ``heap_sources`` plus the build-status verdict ``heap_built`` /
        ``unfinished_sessions``.

        ``-n`` is a strict dry run (reads the existing build databases only —
        no build, no prover); ``-b`` makes the verdict require the stored heap
        image, matching what ``vscode_server`` actually loads; the listing
        comes from the same ``Sessions.deps`` computation PIDE uses for its
        loaded-theories set. Runs once per launch (in parallel with the server
        start). Raises IsabelleToolError when the probe itself cannot run
        (fail-closed): launch treats an unverifiable build status as an error.
        """
        self.heap_sources = set()
        self.heap_built = None
        self.unfinished_sessions = []
        cmd = ["isabelle", "build", "-n", "-b", "-v", "-l"]
        for d in self.session_dirs:
            cmd += ["-d", d]
        cmd.append(self.logic)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                # Own process group: `isabelle build` is a bash wrapper around
                # a java child, and aborting must take down both.
                start_new_session=True,
            )
        except OSError as exc:
            raise IsabelleToolError(
                f"Could not verify the session's build status "
                f"(`{' '.join(cmd[:6])} ...` failed to run): {exc}"
            ) from exc
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            _kill_process_tree(proc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise IsabelleToolError(
                "Could not verify the session's build status: `isabelle build "
                "-n` timed out after 120s (it only checksums sources — check "
                "disk/CPU load and retry the launch)."
            ) from exc
        listing = stdout.decode(errors="replace")
        self.heap_built = proc.returncode == 0
        self.unfinished_sessions = self.parse_unfinished_sessions(listing)
        self.heap_sources = self.parse_build_sources(listing)
        if not self.heap_sources:
            logger.warning(
                "heap source enumeration found nothing for session %r", self.logic
            )

    def heap_warning(self, file_path: str) -> str | None:
        """Warning text when *file_path* is precompiled into the running heap."""
        if os.path.realpath(file_path) not in self.heap_sources:
            return None
        return (
            f"{file_path} is PRECOMPILED into the running session "
            f"'{self.logic}' (heap image). Isabelle IGNORES edits to "
            "precompiled theories: changed content is never reprocessed, and "
            "once the file differs from the heap every evaluation/query on it "
            "fails. Treat it as read-only. To edit it, relaunch via "
            "isabelle_launch with a base session not including the theory."
        )

    async def initialize(self) -> dict[str, Any]:
        try:
            response = await self.request("initialize", {
                "processId": None,
                "rootUri": None,
                "capabilities": {},
            }, timeout=30.0)
        except IsabelleToolError as exc:
            # Attach buffered pre-handshake server errors to a blind timeout.
            # Only the timeout path needs this: the fail-fast path's exception
            # (set by _surface_server_message) already carries the message, and
            # a JSON-RPC error reply ("Undefined session(s): ...") is
            # self-explanatory — appending heap hints there would mislead.
            if isinstance(exc.__cause__, asyncio.TimeoutError) and self.startup_errors:
                raise IsabelleToolError(
                    f"{exc} — the server reported during startup: "
                    + " | ".join(self.startup_errors)
                ) from exc
            raise
        result = response if isinstance(response, dict) else {}
        if result:
            self.server_capabilities = result.get("capabilities", {})
            self.isabelle_version = result.get("serverInfo", {}).get("version", "")
        await self.notify("initialized", {})
        return result

    async def _seed_symbols(self) -> None:
        """Fetch the Isabelle symbol table and seed the local converter.

        Best-effort: the patched server answers PIDE/symbols with the text of its
        etc/symbols files, which feeds ascii_of_unicode without any subprocess.
        On a stock/unpatched server this fails silently and the converter falls
        back to 'isabelle getenv' on first use.
        """
        try:
            text = await self.get_symbols()
        except (IsabelleToolError, asyncio.TimeoutError) as exc:
            logger.info("PIDE/symbols unavailable (%s); converter will use fallback.", exc)
            return
        if text:
            set_symbols_text(text)

    async def get_symbols(self) -> str:
        """Return the concatenated text of the server's etc/symbols files.

        Uses the patched PIDE/symbols request. Returns "" if the server replies
        without content.
        """
        result = await self.request("PIDE/symbols", {})
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, str):
                return content
        return ""

    def kill(self) -> None:
        """Synchronously kill the server process tree. No awaits — safe to call
        from a cleanup path that must survive task cancellation."""
        if self.process is not None:
            _kill_process_tree(self.process)

    async def reap(self) -> None:
        """Reap a killed/dead server: cancel the reader tasks, collect the
        process, clear per-session state. Counterpart of kill()."""
        await self._cancel_background_tasks()
        if self.process is not None:
            with contextlib.suppress(Exception):
                # Killed via the process group, so the pipes are closed and
                # this resolves promptly; the timeout is defense-in-depth —
                # never let launch cleanup hang on a wait.
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
        self._clear_session_state()

    async def shutdown(self) -> None:
        if self.process and self.process.returncode is None:
            if not self._handshake_done:
                # The handshake never completed (e.g. a missing heap image
                # wedges the server before it answers `initialize`): the
                # graceful protocol below could only burn ~10s of timeouts,
                # so kill outright.
                self.kill()
                await self.reap()
                return
            try:
                await asyncio.wait_for(self.request("shutdown", {}, timeout=5.0), timeout=5.0)
            except (asyncio.TimeoutError, IsabelleToolError):
                pass
            with contextlib.suppress(IsabelleToolError):
                await self.notify("exit", {})
            await self._cancel_background_tasks()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # Group kill: killing just the bash wrapper would leave the
                # java child holding the pipes and this wait would never end.
                _kill_process_tree(self.process)
                await self.process.wait()

        self._clear_session_state()

    def _clear_session_state(self) -> None:
        self._handshake_done = False
        self.open_documents.clear()
        self._dep_stat_sigs.clear()
        self.pending_requests.clear()
        self.diagnostic_cache.diagnostics.clear()
        self.diagnostic_cache.last_update.clear()
        self._first_diagnostic_event.clear()
        self._state_init_waiters.clear()
        self._find_theorems_waiters.clear()
        self._find_theorems_output = None
        self._dynamic_output_waiters.clear()
        self._dynamic_output_cache_by_position.clear()
        self._preview_waiters.clear()
        self._processing_trackers.clear()

        # Reset the module-global evaluation singleton so a later relaunch starts
        # clean — otherwise a terminate mid-evaluation leaves evaluation_state.active
        # True and the next session rejects every evaluate_to. Lazy import avoids the
        # import cycle with evaluation.py (which imports this module).
        from isabelle_mcp.evaluation import evaluation_state
        evaluation_state.cancel()
        evaluation_state.auto_opened_files.clear()

    # ── JSON-RPC transport ──────────────────────────────────────────────

    async def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        """Send an LSP request and wait for the response.

        When timeout is None (default), uses progress monitoring — no fixed
        timeout, but raises IsabelleToolError if the server stalls or crashes.
        When timeout is set, uses a hard deadline (for lifecycle methods like
        initialize/shutdown).
        """
        self.request_id += 1
        req_id = self.request_id
        message = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = future

        try:
            await self._send(message)
        except BaseException:
            # BaseException: a CancelledError here (e.g. parked on _write_lock)
            # must not leak the pending entry — a later writer (such as the
            # pre-handshake type-1 handler) would set an exception nobody
            # retrieves.
            self.pending_requests.pop(req_id, None)
            raise

        try:
            if timeout is not None:
                return await asyncio.wait_for(future, timeout=timeout)
            return await self._wait_with_progress(future)
        except asyncio.TimeoutError as exc:
            raise IsabelleToolError(f"LSP request '{method}' timed out after {timeout}s") from exc
        finally:
            self.pending_requests.pop(req_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: JsonDict) -> None:
        if not self.process or not self.process.stdin:
            raise IsabelleToolError("LSP process not running")
        _wire_dump("out", message)
        content = json.dumps(message).encode('utf-8')
        header = f"Content-Length: {len(content)}\r\n\r\n".encode('ascii')
        async with self._write_lock:
            try:
                self.process.stdin.write(header + content)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                raise IsabelleToolError("Failed to write to LSP process") from exc

    # ── Background readers ──────────────────────────────────────────────

    async def _read_loop(self) -> None:
        try:
            while True:
                if not self.process or not self.process.stdout:
                    break
                message = await self._read_message()
                if message is None:
                    break
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Read loop failed: {e}", exc_info=True)
            self._fail_pending_waiters(IsabelleToolError(f"LSP read loop failed: {e}"))
            return
        # Clean EOF: the server closed stdout without answering. That is a prover that died before
        # the handshake — e.g. `isabelle mcp_server` does not exist, so bash printed to stderr and
        # exited. Waking the waiters here is what turns a content-free 30 s `initialize` timeout
        # into the child's own words.
        self._fail_pending_waiters(
            IsabelleToolError(
                "The Isabelle server exited before the LSP handshake."
                + (f"\n{chr(10).join(self.startup_errors)}" if self.startup_errors else "")
            )
        )

    async def _read_message(self) -> JsonDict | None:
        if not self.process or not self.process.stdout:
            return None
        headers: dict[str, str] = {}
        while True:
            header_line = await self.process.stdout.readline()
            if not header_line:
                return None
            line = header_line.decode("ascii", errors="replace").strip()
            if not line:
                break
            name, sep, value = line.partition(":")
            if sep:
                headers[name.lower()] = value.strip()

        raw_length = headers.get("content-length")
        if raw_length is None:
            logger.warning("LSP message missing Content-Length header: %s", headers)
            return {}
        try:
            content_length = int(raw_length)
        except ValueError:
            logger.warning("LSP message has non-integer Content-Length: %r", raw_length)
            return {}

        content = await self.process.stdout.readexactly(content_length)
        try:
            message = json.loads(content.decode("utf-8"))
        except json.JSONDecodeError:
            logger.warning("LSP message has invalid JSON (length=%d): %s", content_length, content[:200])
            return {}
        if isinstance(message, dict):
            _wire_dump("in", message)
            return message
        return {}

    async def _drain_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                # Surface error-ish stderr at WARNING so server-side failures (e.g. a
                # swallowed serialization exception) are visible early, not buried in DEBUG.
                if _STDERR_ERROR_RE.search(text):
                    logger.warning("isabelle stderr: %s", text)
                else:
                    logger.debug("isabelle stderr: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("stderr drain stopped", exc_info=True)

    async def _cancel_background_tasks(self) -> None:
        tasks = [
            t for t in (self.reader_task, self.stderr_task)
            if t is not None
        ]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self.reader_task = None
        self.stderr_task = None

    # ── Message dispatch ────────────────────────────────────────────────

    async def _handle_message(self, message: JsonDict) -> None:
        self._last_server_activity = time.time()
        method = message.get("method", "")
        msg_id = message.get("id", "")
        if method:
            logger.debug("← notification: %s", method)
        elif msg_id:
            logger.debug("← response id=%s", msg_id)

        if "id" in message and message["id"] in self.pending_requests:
            req_id = message["id"]
            future = self.pending_requests.pop(req_id)
            if "result" in message:
                future.set_result(message["result"])
            elif "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    error_message = error.get('message', 'Unknown')
                else:
                    error_message = str(error)
                future.set_exception(
                    IsabelleToolError(f"LSP error: {error_message}")
                )
            else:
                future.set_exception(
                    IsabelleToolError("LSP response missing result/error")
                )
        elif "method" in message:
            await self._handle_notification(message["method"], message.get("params", {}))

    async def _handle_notification(self, method: str, params: Any) -> None:
        if method == "textDocument/publishDiagnostics":
            if not isinstance(params, dict):
                return
            uri = params.get("uri", "")
            if not isinstance(uri, str) or not uri.startswith("file://"):
                return
            diagnostics = params.get("diagnostics", [])
            if not isinstance(diagnostics, list):
                diagnostics = []
            file_path = uri_to_file_path(uri)
            self.diagnostic_cache.diagnostics[file_path] = diagnostics
            self.diagnostic_cache.last_update[file_path] = time.time()
            event = self._first_diagnostic_event.get(file_path)
            if event is not None and not event.is_set():
                event.set()
        elif method == "PIDE/decoration":
            await self._handle_decoration(params)
        elif method == "PIDE/state_output":
            self._handle_state_output(params)
        elif method == "PIDE/find_theorems_output":
            self._handle_find_theorems_output(params)
        elif method == "PIDE/find_theorems_status":
            self._handle_find_theorems_status(params)
        elif method == "PIDE/dynamic_output":
            self._handle_dynamic_output(params)
        elif method == "PIDE/preview_response":
            self._handle_preview_response(params)
        elif method in ("window/logMessage", "window/showMessage"):
            self._surface_server_message(params)

    def _surface_server_message(self, params: Any) -> None:
        """Surface a server-originated LSP log/show message so server-side errors are
        not silently swallowed. Isabelle reports prover/serialization failures here
        (LSP MessageType: 1=Error, 2=Warning, 3=Info, 4=Log); routing them through the
        logger makes them visible early instead of being dropped on the floor."""
        if not isinstance(params, dict):
            return
        text = str(params.get("message", "")).strip()
        if not text:
            return
        mtype = params.get("type")
        if mtype == 1:
            logger.error("isabelle server: %s", text)
            if not self._handshake_done:
                # A pre-handshake error (e.g. "Missing heap image ...") wedges
                # the server before it ever answers `initialize` — fail the
                # pending request now instead of letting it time out blind.
                # Failing ALL pending futures is safe here: isabelle_launch
                # holds _evaluation_state_lock for the whole start sequence and
                # every other request path enters through _ensure_lsp_started,
                # which takes that same lock first — so the only pending
                # request at this point is `initialize`.
                self.startup_errors.append(text)
                failure = IsabelleToolError(
                    f"Isabelle server failed during startup: {text}\n"
                    f"If a heap image is missing or outdated, build it first "
                    f"({self.build_hint()}) and call isabelle_launch again — "
                    f"the MCP server never builds sessions itself."
                )
                for fut in self.pending_requests.values():
                    if not fut.done():
                        fut.set_exception(failure)
        elif mtype == 2:
            logger.warning("isabelle server: %s", text)
        else:
            logger.debug("isabelle server: %s", text)

    async def _handle_decoration(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        uri = params.get("uri", "")
        if not isinstance(uri, str) or not uri.startswith("file://"):
            return
        entries = params.get("entries")
        if not isinstance(entries, list):
            return
        parsed = parse_decoration_ranges(entries)
        file_path = uri_to_file_path(uri)
        tracker = self._processing_trackers.get(file_path)
        if tracker is None:
            if not parsed:
                return
            tracker = ProcessingTracker()
            self._processing_trackers[file_path] = tracker
        await tracker.update(parsed)

    def _handle_state_output(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        raw_panel_id = params.get("id")
        html = str(params.get("content", params.get("output", "")))
        if not isinstance(raw_panel_id, int):
            return

        if self._state_init_waiters:
            init_future = self._state_init_waiters.pop(0)
            if not init_future.done():
                init_future.set_result((raw_panel_id, html))

    def _handle_find_theorems_output(self, params: Any) -> None:
        # Keep the latest NON-empty content for the CURRENT query only. The token
        # guard drops stragglers from a previous (aborted) query whose overlay is
        # still emitting. apply_query also emits an empty init output at query start
        # (Editor.Output.init, rendered to ""); the strip() check ignores it so the
        # buffer is not clobbered or mistaken for a real (empty) result.
        if not isinstance(params, dict):
            return
        if params.get("token") != self._find_theorems_token:
            return
        content = params.get("content")
        if isinstance(content, str) and content.strip():
            self._find_theorems_output = content

    def _handle_find_theorems_status(self, params: Any) -> None:
        # Resolve on `finished` reading the per-query buffer. The token guard ignores
        # a `finished` belonging to a previous query. Within one content_update the
        # output callback fires before the status callback, so a real output is
        # already buffered; a `finished` with the buffer still None means no output
        # was produced (no command at caret / command removed).
        if not isinstance(params, dict):
            return
        if params.get("token") != self._find_theorems_token:
            return
        if params.get("status") != "finished":
            return
        if self._find_theorems_waiters:
            future = self._find_theorems_waiters.pop(0)
            if not future.done():
                future.set_result(self._find_theorems_output)

    def _handle_dynamic_output(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        html = str(params.get("content", ""))
        waiters = self._dynamic_output_waiters
        self._dynamic_output_waiters = []
        for key, future in waiters:
            self._dynamic_output_cache_by_position[key] = html
            if not future.done():
                future.set_result(html)

    def _handle_preview_response(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        uri = str(params.get("uri", ""))
        column = params.get("column", 0)
        if not isinstance(column, int):
            column = 0
        future = self._preview_waiters.pop((uri, column), None)
        if future and not future.done():
            future.set_result(params)

    def _all_waiters(self) -> list[asyncio.Future]:
        futures: list[asyncio.Future] = []
        futures.extend(self.pending_requests.values())
        futures.extend(self._state_init_waiters)
        futures.extend(self._find_theorems_waiters)
        futures.extend(future for _, future in self._dynamic_output_waiters)
        futures.extend(self._preview_waiters.values())
        return futures

    def _fail_pending_waiters(self, exc: Exception) -> None:
        for future in self._all_waiters():
            if not future.done():
                future.set_exception(exc)
        self.pending_requests.clear()
        self._state_init_waiters.clear()
        self._find_theorems_waiters.clear()
        self._find_theorems_output = None
        self._dynamic_output_waiters.clear()
        self._dynamic_output_cache_by_position.clear()
        self._preview_waiters.clear()

    # ── High-level document methods ─────────────────────────────────────

    def _add_file_watch(self, file_path: str) -> None:
        """Register the file's parent dir with the watcher (event-driven sync)."""
        fw = self.file_watcher
        if fw is not None:
            fw.add_watch(os.path.dirname(file_path))

    def _remove_file_watch(self, file_path: str) -> None:
        """Deregister the file's parent dir — only if no other open doc lives there."""
        fw = self.file_watcher
        if fw is None:
            return
        directory = os.path.dirname(file_path)
        if not any(os.path.dirname(p) == directory for p in self.open_documents):
            fw.remove_watch(directory)

    async def open_document(
        self,
        file_path: str,
        content: str | None = None,
        *,
        wait_for_diagnostics: bool = True,
        diagnostic_timeout: float = 2.0,
    ) -> None:
        """Ensure *file_path* is open (didOpen once); never re-sync content here.

        For an already-open document this returns immediately — it does NOT re-read
        disk, bump the version, or send didChange. All content syncing is owned by
        the locked sync paths (:meth:`resync_changed_open_documents` /
        :meth:`sync_dirty_files`), which run via the tool-call backstop before any
        ``open_document`` in a tool body. This removes the only unlocked didChange
        path and the version race it caused.
        """
        file_path = _canon(file_path)

        if file_path in self.open_documents:
            return

        if content is None:
            # Unicode guard (off the event loop): may rewrite the file in
            # Isabelle ASCII; the returned text matches disk afterwards, so the
            # stat_sig taken below stays coherent. Caller-passed content (no
            # current callers) bypasses the guard — it has no disk counterpart
            # to keep in sync.
            content, guard_warning = await asyncio.to_thread(sanitize_read, file_path)
            if guard_warning is not None:
                record_warning(file_path, guard_warning)
            if file_path in self.open_documents:
                # Another coroutine opened it while we were off-loop.
                return

        uri = file_path_to_uri(file_path)

        event = asyncio.Event()
        self._first_diagnostic_event[file_path] = event

        # Register in open_documents BEFORE didOpen: notify -> _send awaits stdin.drain(),
        # a cancel checkpoint. If registration lagged the didOpen, a re-delivered cancel
        # there would leave the server holding the doc with no open_documents entry, so
        # close_document (which pops that dict) could never send the matching didClose —
        # an orphan. Registering first keeps the two in sync under cancellation.
        self.open_documents[file_path] = DocumentState(
            file_path=file_path, uri=uri, version=1, content=content,
            stat_sig=_stat_sig(file_path),
        )
        await self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "isabelle",
                "version": 1,
                "text": content,
            }
        })
        note_edit_sent()  # didOpen pushes content: an edit-send like any other
        self._add_file_watch(file_path)

        if wait_for_diagnostics:
            received = await self.wait_for_first_diagnostics(
                file_path,
                timeout=diagnostic_timeout,
            )
            if not received:
                logger.debug(
                    "No diagnostics received for %s within %.1fs",
                    file_path,
                    diagnostic_timeout,
                )

    async def wait_for_first_diagnostics(self, file_path: str, timeout: float = 2.0) -> bool:
        if file_path in self.diagnostic_cache.last_update:
            return True

        event = self._first_diagnostic_event.get(file_path)
        if event is None:
            event = asyncio.Event()
            self._first_diagnostic_event[file_path] = event

        if event.is_set():
            return True

        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.0, timeout))
        except asyncio.TimeoutError:
            return False
        return True

    async def set_caret(
        self, file_path: str, line: LSPLine, character: LSPCharacter = LSPCharacter(0),
    ) -> None:
        """Send PIDE/caret_update to tell Isabelle which region to process."""
        doc = self.open_documents.get(_canon(file_path))
        if doc is None:
            return
        await self.notify("PIDE/caret_update", {
            "uri": doc.uri,
            "line": line,
            "character": character,
            "focus": True,
        })

    async def close_document(self, file_path: str) -> None:
        file_path = _canon(file_path)
        doc = self.open_documents.pop(file_path, None)
        if doc is None:
            return
        await self.notify("textDocument/didClose", {"textDocument": {"uri": doc.uri}})
        self.diagnostic_cache.diagnostics.pop(file_path, None)
        self.diagnostic_cache.last_update.pop(file_path, None)
        self._first_diagnostic_event.pop(file_path, None)
        tracker = self._processing_trackers.pop(file_path, None)
        if tracker is not None:
            await tracker.reset()
        self._remove_file_watch(file_path)

    # ── Processing status (PIDE/decoration) ────────────────────────────

    async def wait_for_processing(
        self,
        file_path: str,
        start_line: LSPLine,
        end_line: LSPLine | None = None,
    ) -> None:
        """Wait until PIDE has processed [start_line, end_line] (0-indexed).

        When *end_line* is None, waits for the single line *start_line*.
        """
        if end_line is None:
            end_line = start_line
        tracker = self._processing_trackers.get(file_path)
        if tracker is None:
            tracker = ProcessingTracker()
            self._processing_trackers[file_path] = tracker
        await tracker.wait_until_processed(
            start_line,
            end_line,
            health_check=lambda: self._check_server_health(self.STALL_TIMEOUT),
            check_interval=self.PROGRESS_CHECK_INTERVAL,
        )

    async def wait_for_processing_bounded(
        self,
        file_path: str,
        start_line: LSPLine,
        end_line: LSPLine,
        timeout: float,
    ) -> bool:
        """Wait until [start_line, end_line] is processed, or *timeout* expires.

        Returns True if the range was fully processed, False on timeout.
        """
        tracker = self._processing_trackers.get(file_path)
        if tracker is None:
            tracker = ProcessingTracker()
            self._processing_trackers[file_path] = tracker

        return await tracker.wait_until_processed_bounded(
            start_line,
            end_line,
            timeout=timeout,
            health_check=lambda: self._check_server_health(self.STALL_TIMEOUT),
            check_interval=self.PROGRESS_CHECK_INTERVAL,
        )

    async def request_theory_status(self) -> list[dict]:
        """Send PIDE/theory_status and return raw theory list."""
        result = await self.request("PIDE/theory_status", {})
        return result.get("theories", []) if isinstance(result, dict) else []

    async def cancel_execution(self) -> None:
        """Send PIDE/cancel_execution to atomically stop all processing."""
        await self.request("PIDE/cancel_execution", {})

    def get_all_running_commands(self) -> list[RunningCommand]:
        """Collect running commands from all tracked files with elapsed time and text."""
        now = time.monotonic()
        result: list[RunningCommand] = []
        for file_path, tracker in self._processing_trackers.items():
            doc = self.open_documents.get(file_path)
            if doc is None:
                continue
            lines = doc.content.split("\n")
            for sl, sc, el, ec, onset in tracker.get_running_ranges_with_onset():
                clipped = clip_line_range(sl, el, len(lines))
                if clipped is None:
                    continue
                sl, el_clamped = clipped
                ec_clamped = min(ec, len(lines[el_clamped]))
                if sl == el_clamped:
                    text = lines[sl][sc:ec_clamped]
                else:
                    parts = [lines[sl][sc:]]
                    for i in range(sl + 1, el_clamped):
                        parts.append(lines[i])
                    parts.append(lines[el_clamped][:ec_clamped])
                    text = "\n".join(parts)
                result.append(RunningCommand(
                    file_path=file_path,
                    start_line=sl + 1,
                    end_line=el_clamped + 1,
                    text=text,
                    elapsed_seconds=round(now - onset, 1),
                ))
        return result

    async def force_interrupt(self, file_path: str) -> None:
        """Cancel all processing via PIDE/cancel_execution and restrict perspective.

        Uses a three-step approach (verified 2026-05-27):
        1. PIDE/cancel_execution — global stop + interrupt all running threads
        2. Caret to line 0 — restrict perspective
        3. Single edit — trigger Document.update with restricted perspective

        The trailing space on line 0 is self-healing: we drop ``stat_sig`` so the
        next tool-call stat backstop (resync_changed_open_documents) re-reads from
        disk, sees the content mismatch, and didChanges back to the real file.
        """
        doc = self.open_documents.get(_canon(file_path))
        if doc is None:
            return
        await self.cancel_execution()
        await self.notify("PIDE/caret_update", {
            "uri": doc.uri, "line": 0, "character": 0, "focus": True,
        })
        first_line = doc.content.split("\n", 1)[0]
        doc.version += 1
        await self.notify("textDocument/didChange", {
            "textDocument": {"uri": doc.uri, "version": doc.version},
            "contentChanges": [{
                "range": {
                    "start": {"line": 0, "character": len(first_line)},
                    "end": {"line": 0, "character": len(first_line)},
                },
                "text": " ",
            }],
        })
        parts = doc.content.split("\n", 1)
        doc.content = parts[0] + " " + ("\n" + parts[1] if len(parts) > 1 else "")
        # The model now diverges from disk (synthetic space, never written out).
        # Drop the signature so the next stat backstop re-reads disk and heals it.
        doc.stat_sig = None
        note_edit_sent()

    def file_all_processed(self, file_path: str) -> bool:
        """True if the entire file has been processed (no unprocessed/running)."""
        tracker = self._processing_trackers.get(file_path)
        if tracker is None:
            return False
        return tracker.all_processed

    def get_processing_tracker(self, file_path: str) -> ProcessingTracker | None:
        """Return the ProcessingTracker for *file_path*, or None."""
        return self._processing_trackers.get(file_path)

    async def resync_changed_open_documents(self) -> None:
        """Tool-call backstop (Layer 2): re-stat every open doc; sync changed ones.

        Catches edits the event sources silently missed (inotify overflow, a
        non-hooked external editor, NFS, symlink/hardlink). The stat batch runs off
        the event loop so a slow/NFS mount cannot block it. Content comparison in
        :meth:`sync_dirty_files` is the final gate, so a bare metadata touch with no
        content change sends nothing.
        """
        paths = list(self.open_documents)
        if not paths:
            return
        sigs = await asyncio.to_thread(_stat_sigs, paths)
        changed: set[str] = set()
        for path, sig in sigs.items():
            doc = self.open_documents.get(path)
            if doc is not None and sig != doc.stat_sig:
                changed.add(path)
        if changed:
            await self.sync_dirty_files(changed)

    async def sync_dirty_files(self, dirty_paths: set[str]) -> None:
        """Re-sync the open editor documents among *dirty_paths* (didChange on change).

        Only editor-opened ``.thy`` documents (``open_documents``) are pushed here.
        Dependency files (``.ML`` blobs + imported ``.thy``) are the vscode_server's
        own File_Watcher's job, so a dirty dependency is simply ignored. Each synced
        path's ``stat_sig`` is refreshed so the Layer-2 backstop won't re-flag it.
        """
        for raw in dirty_paths:
            path = _canon(raw)
            doc = self.open_documents.get(path)
            if doc is None:
                continue
            try:
                # Unicode guard (off the event loop): may rewrite the file in
                # Isabelle ASCII; the returned text matches disk afterwards, so
                # the stat_sig refresh below stays coherent.
                content, guard_warning = await asyncio.to_thread(sanitize_read, path)
            except OSError:
                # Deleted/unreadable: drop the signature so a later recreate re-syncs.
                doc.stat_sig = None
                continue
            if guard_warning is not None:
                record_warning(path, guard_warning)
            if self.open_documents.get(path) is not doc:
                # Closed (or replaced) while we were off-loop: don't didChange it.
                continue
            if content != doc.content:
                doc.version += 1
                doc.content = content
                logger.info("Syncing dirty file: %s v%d", path, doc.version)
                await self.notify("textDocument/didChange", {
                    "textDocument": {"uri": doc.uri, "version": doc.version},
                    "contentChanges": [{"text": content}],
                })
                note_edit_sent()
            doc.stat_sig = _stat_sig(path)

    # ── Standard LSP queries ────────────────────────────────────────────

    async def get_hover(self, file_path: str, line: LSPLine, character: LSPCharacter) -> JsonDict | None:
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")
        result = await self.request("textDocument/hover", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })
        return result if isinstance(result, dict) or result is None else None

    async def get_command_at_position(
        self, file_path: str, line: LSPLine, character: LSPCharacter,
    ) -> tuple[str, JsonDict] | None:
        """Return (source, range) of the Isar command enclosing the position.

        Uses the patched PIDE/command_at_position request. range is the LSP range
        dict {start:{line,character}, end:{line,character}}. Returns None when no
        command is found at the position.
        """
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")
        result = await self.request("PIDE/command_at_position", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })
        if not isinstance(result, dict):
            return None
        source, rng = result.get("source"), result.get("range")
        if not isinstance(source, str) or not isinstance(rng, dict):
            return None
        return (source, rng)

    async def get_output_at_position(
        self, file_path: str, line: LSPLine, character: LSPCharacter,
    ) -> tuple[str, JsonDict, str] | None:
        """Return (source, range, output_html) of the command enclosing the position.

        Uses the patched PIDE/output_at_position request: a position-explicit query
        that renders the enclosing command's prover output without moving the caret
        (unlike dynamic_output, which only pushes on caret movement). range is the
        LSP range dict; output_html is the Output-panel HTML for the whole command.
        Returns None when no command is found at the position.
        """
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")
        result = await self.request("PIDE/output_at_position", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })
        if not isinstance(result, dict):
            return None
        source, rng, content = (
            result.get("source"), result.get("range"), result.get("content"),
        )
        if not isinstance(source, str) or not isinstance(rng, dict):
            return None
        if not isinstance(content, str):
            content = ""
        return (source, rng, content)

    async def get_completions(
        self,
        file_path: str,
        line: LSPLine,
        character: LSPCharacter,
    ) -> JsonDict | list[JsonDict] | None:
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")
        result = await self.request("textDocument/completion", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return result if isinstance(result, dict) or result is None else None

    async def get_definition(self, file_path: str, line: LSPLine, character: LSPCharacter) -> Any | None:
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")
        return await self.request("textDocument/definition", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })

    async def get_highlights(self, file_path: str, line: LSPLine, character: LSPCharacter) -> list[JsonDict] | None:
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")
        result = await self.request("textDocument/documentHighlight", {
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
        })
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        return None

    def get_cached_diagnostics(self, file_path: str) -> list[dict]:
        return self.diagnostic_cache.diagnostics.get(file_path, [])

    def diagnostics_settled(self, file_path: str, settle_time: float = 1.0) -> bool:
        """True when no new publishDiagnostics arrived in the last *settle_time* seconds."""
        last = self.diagnostic_cache.last_update.get(file_path)
        if last is None:
            return False
        return (time.time() - last) > settle_time

    # ── PIDE extension queries ──────────────────────────────────────────

    async def get_goals_at_position(
        self, file_path: str, line: LSPLine, character: int,
    ) -> list[str]:
        """Get proof goals at a position using PIDE state panels.

        Terminal proof commands (``by``, ``done``, ``qed``) produce empty
        proof state — Isabelle's state panel sends no ``state_output`` for
        them.  We detect this via STATE_OUTPUT_GRACE: if the server stays
        active but no output arrives within that window, return ``[]``.
        """
        uri = file_path_to_uri(file_path)
        panel_id: int | None = None

        init_future: asyncio.Future[tuple[int, str]] = (
            asyncio.get_running_loop().create_future()
        )

        try:
            async with self._caret_lock:
                await self.notify("PIDE/caret_update", {
                    "uri": uri, "line": line, "character": character, "focus": True,
                })
                await asyncio.sleep(0.15)

                self._state_init_waiters.append(init_future)
                # Isabelle2025 turned PIDE/state_init from a notification into a
                # *request* (it replies with the new panel's state_id). Sent as a
                # plain notification on 2025+, the panel is never created, no
                # state_output arrives, and goals come back empty. The waiter above
                # still captures the state_output (a notification in both versions);
                # we only need the request to actually build the panel. Pre-2025
                # keeps the notification form. Undetected version → assume pre-2025.
                year = isabelle_year()
                if year is not None and year >= 2025:
                    await self.request("PIDE/state_init", {}, timeout=30.0)
                else:
                    await self.notify("PIDE/state_init", {})

                try:
                    result = await self._wait_for_state_output(
                        init_future, file_path,
                    )
                except IsabelleToolError:
                    raise self._enrich_timeout_error(file_path)

            if result is None:
                return []
            panel_id, html = result
            return parse_goals_from_html(html)

        finally:
            # Remove the waiter on EVERY exit path — including CancelledError, which
            # the inner `except IsabelleToolError` does not catch. _wait_for_state_output
            # awaits asyncio.shield(future), so a cancelled await leaves the future
            # pending and still in the list; a leaked waiter would later be resolved
            # FIFO by an unrelated state_output. (suppress: already removed on the
            # grace/normal paths.)
            with contextlib.suppress(ValueError):
                self._state_init_waiters.remove(init_future)
            if panel_id is not None:
                with contextlib.suppress(IsabelleToolError):
                    await self.notify("PIDE/state_exit", {"id": panel_id})

    async def _wait_for_state_output(
        self,
        future: asyncio.Future[tuple[int, str]],
        file_path: str,
    ) -> tuple[int, str] | None:
        """Wait for state_output with empty-proof-state detection.

        Returns None when the server is active but no state_output arrives
        within STATE_OUTPUT_GRACE — the command has no proof state to show.
        """
        start = time.time()
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), timeout=self.PROGRESS_CHECK_INTERVAL,
                )
            except asyncio.TimeoutError:
                if future.done():
                    return future.result()
                self._check_server_health(self.STALL_TIMEOUT)
                elapsed = time.time() - start
                process_alive = (
                    self.process is not None
                    and self.process.returncode is None
                )
                if elapsed > self.STATE_OUTPUT_GRACE and process_alive:
                    with contextlib.suppress(ValueError):
                        self._state_init_waiters.remove(future)
                    logger.debug(
                        "No state_output after %.1fs (server active) — "
                        "empty proof state at %s",
                        elapsed, file_path,
                    )
                    return None

    async def get_find_theorems_at_position(
        self, file_path: str, line: LSPLine, character: int,
        query: str, limit: str, allow_dups: str,
    ) -> str | None:
        """Run find_theorems at a position via the PIDE/find_theorems query.

        Sets the caret (the query runs in that command's context), sends the
        request, and awaits the rendered output carried by the `finished` status.
        Returns the output HTML, or None when there is no command at the position
        (or the command was removed before any output was produced).
        """
        uri = file_path_to_uri(file_path)
        future: asyncio.Future[str | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._find_theorems_seq += 1
        token = str(self._find_theorems_seq)
        try:
            async with self._caret_lock:
                await self.notify("PIDE/caret_update", {
                    "uri": uri, "line": line, "character": character, "focus": True,
                })
                await asyncio.sleep(0.15)

                # Stamp this query's token and reset the per-query buffer BEFORE
                # issuing the request, so a straggler from a prior query (different
                # token) is ignored and no stale result can be read back.
                self._find_theorems_token = token
                self._find_theorems_output = None
                self._find_theorems_waiters.append(future)
                await self.notify("PIDE/find_theorems_request", {
                    "token": token, "query": query, "limit": limit, "allow_dups": allow_dups,
                })

                try:
                    return await self._wait_for_find_theorems_output(future, file_path)
                except IsabelleToolError:
                    raise self._enrich_timeout_error(file_path)
        finally:
            # Remove the waiter on EVERY exit path (incl. CancelledError, which the
            # inner except does not catch and which leaves the shielded future
            # pending). Then tear down any still-active server overlay; the cancel is
            # token-guarded on the Scala side, so a late cancel for a query that has
            # already been superseded by the next one is a no-op (no cross-query
            # cancellation).
            with contextlib.suppress(ValueError):
                self._find_theorems_waiters.remove(future)
            with contextlib.suppress(IsabelleToolError):
                await self.notify("PIDE/find_theorems_cancel", {"token": token})

    async def _wait_for_find_theorems_output(
        self,
        future: asyncio.Future[str | None],
        file_path: str,
    ) -> str | None:
        """Wait for the find_theorems `finished` status (clone of
        _wait_for_state_output, but with the longer FIND_THEOREMS_OUTPUT_GRACE).
        Returns the buffered output, or None if the server stays active but never
        reports finished within that grace."""
        start = time.time()
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), timeout=self.PROGRESS_CHECK_INTERVAL,
                )
            except asyncio.TimeoutError:
                if future.done():
                    return future.result()
                self._check_server_health(self.STALL_TIMEOUT)
                elapsed = time.time() - start
                process_alive = (
                    self.process is not None
                    and self.process.returncode is None
                )
                if elapsed > self.FIND_THEOREMS_OUTPUT_GRACE and process_alive:
                    logger.debug(
                        "No find_theorems finished after %.1fs (server active) at %s",
                        elapsed, file_path,
                    )
                    return None

    def _enrich_timeout_error(self, file_path: str) -> IsabelleToolError:
        diags = self.diagnostic_cache.diagnostics.get(file_path, [])
        errors = [
            d.get("message", "")
            for d in diags
            if isinstance(d, dict) and d.get("severity") in (1, 2)
        ]
        if errors:
            summary = "; ".join(errors[:3])
            if len(errors) > 3:
                summary += f" (+{len(errors) - 3} more)"
            return IsabelleToolError(
                f"Timed out waiting for proof state. "
                f"File has {len(errors)} error(s): {summary}"
            )
        if not diags:
            return IsabelleToolError(
                "Timed out waiting for proof state. "
                "No diagnostics received — file may not have been processed."
            )
        return IsabelleToolError("Timed out waiting for proof state.")

    async def get_dynamic_output(
        self, file_path: str, line: LSPLine, character: int = 0,
    ) -> str:
        """Get dynamic output at position (progress-monitored).

        Holds the caret lock for the duration since dynamic output depends
        on the current caret position (unlike state panels which bind an
        overlay to a specific command). Returns cached/empty output once the
        file appears fully processed with no output at this position.
        """
        uri = file_path_to_uri(file_path)
        key = (file_path, line, character)

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        waiter = (key, future)

        try:
            async with self._caret_lock:
                self._dynamic_output_waiters.append(waiter)
                await self.notify("PIDE/caret_update", {
                    "uri": uri, "line": line, "character": character,
                })
                while True:
                    try:
                        return await asyncio.wait_for(
                            asyncio.shield(future), timeout=self.PROGRESS_CHECK_INTERVAL,
                        )
                    except asyncio.TimeoutError:
                        if future.done():
                            return future.result()
                        self._check_server_health(self.STALL_TIMEOUT)
                        if self.diagnostics_settled(file_path, settle_time=3.0):
                            return self._dynamic_output_cache_by_position.get(key, "")
        finally:
            with contextlib.suppress(ValueError):
                self._dynamic_output_waiters.remove(waiter)

    async def request_preview(
        self, file_path: str, column: int = 0,
    ) -> JsonDict:
        """Request document preview (progress-monitored, no fixed timeout)."""
        uri = file_path_to_uri(file_path)
        key = (uri, column)
        async with self._preview_lock:
            future: asyncio.Future[JsonDict] = asyncio.get_running_loop().create_future()
            self._preview_waiters[key] = future

            try:
                await self.notify("PIDE/preview_request", {"uri": uri, "column": column})
                return await self._wait_with_progress(future)
            finally:
                self._preview_waiters.pop(key, None)
