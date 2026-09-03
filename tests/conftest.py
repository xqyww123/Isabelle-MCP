import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import types

import pytest

from isabelle_mcp.lsp_client import DocumentState, IsabelleLSPClient, _canon
from isabelle_mcp.models import TheoryStatus
from isabelle_mcp.utils import LSPCharacter, LSPLine, set_symbols_text


@pytest.fixture(autouse=True, scope="session")
def _seed_symbol_table():
    """Seed the symbol table from the bundled fixture.

    At runtime the table is seeded over PIDE/symbols; in tests there is no
    server, so we seed from a checked-in copy of Isabelle's etc/symbols. This
    keeps the ASCII/Unicode conversion (and the token tests that rely on it)
    hermetic — independent of whether 'isabelle' is on PATH.
    """
    symbols_file = Path(__file__).parent / "data" / "symbols"
    set_symbols_text(symbols_file.read_text(encoding="utf-8"))


@pytest.fixture
def temp_theory_file(tmp_path):
    theory_file = tmp_path / "Test.thy"
    theory_file.write_text(
        'theory Test\n'
        'imports Main\n'
        'begin\n'
        '\n'
        'definition my_const :: "nat" where\n'
        '  "my_const = 42"\n'
        '\n'
        'lemma test_lemma: "my_const = 42"\n'
        '  by (simp add: my_const_def)\n'
        '\n'
        'end\n'
    )
    return str(theory_file)


@pytest.fixture
def temp_theory_with_errors(tmp_path):
    theory_file = tmp_path / "TestError.thy"
    theory_file.write_text(
        'theory TestError\n'
        'imports Main\n'
        'begin\n'
        '\n'
        'lemma false_lemma: "False"\n'
        '  by auto\n'
        '\n'
        'end\n'
    )
    return str(theory_file)


class MockProcessingTracker:
    """ProcessingTracker stub with an honest execution frontier.

    The decoration ranges are the real tracker's vocabulary: 0-indexed
    ``(start_line, start_char, end_line, end_char)`` tuples in ``unprocessed``
    and ``running`` (plus the error/warning getters the snapshot reads).
    ``line_reached`` / ``range_processed`` / ``line_running`` are derived from
    them exactly as ProcessingTracker derives its own, so a test that moves
    the frontier edits ``unprocessed`` (the lists are live — a concurrent task
    can shrink them while an evaluation waits).

    Not modelled: the post-edit grace window (the real ``_fresh`` gate, under
    which every predicate answers "not yet" / ``unknown``) and cancelled
    ranges. *all_processed=False* (without ranges) means "nothing evaluated at
    all". *frontier* / *quiet* force ``line_reached`` / ``range_processed``
    outright (for "frontier reached the target but a trailing fork in the
    prefix is still in flight"); *state* forces ``position_state``.
    """

    def __init__(
        self, *, all_processed: bool = True,
        frontier: bool | None = None, quiet: bool | None = None,
        state: str | None = None,
        bad=None, sorry=None, overview_error=None, overview_warning=None,
        running=None, unprocessed=None,
    ):
        assert all_processed or not (running or unprocessed), \
            "all_processed=False means no ranges at all; pass ranges instead"
        self._all_processed = all_processed
        self._frontier = frontier
        self._quiet = quiet
        self._state = state
        self._bad = bad or []
        self._sorry = sorry or []
        self._oerr = overview_error or []
        self._owarn = overview_warning or []
        self.running = list(running or [])
        self.unprocessed = list(unprocessed or [])

    @staticmethod
    def _overlaps(ranges, start_line: int, end_line: int) -> bool:
        return any(sl <= end_line and start_line <= el for sl, _, el, _ in ranges)

    def _range_reached(self, start_line: int, end_line: int) -> bool:
        """No unprocessed range overlaps the span (running ranges do not count)."""
        if self._frontier is not None:
            return self._frontier
        return self._all_processed and not self._overlaps(self.unprocessed, start_line, end_line)

    def range_processed(self, start_line: LSPLine, end_line: LSPLine) -> bool:
        if self._quiet is not None:
            return self._quiet
        return (self._all_processed
                and not self._overlaps(self.unprocessed + self.running, start_line, end_line))

    def line_reached(self, line: int) -> bool:
        return self._range_reached(line, line)

    def line_running(self, line: int) -> bool:
        return self._overlaps(self.running, line, line)

    def range_state(self, start_line: int, end_line: int) -> tuple[str, float]:
        """Mirror ProcessingTracker.range_state (unprocessed before running;
        elapsed time always 0.0).

        *state* forces an answer outright, so a tool-level test can reach the
        outcomes this stub cannot model — `unknown` (inside the post-edit grace
        window) and `cancelled` (an interrupted command)."""
        from isabelle_mcp import processing
        if self._state is not None:
            return (self._state, 0.0)
        if not self._range_reached(start_line, end_line):
            return (processing.NOT_EVALUATED, 0.0)
        if self._overlaps(self.running, start_line, end_line):
            return (processing.RUNNING, 0.0)
        return (processing.PROCESSED, 0.0)

    def position_state(self, line: int) -> str:
        return self.range_state(line, line)[0]

    @property
    def all_processed(self) -> bool:
        return self._all_processed and not self.unprocessed and not self.running

    def get_running_ranges(self) -> list[tuple[int, int, int, int]]:
        return list(self.running)

    def get_running_ranges_with_onset(self) -> list[tuple[int, int, int, int, float]]:
        # Onset "now": elapsed reads as ~0 s, below every reporting threshold.
        return [(*r, time.monotonic()) for r in self.running]

    def get_unprocessed_ranges(self) -> list[tuple[int, int, int, int]]:
        return list(self.unprocessed)

    def get_bad_ranges(self) -> list[tuple[int, int, int, int]]:
        return list(self._bad)

    def get_sorry_ranges(self) -> list[tuple[int, int, int, int]]:
        return list(self._sorry)

    def get_overview_error_ranges(self) -> list[tuple[int, int, int, int]]:
        return list(self._oerr)

    def get_overview_warning_ranges(self) -> list[tuple[int, int, int, int]]:
        return list(self._owarn)

    # The wait stubs yield once and answer from the current ranges. Yielding
    # exactly once closes the real race window (the loop re-reads state right
    # after); a test about a race must install a wait that truly blocks.
    async def wait_until_processed_bounded(
        self, start_line: LSPLine, end_line: LSPLine,
        timeout: float = 5.0, health_check=None, check_interval: float = 5.0,
    ) -> bool:
        await asyncio.sleep(0)
        return self.range_processed(start_line, end_line)

    async def wait_until_line_reached_bounded(
        self, line: LSPLine,
        timeout: float = 5.0, health_check=None, check_interval: float = 5.0,
    ) -> bool:
        await asyncio.sleep(0)
        return self.line_reached(line)


class MockLSPClient:
    """Mock LSP client for unit testing."""

    # Real ProcessingTracker wait loops health-check through the client.
    STALL_TIMEOUT = 60.0

    def _check_server_health(self, stall_timeout: float) -> None:
        pass

    def __init__(self):
        self.logic = "HOL"
        self.debug = False
        self.initialized = True
        self.project_root = None
        # A running prover by default (evaluate_to reports "session gone" on
        # ``process is None``); launch/terminate/guard tests set it themselves.
        self.process = types.SimpleNamespace(returncode=None)
        self.isabelle_version = ""
        self.open_documents: dict[str, DocumentState] = {}
        self.diagnostics_cache: dict[str, list[dict[str, Any]]] = {}
        self.processing_status: dict[str, bool] = {}
        self._processing_trackers: dict[str, Any] = {}
        self.heap_sources: set[str] = set()
        # The tool-call entry's parsed theory_status (see the real client);
        # tests preset it for the paths that read it without a round trip.
        self.entry_theories: list[TheoryStatus] = []

        self.hover_response = None
        self.definition_response = None
        self.highlights_response = None
        self.goal_response: list[str] = []
        # Set to a QueryReply to test a specific status; None means "ok, with
        # goal_response's subgoals".
        self.proof_state_reply = None
        self.find_theorems_reply = None
        self.find_theorems_html = ""
        # Whatever QUERY_BACKSTOP the real client would report in a timeout message.
        self.QUERY_BACKSTOP = 600.0
        self.command_at_position_response: tuple[str, dict[str, Any]] | None = None
        self.output_at_position_response: tuple[str, dict[str, Any], str] | None = None

    async def start(self):
        self.initialized = True

    async def shutdown(self):
        self.initialized = False

    # Reuse the real precompiled-heap warning logic (duck-typed on .heap_sources/.logic).
    heap_warning = IsabelleLSPClient.heap_warning

    async def open_document(
        self,
        file_path: str,
        content: str | None = None,
        *,
        wait_for_decoration: bool = True,
        decoration_timeout: float = 2.0,
        evaluation_target: bool = False,
    ):
        # The real client's shape: canonical key, an already-open document is
        # left as it is except that the evaluation-target mark may only rise.
        file_path = _canon(file_path)
        doc = self.open_documents.get(file_path)
        if doc is not None:
            doc.is_evaluation_target = doc.is_evaluation_target or evaluation_target
            return
        if not Path(file_path).exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        if content is None:
            with open(file_path) as f:
                content = f.read()
        self.open_documents[file_path] = DocumentState(
            file_path=file_path, uri=f"file://{file_path}", version=1, content=content,
            is_evaluation_target=evaluation_target,
        )
        if file_path not in self.processing_status:
            self.processing_status[file_path] = False
        if file_path not in self._processing_trackers:
            self._processing_trackers[file_path] = MockProcessingTracker()

    async def set_caret(
        self, file_path: str, line: LSPLine, character: LSPCharacter = LSPCharacter(0),
    ) -> None:
        pass

    # Where the real client blocks on prover I/O, the mock yields the event loop
    # once, so a concurrent task (another tool call, a frontier advance) gets to
    # run: resync_changed_open_documents and request_theory_status here, the two
    # wait stubs on the tracker. open_document / set_caret stay silent stubs so
    # evaluate_to's lock critical section is atomic in tests.
    async def resync_changed_open_documents(self) -> None:
        await asyncio.sleep(0)

    async def force_interrupt(self) -> dict:
        return {"outcome": "nothing_running", "retired": [], "excluded": [],
                "waived": [], "unloaded_from": []}

    async def teardown(self, reason: str = "The Isabelle session was terminated.") -> None:
        # mirrors IsabelleLSPClient.teardown's observable effects
        from isabelle_mcp.evaluation import evaluation_state
        from isabelle_mcp.utils import IsabelleToolError
        self._fail_pending_waiters(IsabelleToolError(reason))
        await self.shutdown()
        self.process = None
        evaluation_state.cancel()

    def _fail_pending_waiters(self, exc: Exception) -> None:
        self.failed_waiters_with = exc

    # the load commands of a non-.thy file; tests set ``loaders`` per case
    loaders: list[dict] = []

    async def request_loaders(self, file_path: str) -> list[dict]:
        return list(self.loaders)

    async def request_theory_status(self) -> list[dict]:
        await asyncio.sleep(0)
        theories = []
        for path in self.open_documents:
            name = Path(path).stem
            theories.append({
                "node_name": path,
                "theory_name": name,
                "external": False,
                "imports": [],
                "ok": True,
                "total": 10,
                "unprocessed": 0,
                "running": 0,
                "warned": 0,
                "failed": 0,
                "finished": 10,
                "canceled": False,
                "consolidated": True,
                "percentage": 100,
            })
        return theories

    async def cancel_execution(self) -> None:
        pass

    # Reuse the real derivation (duck-typed on ._processing_trackers /
    # .open_documents, like heap_warning): in production the running-command
    # list and the tracker's running ranges are ONE source with ONE clipping --
    # that is what makes "Nothing is running" unable to sit above a running:
    # row -- and the mock must not be able to split them either.
    get_all_running_commands = IsabelleLSPClient.get_all_running_commands

    def file_all_processed(self, file_path: str) -> bool:
        return self.processing_status.get(file_path, False)

    def get_processing_tracker(self, file_path: str) -> Any:
        # The real client's open check: a closed document's tracker is unreadable.
        if file_path not in self.open_documents:
            return None
        return self._processing_trackers.get(file_path)

    async def close_document(self, file_path: str):
        # Mirrors the real close: the mark guard, and the tracker goes with
        # the document (a test that wants a ghost tracker re-installs one).
        file_path = _canon(file_path)
        doc = self.open_documents.get(file_path)
        if doc is None:
            return
        assert not doc.is_evaluation_target, f"closing an evaluation target: {file_path}"
        del self.open_documents[file_path]
        self._processing_trackers.pop(file_path, None)

    async def get_hover(self, file_path: str, line: LSPLine, character: LSPCharacter) -> Any:
        if callable(self.hover_response):
            return self.hover_response(file_path, line, character)
        return self.hover_response

    async def get_definition(self, file_path: str, line: LSPLine, character: LSPCharacter) -> Any:
        if callable(self.definition_response):
            return self.definition_response(file_path, line, character)
        return self.definition_response

    async def get_highlights(self, file_path: str, line: LSPLine, character: LSPCharacter) -> Any:
        return self.highlights_response

    async def get_proof_state_at_position(
        self, file_path: str, line: LSPLine, character: int,
    ):
        """Answer as the prover would.

        ``proof_state_reply`` forces a specific reply when a test is about a
        status; otherwise ``goal_response`` (a plain list of subgoals) is dressed
        up as the ``subgoal``-classed HTML the real path renders, so the tests
        that only care about goals stay readable.
        """
        from isabelle_mcp.query import OK, QueryReply
        if self.proof_state_reply is not None:
            return self.proof_state_reply
        body = "".join(
            f'<span class="subgoal">{n}. {text}</span>'
            for n, text in enumerate(self.goal_response, start=1)
        )
        return QueryReply(status=OK, content=f"<pre>{body}</pre>" if body else "")

    async def get_find_theorems_at_position(
        self, file_path: str, line: LSPLine, character: int,
        query_text: str, limit: str, allow_dups: str,
    ):
        from isabelle_mcp.query import OK, QueryReply
        self.find_theorems_args = (query_text, limit, allow_dups)
        if self.find_theorems_reply is not None:
            return self.find_theorems_reply
        return QueryReply(status=OK, content=self.find_theorems_html)

    async def get_command_at_position(
        self, file_path: str, line: LSPLine, character: LSPCharacter,
    ) -> tuple[str, dict[str, Any]] | None:
        return self.command_at_position_response

    async def get_output_at_position(
        self, file_path: str, line: LSPLine, character: LSPCharacter,
    ) -> tuple[str, dict[str, Any], str] | None:
        return self.output_at_position_response

    def get_cached_diagnostics(self, file_path: str) -> list[dict[str, Any]]:
        return self.diagnostics_cache.get(file_path, [])

    def diagnostics_settled(self, file_path: str, settle_time: float = 1.0) -> bool:
        return self.processing_status.get(file_path, False)

    async def notify(self, method: str, params: dict[str, Any]):
        pass

    async def request(self, method: str, params: dict[str, Any]):
        return {}


@pytest.fixture
def mock_lsp_client():
    return MockLSPClient()


@pytest.fixture
async def evaluated_theory_file(mock_lsp_client, temp_theory_file) -> str:
    """``temp_theory_file`` evaluated up front on the mock: opened with the
    evaluation-target mark, its tracker all-processed. Queries never evaluate,
    so a query-tool test starts from here or expects the not-evaluated error."""
    await mock_lsp_client.open_document(temp_theory_file, evaluation_target=True)
    return temp_theory_file


@pytest.fixture(autouse=True)
def _reset_evaluation_state():
    """Every test starts with no run outstanding and no run on record: a run
    a test left behind would be stamped cancelled here and then read by the
    next test as "the last evaluation was cancelled"."""
    from isabelle_mcp.evaluation import evaluation_state
    evaluation_state.cancel()
    evaluation_state.current = None
    yield
    evaluation_state.cancel()
    evaluation_state.current = None


# Modules that imported the lock by value (``from ... import _evaluation_state_lock``).
_LOCK_HOLDERS = ("isabelle_mcp.evaluation", "isabelle_mcp.server")


@pytest.fixture(autouse=True)
def _per_test_evaluation_state_lock(monkeypatch):
    """Give every test its own ``_evaluation_state_lock``.

    An asyncio.Lock binds itself to the running event loop on its first
    contended acquire, and pytest-asyncio runs each test on a new loop. Now
    that the mocks yield, two tests can each contend for the module-level
    lock, and the second one would die with "bound to a different event loop".
    The lock is replaced in every module holding it by value; any already
    imported module whose ``_evaluation_state_lock`` is not this test's lock
    (a holder missing from _LOCK_HOLDERS) is reported rather than silently
    kept. A holder first imported in the middle of a test is caught by the
    next test.
    """
    import importlib
    new = asyncio.Lock()
    for name in _LOCK_HOLDERS:
        monkeypatch.setattr(importlib.import_module(name), "_evaluation_state_lock", new)
    stale = [
        f"{name}._evaluation_state_lock"
        for name, module in list(sys.modules.items())
        if module is not None and name.startswith(("isabelle_mcp", "tests"))
        and getattr(module, "_evaluation_state_lock", new) is not new
    ]
    assert not stale, (
        f"{stale} do not hold this test's _evaluation_state_lock; "
        "add the module to _LOCK_HOLDERS in tests/conftest.py"
    )


@pytest.fixture(autouse=True)
def _per_test_registry_lock(monkeypatch):
    """Give every test its own breakpoint ``registry.lock`` (same reason as
    ``_per_test_evaluation_state_lock``: the unified close contends for it at
    every tool entry, and a lock bound to an earlier test's loop would die)."""
    from isabelle_mcp import debugger
    monkeypatch.setattr(debugger.registry, "lock", asyncio.Lock())


@pytest.fixture(autouse=True)
def _reset_edit_clock(monkeypatch):
    """Isolate the global edit clock per test.

    Real-client tests bump processing._last_edit_sent (didOpen/didChange paths);
    without this reset a leaked stamp would freshness-gate any real-tracker
    assertion in the next ~2s of the suite. Pinning DECORATION_GRACE also
    shields assertions from an ISABELLE_MCP_DECORATION_GRACE env override
    (read at import time)."""
    from isabelle_mcp import processing
    monkeypatch.setattr(processing, "_last_edit_sent", float("-inf"))
    monkeypatch.setattr(processing, "DECORATION_GRACE", 2.0)


@pytest.fixture
def sample_hover_response():
    return {
        "contents": {"kind": "markdown", "value": "**my_const** :: nat\n\nDefined as: `my_const = 42`"},
        "range": {
            "start": {"line": 4, "character": 11},
            "end": {"line": 4, "character": 19},
        },
    }


@pytest.fixture
def sample_definition_response():
    return [{
        "uri": "file:///path/to/Test.thy",
        "range": {"start": {"line": 4, "character": 11}, "end": {"line": 4, "character": 19}},
    }]


@pytest.fixture
def sample_highlights_response():
    return [
        {"range": {"start": {"line": 4, "character": 11}, "end": {"line": 4, "character": 19}}, "kind": 1},
        {"range": {"start": {"line": 7, "character": 20}, "end": {"line": 7, "character": 28}}, "kind": 2},
    ]


@pytest.fixture
def sample_diagnostics():
    return [
        {"range": {"start": {"line": 4, "character": 0}, "end": {"line": 4, "character": 10}}, "severity": 1, "message": "Type error: expected nat, got bool"},
        {"range": {"start": {"line": 7, "character": 0}, "end": {"line": 7, "character": 5}}, "severity": 2, "message": "Unused variable"},
    ]


@pytest.fixture(autouse=True)
def _reset_component_cache():
    """Isolate component.py's process-wide resolution.

    `_resolve()` memoises the isabelle binary, its identifier and ISABELLE_HOME_USER — they cannot
    change while the server runs. Tests, however, fake PATH and `isabelle` freely, so a poisoned
    entry would leak into every later test (including the ones that talk to a real Isabelle)."""
    from isabelle_mcp.component import _resolve
    _resolve.cache_clear()
    yield
    _resolve.cache_clear()
