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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from isabelle_mcp import query
from isabelle_mcp.document_diff import ranged_content_changes
from isabelle_mcp.models import RunningCommand, TheoryStatus, TheoryStatusRecord
from isabelle_mcp.query import QueryReply
from isabelle_mcp.processing import (
    FreshnessState,
    ProcessingTracker,
    clip_line_range,
    parse_decoration_ranges,
)
from isabelle_mcp.unicode_guard import record_warning, sanitize_read
from isabelle_mcp.component import ensure_component
from isabelle_mcp.utils import (
    IsabelleCatastrophe,
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    file_path_to_uri,
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


# The client<->server wire version. The server emits its own as a top-level field
# of the initialize reply (Language_Server.protocol_version); a jar without the
# field is version 0. Bumped whenever the wire changes; independent of __version__.
PROTOCOL_VERSION: int = 1

# Agent-visible, approved verbatim (plan D-G): the launch refusal on a mismatch.
PROTOCOL_MISMATCH_MESSAGE = (
    "Isabelle-MCP's Scala component does not match this isabelle-mcp package. "
    "Run `isabelle-mcp install` to update it."
)

# Backstop for PIDE/cancel_evaluation's REPLY: the server's own budget is 120 s
# from the moment its worker thread starts; the 15 s on top cover queueing in the
# server's dispatch loop. Hitting it is the catastrophe (the prover is torn
# down), so it is not an envelope the server must meet but an independent gate.
# The send is bounded separately by SEND_TIMEOUT, so one force_interrupt takes at
# most 135 + 30 s; the lock in evaluation.cancel_evaluation is held for that plus
# the bounded wrap-up (teardown bounds, per-file close budget, the debugger sweep).
CANCEL_REQUEST_TIMEOUT: float = 135.0

# The same shape for PIDE/flush (server budget 120 s + 15 s), and for the same
# reason a HARD timeout rather than the progress-monitored default: the flush's
# wait for an assignment is silent by construction (the unassigned change makes
# every snapshot outdated, so the server publishes nothing meanwhile), and the
# progress monitor's stall clock counts silence accumulated BEFORE the request.
# Inside the budget the client sees the server's reasoned LSP error; past it an
# IsabelleToolError — a flush parked in one of the server's untimed manager
# round trips ends here, never in a hang.
FLUSH_REQUEST_TIMEOUT: float = 135.0

# Bound on one wire write (taking the write lock included): a server that does not
# drain its stdin is not coming back, and cancel_evaluation holds the evaluation
# lock across every send, so no send may hang.
SEND_TIMEOUT: float = 30.0

# Bound on the bookkeeping steps of a teardown (reader-task cancellation, the
# process wait after a kill).
TEARDOWN_STEP_TIMEOUT: float = 5.0


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


# D-C7: modifying a heap-precompiled file is refused outright, with the one
# way out. Raised by the sync path on detection and by evaluate_to's
# heap-abandon branch (a modification that predates the open).
PRECOMPILED_MODIFIED_ERROR = (
    "{file} is precompiled into the running session '{logic}'. Modifying it "
    "is not supported: your change has not taken effect, and the prover "
    "still uses the old version compiled into the heap. If you really want "
    "to modify this file and have Isabelle-MCP evaluate it, relaunch via "
    "isabelle_launch with a base session that does not include it."
)


def _read_text(path: str) -> str:
    """Plain disk read, no unicode-guard rewrite — blocking, call via to_thread."""
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


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


def parse_theory_status(raw: dict) -> TheoryStatus:
    """One PIDE/theory_status row. The one entry of prover paths into the Python
    side: ``node_name`` leaves here canonical (:func:`_canon`), so every map keyed
    by it and every comparison against an ``open_documents`` key agrees with the
    client's own keying — a symlinked node_name never splits one file into two.

    An empty node (a theory with no file) stays empty: ``os.path.realpath("")``
    is the current directory, and the truth tests on ``node_name`` rely on the
    empty string.
    """
    node = raw.get("node_name", "")
    return TheoryStatus(
        node_name=_canon(node) if node else "",
        theory_name=raw.get("theory_name", ""),
        external=raw.get("external", False),
        imports=[imp["theory_name"] for imp in raw.get("imports", [])],
        ok=raw.get("ok", True),
        total=raw.get("total", 0),
        unprocessed=raw.get("unprocessed", 0),
        running=raw.get("running", 0),
        warned=raw.get("warned", 0),
        failed=raw.get("failed", 0),
        finished=raw.get("finished", 0),
        canceled=raw.get("canceled", False),
        consolidated=raw.get("consolidated", False),
        percentage=raw.get("percentage", 0),
    )


def _reply_document_version(result: Any, request: str) -> int:
    """The document version a reply carries. After the protocol handshake the
    key is required; a reply without it is the catastrophe (raised on the tool
    task, where the middleware sees it — a plain assert would be masked)."""
    version = result.get("document_version") if isinstance(result, dict) else None
    if not isinstance(version, int):
        raise IsabelleCatastrophe(f"{request} replied without document_version: {result!r}")
    return version


@dataclass
class DocumentState:
    file_path: str
    uri: str
    version: int
    content: str
    language_id: str = "isabelle"
    # Last on-disk signature we synced to the server. ``None`` forces a re-read on
    # the next stat backstop.
    stat_sig: StatSig | None = None
    # Set when the server may hold text that diverged from ``content`` (it rejected
    # a didChange, which is a silent drop server-side): the next sync must push the
    # FULL text, because a ranged diff against a wrong base corrupts the document.
    needs_full_sync: bool = False
    # The evaluation-target mark: the file was evaluated by isabelle_evaluate_to,
    # or reopened for a query after the unified close had closed it. It only ever
    # becomes True (open_document: ``doc.is_evaluation_target or evaluation_target``)
    # and dies with the record when the session state is cleared — the unified
    # close, the sole closer, never closes a marked document, so "closed but still
    # marked" is not representable.
    is_evaluation_target: bool = False


@dataclass
class DiagnosticCache:
    diagnostics: dict[str, list[dict]] = field(default_factory=dict)
    last_update: dict[str, float] = field(default_factory=dict)


class IsabelleLSPClient:
    """Manages the lifecycle of `isabelle vscode_server` and JSON-RPC 2.0 communication."""

    STALL_TIMEOUT: ClassVar[float] = 120.0
    PROGRESS_CHECK_INTERVAL: ClassVar[float] = 5.0
    # The prover-side backstop for a position-explicit query. It is not the
    # policy: the client's own wait is progress-monitored rather than
    # deadline-bound (see request()). This is the point at which a reply that
    # never comes stops holding a prover-side table entry and an ML task, so it
    # is generous on purpose.
    QUERY_BACKSTOP: ClassVar[float] = 600.0

    def __init__(
        self,
        logic: str = "HOL",
        session_dirs: list[str] | None = None,
        verbose: bool = False,
        extra_args: list[str] | None = None,
        project_root: str | None = None,
        debug: bool = False,
    ):
        self.logic = logic
        self.session_dirs = session_dirs or []
        self.verbose = verbose
        self.extra_args = extra_args or []
        # ML debugger instrumentation (design section 2.1): when set, start()
        # spawns the server with `-o ML_debugger=true`, so newly compiled ML is
        # breakable. Part of the launch identity — a differing debug value
        # makes isabelle_launch restart the prover.
        self.debug = debug
        # Real paths of every source file precompiled into the running logic's
        # heap chain (filled by enumerate_heap_sources at launch). Such files
        # cannot be edited: PIDE ignores their changes and never reprocesses
        # them, so the sync path refuses such edits outright (D-C7) instead of
        # silently wedging.
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
        # The freshness state (processing.FreshnessState): the newest document
        # version seen, the two content counters, and the ONE client-level
        # condition every freshness wait parks on. Shared by reference with every
        # tracker this client builds; reset with the session.
        self.freshness = FreshnessState()

        # Optional FileWatcher (set by the server). open_document/close_document
        # register/deregister the file's parent directory for event-driven sync.
        self.file_watcher: Any = None

        # Correlation token for position-explicit queries: monotonic, and what a
        # PIDE/query_cancel names.
        self._query_seq: int = 0

        # PIDE preview
        self._preview_lock = asyncio.Lock()
        self._preview_waiters: dict[tuple[str, int], asyncio.Future[JsonDict]] = {}

        # PIDE processing status (from PIDE/decoration)
        self._processing_trackers: dict[str, ProcessingTracker] = {}

        # ML debugger (PIDE/debugger_state, PIDE/debugger_output). The state
        # notification always carries the FULL current thread map — a thread's
        # absence means it resumed — so debugger_threads is a plain replace.
        # The histories keep every raw notification for the wait helper and the
        # Phase A probes; Phase C consumes them into hit reports and notices.
        self.debugger_threads: dict[str, list[dict[str, Any]]] = {}
        self.debugger_state_history: list[dict[str, Any]] = []
        self.debugger_output_history: list[dict[str, Any]] = []
        self._debugger_event = asyncio.Event()

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
        # "Bad JSON value"). No tool consumes those panels any more, but the server still
        # RUNS Dynamic_Output and pushes on every caret move (evaluate_to makes one),
        # so leaving the broken branch enabled would put errors on the wire for output
        # nobody reads. The option does not exist pre-2025 (passing it aborts the
        # server), so gate it.
        if (isabelle_year() or 0) >= 2025:
            cmd += ["-o", "vscode_html_output=true"]
        if self.debug:
            # Not a build-identity option: it invalidates no heap, and only
            # code compiled AFTER launch gets debug instrumentation.
            cmd += ["-o", "ML_debugger=true"]
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
        # The wire version, before anything else is said to the server: a
        # mismatched jar (a stock one, or one built from another revision)
        # would answer without stamps, and every freshness wait would end in
        # the catastrophe. A reply without the field is version 0.
        if result.get("protocol_version", 0) != PROTOCOL_VERSION:
            raise IsabelleToolError(PROTOCOL_MISMATCH_MESSAGE)
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
        await self._cancel_background_tasks_bounded()
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
                await self.notify("exit", {})   # bounded by _send
            await self._cancel_background_tasks_bounded()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # Group kill: killing just the bash wrapper would leave the
                # java child holding the pipes and this wait would never end.
                _kill_process_tree(self.process)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self.process.wait(), timeout=TEARDOWN_STEP_TIMEOUT)

        self._clear_session_state()

    async def teardown(
        self, reason: str = "The Isabelle session was terminated.",
    ) -> None:
        """Tear the prover down without taking any lock. isabelle_terminate, a
        relaunch, server shutdown and the catastrophe handler all share this so
        they cannot drift, and its guarantees are layered so a failure inside it cannot
        undo them: every in-flight waiter is failed with *reason* first (no
        request may outlive its prover); whatever shutdown() does, the process
        is killed and forgotten; the remaining bookkeeping is best-effort and
        only logged -- the next launch clears session state again anyway.
        Every await inside is bounded (shutdown's segments)."""
        self._fail_pending_waiters(IsabelleToolError(reason))
        try:
            await self.shutdown()
        except Exception:
            logger.error("teardown: shutdown raised; killing the process regardless",
                         exc_info=True)
        finally:
            self.kill()                    # no-op once the process is gone
            self.process = None
            try:
                self._clear_session_state()   # idempotent; shutdown() ran it on success
            except Exception:
                logger.error("teardown: clearing session state failed", exc_info=True)
            try:
                if self.file_watcher is not None:
                    self.file_watcher.clear_watches()
            except Exception:
                logger.error("teardown: clearing file watches failed", exc_info=True)

    def _clear_session_state(self) -> None:
        self._handshake_done = False
        self.open_documents.clear()
        self.pending_requests.clear()
        self.diagnostic_cache.diagnostics.clear()
        self.diagnostic_cache.last_update.clear()
        self._preview_waiters.clear()
        self._processing_trackers.clear()
        self.freshness.reset()
        # Debugger state must not survive its prover: thread names restart
        # their counter with each prover process, so a stale map would show
        # phantom stopped threads. The registry retires every hit ("the
        # prover was terminated") and demotes armed entries to pending.
        self.debugger_threads.clear()
        self.debugger_state_history.clear()
        self.debugger_output_history.clear()
        from isabelle_mcp.debugger import registry
        registry.on_prover_teardown(self)

        # Reset the module-global evaluation singleton so a later relaunch starts
        # clean — otherwise a terminate mid-evaluation leaves evaluation_state.active
        # True and the next session rejects every evaluate_to. Lazy import avoids the
        # import cycle with evaluation.py (which imports this module).
        from isabelle_mcp.evaluation import evaluation_state
        evaluation_state.cancel()

    # ── JSON-RPC transport ──────────────────────────────────────────────

    async def request(
        self, method: str, params: dict[str, Any], timeout: float | None = None,
        *, on_write: Callable[[], None] | None = None,
    ) -> Any:
        """Send an LSP request and wait for the response.

        When timeout is None (default), uses progress monitoring — no fixed
        timeout, but raises IsabelleToolError if the server stalls or crashes.
        When timeout is set, uses a hard deadline (for lifecycle methods like
        initialize/shutdown). *on_write* runs inside the request's own write
        section (see :meth:`_send`).
        """
        self.request_id += 1
        req_id = self.request_id
        message = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}

        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = future

        try:
            await self._send(message, on_write=on_write)
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

    async def notify(
        self, method: str, params: dict[str, Any], *, content: bool = False,
    ) -> None:
        """*content* marks the two messages that change the prover's text —
        didOpen and didChange — so :meth:`_send` counts them as unflushed content."""
        await self._send({"jsonrpc": "2.0", "method": method, "params": params},
                         content=content)

    async def _send(
        self, message: JsonDict, *, content: bool = False,
        on_write: Callable[[], None] | None = None,
    ) -> None:
        """Write one frame. Inside the write section, right after the bytes are
        written, a content message is counted in ``freshness.content_sends`` and
        *on_write* runs — so "counted" and "written before this request" are the
        same set by construction: the flush request takes its snapshot of the
        counter through *on_write*, and a didOpen counted before the snapshot
        was written before the flush. A content send then notifies the freshness
        condition as the LAST act, outside the lock and the timed section (a
        wait parked on the condition re-arms its flush from it)."""
        if not self.process or not self.process.stdin:
            raise IsabelleToolError("LSP process not running")
        stdin = self.process.stdin
        _wire_dump("out", message)
        payload = json.dumps(message).encode('utf-8')
        header = f"Content-Length: {len(payload)}\r\n\r\n".encode('ascii')

        async def write() -> None:
            async with self._write_lock:
                try:
                    stdin.write(header + payload)
                    if content:
                        self.freshness.content_sends += 1
                    if on_write is not None:
                        on_write()
                    await stdin.drain()
                except (BrokenPipeError, ConnectionError, OSError) as exc:
                    raise IsabelleToolError("Failed to write to LSP process") from exc

        # Bounded: notify() and close_document come straight here, and
        # cancel_evaluation holds the evaluation lock across every send.
        try:
            await asyncio.wait_for(write(), timeout=SEND_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise IsabelleToolError(
                f"LSP write did not complete within {SEND_TIMEOUT:g}s") from exc
        if content:
            await self.freshness.notify()

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

    async def _cancel_background_tasks_bounded(self) -> None:
        """_cancel_background_tasks with a bound: a teardown must not hang on a
        reader that ignores its cancellation."""
        try:
            await asyncio.wait_for(
                self._cancel_background_tasks(), timeout=TEARDOWN_STEP_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("reader tasks did not stop within %ss", TEARDOWN_STEP_TIMEOUT)
            self.reader_task = None
            self.stderr_task = None

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
        elif method == "PIDE/decoration":
            await self._handle_decoration(params)
        elif method == "PIDE/preview_response":
            self._handle_preview_response(params)
        elif method == "PIDE/debugger_state":
            self._handle_debugger_state(params)
        elif method == "PIDE/debugger_output":
            self._handle_debugger_output(params)
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
            if "Failed to apply document change" in text:
                # The server rejected a didChange and DROPPED it (no reply
                # channel), while our model already committed the new text --
                # silent divergence.  Drop the signatures so the next stat
                # backstop re-syncs, and force the
                # full-text form, since a ranged diff against the server's
                # unknown base would corrupt the document further.
                for doc in self.open_documents.values():
                    doc.stat_sig = None
                    doc.needs_full_sync = True
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
        # The picture stamp is a required key after the handshake. A push without
        # it is DROPPED with one ERROR line (an exception here would be swallowed
        # and kill the reader): the file then never becomes fresh, and the next
        # freshness wait ends in the catastrophe at its bound.
        document_version = params.get("document_version")
        if not isinstance(document_version, int):
            logger.error("dropped a PIDE/decoration push without document_version for %s",
                         params.get("uri"))
            return
        # The FIRST act, ahead of every other early return: every stamped
        # message advances the newest version — acknowledgement pushes and
        # pushes for files no longer open included.
        self.freshness.advance(document_version)
        uri = params.get("uri", "")
        if not isinstance(uri, str) or not uri.startswith("file://"):
            return
        entries = params.get("entries")
        if not isinstance(entries, list):
            return
        # Canonical key, like open_documents: the getters compare the two.
        file_path = _canon(uri_to_file_path(uri))
        if file_path not in self.open_documents:
            return
        # Every push for an open document is folded; whether it initializes the
        # tracker is the tracker's own rule (ProcessingTracker.update, I-5).
        await self._tracker_for(file_path).update(parse_decoration_ranges(entries),
                                                  document_version)
        await self.freshness.notify()

    def _tracker_for(self, file_path: str) -> ProcessingTracker:
        """The tracker of an open document, built with the client's freshness
        state on first use — the only constructor site inside the client."""
        tracker = self._processing_trackers.get(file_path)
        if tracker is None:
            tracker = ProcessingTracker(self.freshness)
            self._processing_trackers[file_path] = tracker
        return tracker

    def _handle_debugger_state(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        entries = params.get("threads")
        if not isinstance(entries, list):
            return
        threads: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("thread"), str):
                stack = entry.get("stack")
                threads[entry["thread"]] = stack if isinstance(stack, list) else []
        self.debugger_threads = threads
        self.debugger_state_history.append(params)
        self._debugger_event.set()

    def _handle_debugger_output(self, params: Any) -> None:
        if not isinstance(params, dict):
            return
        self.debugger_output_history.append(params)
        self._debugger_event.set()

    async def wait_debugger_event(
        self, predicate: Any, timeout: float = 60.0
    ) -> bool:
        """Wait until predicate(self) is true, re-checking on every debugger
        notification. Waits generously by default (probe policy: >= 60s before
        concluding a negative). Returns False on timeout."""
        deadline = time.time() + timeout
        while True:
            if predicate(self):
                return True
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            self._debugger_event.clear()
            if predicate(self):  # re-check: a notification may have landed meanwhile
                return True
            try:
                await asyncio.wait_for(self._debugger_event.wait(), timeout=remaining)
            except TimeoutError:
                return False

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
        futures.extend(self._preview_waiters.values())
        return futures

    def _fail_pending_waiters(self, exc: Exception) -> None:
        for future in self._all_waiters():
            if not future.done():
                future.set_exception(exc)
        self.pending_requests.clear()
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
        evaluation_target: bool = False,
    ) -> None:
        """Ensure *file_path* is open (didOpen once); never re-sync content here.

        For an already-open document this returns immediately — it does NOT re-read
        disk, bump the version, or send didChange. All content syncing is owned by
        the locked sync paths (:meth:`resync_changed_open_documents` /
        :meth:`sync_dirty_files`), which run via the tool-call backstop before any
        ``open_document`` in a tool body. This removes the only unlocked didChange
        path and the version race it caused.

        *evaluation_target* sets the document's evaluation-target mark
        (:attr:`DocumentState.is_evaluation_target`). Every exit only ever raises
        the mark, never lowers it: an auto-open of an already-marked file passes
        the default False and must not erase the mark.

        Registers, sends the didOpen (counted as unflushed content in _send) and
        returns; it does not wait. A caller that opened a batch of files does ONE
        ``wait_until_fresh`` over them before reading their pictures.
        """
        file_path = _canon(file_path)

        doc = self.open_documents.get(file_path)
        if doc is not None:
            doc.is_evaluation_target = doc.is_evaluation_target or evaluation_target
            return

        if content is None:
            # Unicode guard (off the event loop): may rewrite the file in
            # Isabelle ASCII; the returned text matches disk afterwards, so the
            # stat_sig taken below stays coherent. Caller-passed content (no
            # current callers) bypasses the guard — it has no disk counterpart
            # to keep in sync.
            text, guard_warning = await asyncio.to_thread(sanitize_read, file_path)
            content = text
            if guard_warning is not None:
                record_warning(file_path, guard_warning)
            doc = self.open_documents.get(file_path)
            if doc is not None:
                # Another coroutine opened it while we were off-loop.
                doc.is_evaluation_target = doc.is_evaluation_target or evaluation_target
                return
        # One stat, after the read (hence after any guard rewrite) and before
        # the didOpen: it serves the DocumentState.
        stat_sig = _stat_sig(file_path)

        uri = file_path_to_uri(file_path)

        # Register in open_documents BEFORE didOpen: notify -> _send awaits stdin.drain(),
        # a cancel checkpoint. If registration lagged the didOpen, a re-delivered cancel
        # there would leave the server holding the doc with no open_documents entry, so
        # close_document (which pops that dict) could never send the matching didClose —
        # an orphan. Registering first keeps the two in sync under cancellation.
        self.open_documents[file_path] = DocumentState(
            file_path=file_path, uri=uri, version=1, content=content,
            stat_sig=stat_sig, is_evaluation_target=evaluation_target,
        )
        await self.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": "isabelle",
                "version": 1,
                "text": content,
            }
        }, content=True)
        self._add_file_watch(file_path)

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
        doc = self.open_documents.get(file_path)
        if doc is None:
            return
        # The unified close (evaluation.close_settled_documents) is the only
        # closer, and it never closes a marked document; the mark's lifetime
        # argument rests on that. A second closer that reaches here with a
        # marked record is a bug, not a policy choice.
        assert not doc.is_evaluation_target, f"closing an evaluation target: {file_path}"
        del self.open_documents[file_path]
        await self.notify("textDocument/didClose", {"textDocument": {"uri": doc.uri}})
        self.diagnostic_cache.diagnostics.pop(file_path, None)
        self.diagnostic_cache.last_update.pop(file_path, None)
        tracker = self._processing_trackers.pop(file_path, None)
        if tracker is not None:
            await tracker.reset()
        self._remove_file_watch(file_path)
        # a parked freshness wait re-evaluates its wait set: the closed file leaves it
        await self.freshness.notify()

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
        await self._tracker_for(file_path).wait_until_processed(
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
        return await self._tracker_for(file_path).wait_until_processed_bounded(
            start_line,
            end_line,
            timeout=timeout,
            health_check=lambda: self._check_server_health(self.STALL_TIMEOUT),
            check_interval=self.PROGRESS_CHECK_INTERVAL,
        )

    async def request_theory_status(self) -> TheoryStatusRecord:
        """One PIDE/theory_status: the stamped, frozen record of its reply. The
        single place a theory_status reply advances the newest version."""
        result = await self.request("PIDE/theory_status", {})
        document_version = _reply_document_version(result, "PIDE/theory_status")
        self.freshness.advance(document_version)
        rows = result.get("theories", [])
        return TheoryStatusRecord(
            document_version=document_version,
            theories=tuple(parse_theory_status(t) for t in rows if isinstance(t, dict)),
        )

    async def flush(self, *, resync_dependencies: bool) -> tuple[JsonDict, int]:
        """One PIDE/flush: "absorb everything I have sent, re-read the dependency
        files if asked, and name an assigned version that contains it all".

        Returns the reply and the snapshot of ``content_sends`` taken in the
        request's own write section: everything counted up to it was written
        before the request, so the reply version covers it (I-1b) and
        ``content_sends_flushed`` is raised to it — a monotone maximum, never an
        assignment, since two flushes can be in flight. The reply's
        ``changed_uris`` (dependency files whose bytes differed or that no
        longer read) are converted to paths and marked dirty in the breakpoint
        registry. A stampless reply is the catastrophe. Never awaited under the
        evaluation lock (I-8).
        """
        from isabelle_mcp.evaluation import _evaluation_state_lock
        if _evaluation_state_lock.held.get():
            raise RuntimeError("I-8: PIDE/flush awaited under _evaluation_state_lock")
        snapshot = 0

        def on_write() -> None:
            nonlocal snapshot
            snapshot = self.freshness.content_sends

        result = await self.request(
            "PIDE/flush", {"resync_dependencies": resync_dependencies},
            timeout=FLUSH_REQUEST_TIMEOUT, on_write=on_write)
        document_version = _reply_document_version(result, "PIDE/flush")
        self.freshness.advance(document_version)
        self.freshness.content_sends_flushed = max(
            self.freshness.content_sends_flushed, snapshot)
        changed = result.get("changed_uris", [])
        if isinstance(changed, list) and changed:
            # Lazy import: debugger.py imports this module at its top.
            from isabelle_mcp.debugger import registry as _bp_registry
            for uri in changed:
                if isinstance(uri, str):
                    _bp_registry.mark_dirty(uri_to_file_path(uri))
        await self.freshness.notify()
        return result, snapshot

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

    async def request_loaders(self, file_path: str) -> list[dict[str, Any]]:
        """The load commands (ML_file and kin) that load *file_path*, each with
        ``file``, ``theory``, ``command``, ``state`` (unevaluated | running |
        evaluated) and, when the loading theory is open, ``line``. Empty
        until the loading theory is in the document model."""
        result = await self.request(
            "PIDE/loaders", {"uri": file_path_to_uri(file_path)}, timeout=10.0)
        loaders = result.get("loaders") if isinstance(result, dict) else None
        return [x for x in loaders if isinstance(x, dict)] if loaders else []

    async def force_interrupt(self) -> dict[str, Any]:
        """One PIDE/cancel_evaluation request; returns its reply payload.

        The server does the whole cancellation itself -- stanch the prover,
        retract every perspective, retire each interrupted command with a
        zero-length edit until none is left -- within its own 120 s budget, and
        answers once with one of two success outcomes: retired, nothing_running
        (see ``evaluation.CANCEL_OUTCOME_*``).  Nothing here touches the
        document model: the text on the prover is byte-identical afterwards.
        Everything else -- the server's aborted outcome, a timeout, a transport
        failure, a reply outside the contract -- is the catastrophe: it raises
        IsabelleCatastrophe, and the tool boundary terminates the session.
        """
        try:
            result = await self.request(
                "PIDE/cancel_evaluation", {}, timeout=CANCEL_REQUEST_TIMEOUT)
        except IsabelleToolError as exc:
            raise IsabelleCatastrophe(f"cancel request failed: {exc}") from exc
        # The payload contract is enforced here, the only place the value comes
        # from outside the process.
        if not isinstance(result, dict) or result.get("outcome") not in (
                "retired", "nothing_running"):
            reason = (
                result.get("reason", "no reason given")
                if isinstance(result, dict) and result.get("outcome") == "aborted"
                else f"unexpected reply {result!r}"
            )
            raise IsabelleCatastrophe(f"cancellation aborted: {reason}")
        # Retirement re-mints command ids and retraction empties the perspective:
        # the reply version names an assigned version containing those edits, and
        # every picture older than it is unfresh until its push arrives.
        self.freshness.advance(_reply_document_version(result, "PIDE/cancel_evaluation"))
        return result

    def file_all_processed(self, file_path: str) -> bool:
        """True if the entire file has been processed (no unprocessed/running).

        No production caller today; kept as the second reader of the tracker
        dict so it inherits the same open check as :meth:`get_processing_tracker`.
        """
        tracker = self.get_processing_tracker(file_path)
        if tracker is None:
            return False
        return tracker.all_processed

    def get_processing_tracker(self, file_path: str) -> ProcessingTracker | None:
        """Return the ProcessingTracker for *file_path*, or None.

        A tracker is readable only while its document is open (same key form
        as ``open_documents``): closing a document drops its tracker, and a
        differential push for a closed file (the server's erase push after
        didClose) builds none — see _handle_decoration. Answering None for a
        closed document here means every reader, present and future, says
        "nothing known" whatever the tracker map holds.

        ``open_documents`` never holds a false value (its one writer stores a
        DocumentState), so ``path in open_documents`` and
        ``open_documents.get(path) is not None`` are the same test; both
        spellings occur and either is fine.
        """
        if file_path not in self.open_documents:
            return None
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
            heap = path in self.heap_sources
            try:
                if heap:
                    # A precompiled file is never pushed, so the ASCII guard has
                    # no work here — and the refusing path must not write to
                    # the very file it declares untouchable.
                    content = await asyncio.to_thread(_read_text, path)
                    guard_warning = None
                else:
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
            if heap and content != doc.content:
                # D-C7: refuse the edit — no didChange goes out, and stat_sig
                # is not refreshed, so every later backstop re-detects and
                # re-raises until the file on disk matches the text the prover
                # was given (the content at didOpen), or the session is
                # relaunched without the file. A file that was ALREADY modified
                # when MCP opened it pushed that modification at didOpen, so
                # for it only the relaunch clears the refusal. On the
                # event-driven watcher path the raise is logged and swallowed;
                # the tool-call backstop surfaces it on the next tool call.
                raise IsabelleToolError(PRECOMPILED_MODIFIED_ERROR.format(
                    file=path, logic=self.logic))
            if content != doc.content or doc.needs_full_sync:
                # RANGED contentChanges preserve the server's evaluated prefix
                # (a whole-document didChange is remove-all + insert-all server-
                # side and re-executes the entire file).  The full-text form is
                # kept as the recovery shape: after a server-side rejection the
                # base text over there is unknown, so a diff would corrupt it.
                changes = (
                    None if doc.needs_full_sync
                    else ranged_content_changes(doc.content, content)
                )
                if changes is None:
                    changes = [{"text": content}]
                doc.version += 1
                doc.content = content
                logger.info(
                    "Syncing dirty file: %s v%d (%s)", path, doc.version,
                    "full" if "range" not in changes[0] else f"{len(changes)} hunk(s)",
                )
                await self.notify("textDocument/didChange", {
                    "textDocument": {"uri": doc.uri, "version": doc.version},
                    "contentChanges": changes,
                }, content=True)
                doc.needs_full_sync = False
                # Phase D bookkeeping: a didChange actually went out — the
                # file's breakable-site serials may be dead. Lazy import:
                # debugger.py imports this module at its top.
                from isabelle_mcp.debugger import registry as _bp_registry
                _bp_registry.mark_dirty(path)
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

    async def get_commands_at_lines(
        self, file_path: str, lines: list[LSPLine],
    ) -> dict[int, list[tuple[JsonDict, str]]] | None:
        """Return, per requested 0-indexed line, the commands overlapping it.

        Each command comes back as its LSP range and its source text; ignored
        spans (the whitespace and comments between commands) are left out, so a
        blank line yields an empty list. A command spanning several lines is
        returned for every line it covers. ``None`` means the server does not
        hold the file.

        One request answers however many lines are asked about, which is what
        makes a bulk position-to-state table cost one round trip per file.
        """
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")
        result = await self.request("PIDE/commands_at_lines", {
            "uri": doc.uri, "lines": [int(line) for line in lines],
        })
        if not isinstance(result, dict) or not result.get("open"):
            return None
        out: dict[int, list[tuple[JsonDict, str]]] = {}
        for entry in result.get("lines") or []:
            if not isinstance(entry, dict):
                continue
            line = entry.get("line")
            if not isinstance(line, int):
                continue
            commands = []
            for command in entry.get("commands") or []:
                rng, source = command.get("range"), command.get("source")
                if isinstance(rng, dict) and isinstance(source, str):
                    commands.append((rng, source))
            out[line] = commands
        return out

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

    def _next_query_token(self) -> str:
        """Fresh correlation token, shared by the position-explicit queries and
        the debugger requests (both live in the same Scala-side handler table,
        so uniqueness must hold across them)."""
        self._query_seq += 1
        return str(self._query_seq)

    async def query_at_position(
        self,
        method: str,
        file_path: str,
        line: LSPLine,
        character: int,
        extra: dict[str, Any] | None = None,
    ) -> QueryReply:
        """Ask the prover about the command at a position, and return its answer.

        This reads the command's state straight out of the prover's document
        state: no caret movement, no overlay, no document update. So it is safe
        during an evaluation, and two of them can be in flight at once.

        A cancel goes out on every exit path — including CancelledError, where
        nobody is left to read the answer — so the prover does not keep working
        on a query whose result is already unwanted.
        """
        doc = self.open_documents.get(file_path)
        if not doc:
            raise IsabelleToolError(f"Document not open: {file_path}")

        token = self._next_query_token()
        params: dict[str, Any] = {
            "token": token,
            "textDocument": {"uri": doc.uri},
            "position": {"line": line, "character": character},
            "timeout": self.QUERY_BACKSTOP,
        }
        params.update(extra or {})
        try:
            result = await self.request(method, params)
        finally:
            with contextlib.suppress(IsabelleToolError):
                await self.notify("PIDE/query_cancel", {"token": token})

        if not isinstance(result, dict):
            return QueryReply(status=query.CRASHED)
        return QueryReply(
            status=str(result.get("status") or query.CRASHED),
            comment=bool(result.get("comment")),
            forked=bool(result.get("forked")),
            content=result.get("content") if isinstance(result.get("content"), str) else "",
        )

    async def get_proof_state_at_position(
        self, file_path: str, line: LSPLine, character: int,
    ) -> QueryReply:
        """The proof state after the command at a position, rendered as HTML."""
        return await self.query_at_position(
            "PIDE/proof_state_at_position", file_path, line, character,
        )

    async def get_find_theorems_at_position(
        self, file_path: str, line: LSPLine, character: int,
        query_text: str, limit: str, allow_dups: str,
    ) -> QueryReply:
        """find_theorems in the context of the command at a position.

        ``allow_dups`` keeps the prover's own inverted reading: duplicates are
        removed only when the argument is exactly the string ``"false"``.
        """
        return await self.query_at_position(
            "PIDE/find_theorems_at_position", file_path, line, character,
            {"query": query_text, "limit": limit, "allow_dups": allow_dups},
        )

    # ── ML debugger requests (docs/archive/DEBUGGER_DESIGN.md section 7.1) ──
    #
    # Thin wrappers: compose the wire params, correlate by a fresh query token,
    # and return the reply dict as-is — status words become sentences in the
    # tool layer, not here. The Scala side always answers (its backstop timer
    # replies `timeout` itself), so the default wait is progress-monitored like
    # the queries; ``request_timeout`` adds a hard transport deadline instead
    # (the probes' failsafe against a wedged reply path).

    async def _debugger_request(
        self, method: str, params: dict[str, Any],
        request_timeout: float | None,
    ) -> JsonDict:
        reply = await self.request(method, params, timeout=request_timeout)
        return reply if isinstance(reply, dict) else {"status": query.CRASHED}

    async def debugger_breakpoints(
        self, file_path: str, *, timeout: float = 30.0,
        request_timeout: float | None = None,
    ) -> JsonDict:
        """Every breakable site in the file, with prover-truth enabled-states.

        ``outdated`` (pending edits not yet incorporated) is retryable; ``ok``
        with an empty list does NOT mean "no sites" — breakpoint markup exists
        only after ML compilation. (The wire's optional ``range`` filter is not
        exposed: no consumer yet.)
        """
        return await self._debugger_request(
            "PIDE/debugger_breakpoints",
            {"uri": file_path_to_uri(file_path),
             "token": self._next_query_token(), "timeout": float(timeout)},
            request_timeout)

    async def debugger_toggle_breakpoint(
        self, file_path: str, serial: int, state: bool, *,
        timeout: float = 30.0, request_timeout: float | None = None,
    ) -> JsonDict:
        """Acknowledged toggle on the real breakpoint ref; ``state`` is absolute
        (retry is idempotent) and an ``ok`` reply carries ``was``, the previous
        value — only an ``ok`` may record an arming client-side."""
        return await self._debugger_request(
            "PIDE/debugger_toggle_breakpoint",
            {"uri": file_path_to_uri(file_path), "serial": serial,
             "state": state, "token": self._next_query_token(),
             "timeout": float(timeout)},
            request_timeout)

    async def debugger_eval(
        self, thread: str, expr: str, *, frame: int = 0,
        timeout: float = 30.0, request_timeout: float | None = None,
    ) -> JsonDict:
        """One ML expression in a stopped thread's frame; the Scala side embeds
        ``expr`` as ONE ML string literal compiled inside the prelude wrapper's
        protection, and ``timeout`` is the prover-side deadline."""
        return await self._debugger_request(
            "PIDE/debugger_eval",
            {"token": self._next_query_token(), "thread": thread,
             "frame": frame, "expr": expr, "timeout": float(timeout)},
            request_timeout)

    async def debugger_print_vals(
        self, thread: str, *, frame: int = 0,
        timeout: float = 30.0, request_timeout: float | None = None,
    ) -> JsonDict:
        """The frame's locals, realised through the eval verb server-side; the
        reply shape is exactly ``debugger_eval``'s."""
        return await self._debugger_request(
            "PIDE/debugger_print_vals",
            {"token": self._next_query_token(), "thread": thread,
             "frame": frame, "timeout": float(timeout)},
            request_timeout)

    async def debugger_abort(
        self, thread: str, *, request_timeout: float | None = None,
    ) -> JsonDict:
        """Set the abort flag for the thread's outstanding evaluation:
        ``no_evaluation`` means the thread is settled (the retry loop's stop
        signal), ``aborting`` means the flag command went out once."""
        return await self._debugger_request(
            "PIDE/debugger_abort", {"thread": thread}, request_timeout)

    async def debugger_input(
        self, thread: str, verbs: list[str], *,
        request_timeout: float | None = None,
    ) -> JsonDict:
        """Raw debugger verbs (continue/step/...) to a stopped thread;
        acknowledged with ``{ok: true}``, results arrive as notifications."""
        return await self._debugger_request(
            "PIDE/debugger_input", {"thread": thread, "verbs": verbs},
            request_timeout)

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
