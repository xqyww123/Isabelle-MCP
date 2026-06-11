from pathlib import Path
from typing import Any

import pytest

from isabelle_mcp.lsp_client import DocumentState, IsabelleLSPClient
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
    """ProcessingTracker stub where everything is already processed."""

    def __init__(self, *, all_processed: bool = True):
        self._all_processed = all_processed

    def range_processed(self, start_line: LSPLine, end_line: LSPLine) -> bool:
        return self._all_processed

    def line_reached(self, line: int) -> bool:
        return self._all_processed

    def line_running(self, line: int) -> bool:
        return False

    @property
    def all_processed(self) -> bool:
        return self._all_processed

    def note_doc_update_sent(self) -> None:
        pass

    def get_running_ranges(self) -> list[tuple[int, int, int, int]]:
        return []

    def get_running_ranges_with_onset(self) -> list[tuple[int, int, int, int, float]]:
        return []

    def get_unprocessed_ranges(self) -> list[tuple[int, int, int, int]]:
        return []

    def get_bad_ranges(self) -> list[tuple[int, int, int, int]]:
        return []

    def get_overview_error_ranges(self) -> list[tuple[int, int, int, int]]:
        return []

    def get_overview_warning_ranges(self) -> list[tuple[int, int, int, int]]:
        return []

    async def wait_until_processed_bounded(
        self, start_line: LSPLine, end_line: LSPLine,
        timeout: float = 5.0, health_check=None, check_interval: float = 5.0,
    ) -> bool:
        return self._all_processed


class MockLSPClient:
    """Mock LSP client for unit testing."""

    def __init__(self):
        self.logic = "HOL"
        self.initialized = True
        self.project_root = None
        # Present so launch/terminate/guard tests can simulate a (not-)running prover.
        self.process = None
        self.isabelle_version = ""
        self.open_documents: dict[str, DocumentState] = {}
        self.diagnostics_cache: dict[str, list[dict[str, Any]]] = {}
        self.processing_status: dict[str, bool] = {}
        self._processing_trackers: dict[str, Any] = {}
        self.heap_sources: set[str] = set()

        self.hover_response = None
        self.definition_response = None
        self.highlights_response = None
        self.goal_response: list[str] = []
        self.dynamic_output_response = ""
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
        wait_for_diagnostics: bool = True,
        diagnostic_timeout: float = 2.0,
    ):
        if not Path(file_path).exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        if content is None:
            with open(file_path) as f:
                content = f.read()
        self.open_documents[file_path] = DocumentState(
            file_path=file_path, uri=f"file://{file_path}", version=1, content=content,
        )
        if file_path not in self.processing_status:
            self.processing_status[file_path] = False
        if file_path not in self._processing_trackers:
            self._processing_trackers[file_path] = MockProcessingTracker()

    async def set_caret(
        self, file_path: str, line: LSPLine, character: LSPCharacter = LSPCharacter(0),
    ) -> None:
        pass

    async def wait_for_processing(
        self, file_path: str, start_line: LSPLine, end_line: LSPLine | None = None,
    ) -> None:
        pass

    async def wait_for_processing_bounded(
        self, file_path: str, start_line: LSPLine, end_line: LSPLine, timeout: float,
    ) -> bool:
        return True

    async def force_interrupt(self, file_path: str) -> None:
        pass

    async def request_theory_status(self) -> list[dict]:
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

    def get_all_running_commands(self) -> list:
        return []

    def file_all_processed(self, file_path: str) -> bool:
        return self.processing_status.get(file_path, False)

    def get_processing_tracker(self, file_path: str) -> Any:
        return self._processing_trackers.get(file_path)

    async def close_document(self, file_path: str):
        self.open_documents.pop(file_path, None)

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

    async def get_goals_at_position(self, file_path: str, line: LSPLine, character: int) -> list[str]:
        return self.goal_response

    async def get_command_at_position(
        self, file_path: str, line: LSPLine, character: LSPCharacter,
    ) -> tuple[str, dict[str, Any]] | None:
        return self.command_at_position_response

    async def get_dynamic_output(self, file_path: str, line: LSPLine, character: int = 0) -> str:
        return self.dynamic_output_response

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


@pytest.fixture(autouse=True)
def _reset_evaluation_state():
    from isabelle_mcp.evaluation import evaluation_state
    evaluation_state.cancel()
    yield
    evaluation_state.cancel()


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
