from pathlib import Path
from typing import Any

import pytest


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


class MockLSPClient:
    """Mock LSP client for unit testing."""

    def __init__(self):
        self.logic = "HOL"
        self.initialized = True
        self.open_documents: dict[str, dict[str, Any]] = {}
        self.diagnostics_cache: dict[str, list[dict[str, Any]]] = {}
        self.processing_status: dict[str, bool] = {}

        self.hover_response = None
        self.completion_response = None
        self.definition_response = None
        self.highlights_response = None
        self.goal_response: list[str] = []
        self.dynamic_output_response = ""
        self.preview_response: dict[str, Any] = {"content": ""}

    async def start(self):
        self.initialized = True

    async def shutdown(self):
        self.initialized = False

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
        self.open_documents[file_path] = {
            'uri': f"file://{file_path}", 'version': 1, 'content': content,
        }
        if file_path not in self.processing_status:
            self.processing_status[file_path] = False

    async def close_document(self, file_path: str):
        self.open_documents.pop(file_path, None)

    async def get_hover(self, file_path: str, line: int, character: int):
        return self.hover_response

    async def get_completions(self, file_path: str, line: int, character: int):
        return self.completion_response

    async def get_definition(self, file_path: str, line: int, character: int):
        return self.definition_response

    async def get_highlights(self, file_path: str, line: int, character: int):
        return self.highlights_response

    async def get_goals_at_position(self, file_path: str, line: int, character: int):
        return self.goal_response

    async def get_dynamic_output(self, file_path: str, line: int, character: int = 0, timeout: float = 2.0):
        return self.dynamic_output_response

    async def request_preview(self, file_path: str, column: int = 0, timeout: float = 30.0):
        return self.preview_response

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
def sample_hover_response():
    return {
        "contents": {"kind": "markdown", "value": "**my_const** :: nat\n\nDefined as: `my_const = 42`"},
        "range": {
            "start": {"line": 4, "character": 11},
            "end": {"line": 4, "character": 19},
        },
    }


@pytest.fixture
def sample_completion_response():
    return {
        "isIncomplete": False,
        "items": [
            {"label": "lemma", "kind": 14, "detail": "Isabelle keyword"},
            {"label": "theorem", "kind": 14, "detail": "Isabelle keyword"},
            {"label": "apply", "kind": 14, "detail": "Proof method"},
        ],
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
