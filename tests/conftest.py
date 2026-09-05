import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import types

import pytest

from isabelle_mcp.lsp_client import (
    DocumentState,
    IsabelleLSPClient,
    _canon,
    parse_theory_status,
)
from isabelle_mcp.models import TheoryStatusRecord
from isabelle_mcp.processing import FreshnessState
from isabelle_mcp.utils import LSPCharacter, LSPLine, OwnedLock, set_symbols_text


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

    Not modelled: the freshness rule (the real ``fresh`` gate, under which
    every predicate answers "not yet" / ``unknown``) and cancelled ranges. The
    picture is a FULL one (``initialized``) stamped *document_version* (0 by
    default: Version.init, older than everything, so at least as new as the
    mock client's default record and never newer than any real stamp); a test
    about the stamp arbitration sets a stamp of its own. *all_processed=False*
    (without ranges) means "nothing evaluated at all". *frontier* / *quiet*
    force ``line_reached`` / ``range_processed`` outright (for "frontier
    reached the target but a trailing fork in the prefix is still in flight");
    *state* forces ``position_state``.
    """

    def __init__(
        self, *, all_processed: bool = True,
        frontier: bool | None = None, quiet: bool | None = None,
        state: str | None = None,
        bad=None, sorry=None, overview_error=None, overview_warning=None,
        running=None, unprocessed=None,
        initialized: bool = True, document_version: int | None = 0,
    ):
        assert all_processed or not (running or unprocessed), \
            "all_processed=False means no ranges at all; pass ranges instead"
        self._all_processed = all_processed
        self._frontier = frontier
        self._quiet = quiet
        self._state = state
        self.initialized = initialized
        self.document_version = document_version
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

    @property
    def fresh(self) -> bool:
        # The stub models no freshness rule: an initialized picture is fresh.
        return self.initialized

    def stamp_at_least_as_new_as(self, version: int) -> bool:
        # Mirrors ProcessingTracker: a full picture stamped at least as new as
        # *version*. The stub's default stamp is 0 (Version.init), so it is at
        # least as new as the mock client's default newest version (also 0).
        return (
            self.initialized
            and self.document_version is not None
            and self.document_version <= version
        )

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
    PROGRESS_CHECK_INTERVAL = 5.0

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
        # The freshness state (see the real client): the newest version stays 0
        # unless a test advances it, so every stub tracker's default stamp is
        # at least as new as it and the mock's flush reconciles at once.
        self.freshness = FreshnessState()
        # The rows the mock's theory_status answers with, or None for "one
        # settled row per open document"; ``theory_status_stamp`` is the reply's
        # document_version (the mock's records carry the newest version unless
        # a test says otherwise).
        self.theory_rows: list[dict] | None = None
        self.theory_status_stamp: int | None = None
        self.flush_calls: list[bool] = []

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

    async def request_theory_status(self) -> TheoryStatusRecord:
        await asyncio.sleep(0)
        rows = self.theory_rows
        if rows is None:
            rows = [settled_theory_row(path) for path in self.open_documents]
        stamp = self.theory_status_stamp
        if stamp is None:
            stamp = self.freshness.newest_document_version
        return theory_status_record(rows, stamp)

    async def flush(self, *, resync_dependencies: bool) -> tuple[dict, int]:
        """The mock's flush: yields once, names the newest version (nothing on
        the mock ever creates one) and covers every content send."""
        await asyncio.sleep(0)
        self.flush_calls.append(resync_dependencies)
        snapshot = self.freshness.content_sends
        self.freshness.content_sends_flushed = max(
            self.freshness.content_sends_flushed, snapshot)
        await self.freshness.notify()
        return (
            {"document_version": self.freshness.newest_document_version, "changed_uris": []},
            snapshot,
        )

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
    new = OwnedLock("isabelle_mcp_evaluation_lock_held")
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


@pytest.fixture
def trap_run_writes(monkeypatch):
    """Ruling 22's structural invariant, as a booby trap: every entry point
    that writes the run — starting one, joining and advancing one — and the
    caret send raise. A tool path that walks its branches under this fixture
    never evaluates. All four traps live here: the caret trap is installed on
    the mock client's class, so it holds for every MockLSPClient of the test
    (the FakeClients of the tool test modules assert on their own)."""
    from isabelle_mcp import evaluation as ev

    def trap(*a, **k):
        raise AssertionError("a tool path wrote the evaluation state")

    async def async_trap(*a, **k):
        raise AssertionError("a tool path moved the caret")

    monkeypatch.setattr(ev.EvaluationState, "join_or_start", trap)
    monkeypatch.setattr(ev.EvaluationState, "start", trap)
    monkeypatch.setattr(ev.EvaluationState, "advance", trap)
    monkeypatch.setattr(MockLSPClient, "set_caret", async_trap)


@pytest.fixture(autouse=True)
def _per_test_registry_lock(monkeypatch):
    """Give every test its own breakpoint ``registry.lock`` (same reason as
    ``_per_test_evaluation_state_lock``: the unified close contends for it at
    every tool entry, and a lock bound to an earlier test's loop would die)."""
    from isabelle_mcp import debugger
    monkeypatch.setattr(debugger.registry, "lock", asyncio.Lock())


@pytest.fixture(autouse=True)
def _default_entry_record():
    """Every test starts with an EMPTY entry record (as if the tool entry had
    pulled a theory_status with no rows, stamped 0): the readers of the record
    (the unified close, the cancel shortcut, the footer, the reopen) are
    reachable from tests that never run an entry, and reading an unset record
    is a programming error in production. A test about the record sets its
    own with ``evaluation.set_entry_record``."""
    from isabelle_mcp import evaluation as ev
    token = ev._entry_record.set(TheoryStatusRecord(document_version=0, theories=()))
    yield
    ev._entry_record.reset(token)


def settled_theory_row(path: str, **kw) -> dict:
    """One raw theory_status row: settled unless *kw* says otherwise."""
    row = {
        "node_name": path, "theory_name": Path(path).stem, "external": False,
        "imports": [], "ok": True, "total": 10, "unprocessed": 0, "running": 0,
        "warned": 0, "failed": 0, "finished": 10, "canceled": False,
        "consolidated": True, "percentage": 100,
    }
    row.update(kw)
    return row


def theory_status_record(rows: list[dict], document_version: int = 0) -> TheoryStatusRecord:
    """The stamped record a PIDE/theory_status reply becomes, from raw rows."""
    return TheoryStatusRecord(
        document_version=document_version,
        theories=tuple(parse_theory_status(r) for r in rows),
    )


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


def full_decoration_entries(**content: list) -> list[dict]:
    """The shape of a FULL ``PIDE/decoration`` push: every tracked type named,
    empty unless *content* gives it ranges (LSP ``(l, c, l, c)`` tuples). The
    server sends this list on every open and reopen; a differential push names
    only what changed, and the client builds no tracker from one."""
    from isabelle_mcp.processing import _TRACKED_TYPES
    return [
        {"type": typ, "content": [{"range": list(r)} for r in content.get(typ, [])]}
        for typ in sorted(_TRACKED_TYPES)
    ]
