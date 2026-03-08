"""
Pytest configuration and shared fixtures.
"""

import asyncio
import pytest
from pathlib import Path
from typing import Dict, Any, Optional, List
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def temp_theory_file(tmp_path):
    """Create a temporary theory file for testing."""
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
    """Create a temporary theory file with errors."""
    theory_file = tmp_path / "TestError.thy"
    theory_file.write_text(
        'theory TestError\n'
        'imports Main\n'
        'begin\n'
        '\n'
        'lemma false_lemma: "False"\n'
        '  by auto  (* This will fail *)\n'
        '\n'
        'end\n'
    )
    return str(theory_file)


class MockLSPClient:
    """Mock LSP client for unit testing."""

    def __init__(self):
        self.logic = "HOL"
        self.initialized = True
        self.open_documents: Dict[str, Dict[str, Any]] = {}
        self.diagnostics_cache: Dict[str, List[Dict[str, Any]]] = {}
        self.processing_status: Dict[str, bool] = {}

        # Mock responses
        self.hover_response = None
        self.completion_response = None
        self.definition_response = None
        self.highlights_response = None

    async def start(self):
        """Mock start."""
        self.initialized = True

    async def shutdown(self):
        """Mock shutdown."""
        self.initialized = False

    async def initialize(self):
        """Mock initialize."""
        self.initialized = True

    async def open_document(self, file_path: str):
        """Mock open document."""
        if not Path(file_path).exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        with open(file_path, 'r') as f:
            content = f.read()

        self.open_documents[file_path] = {
            'uri': f"file://{file_path}",
            'version': 1,
            'content': content,
        }
        # Don't automatically mark as processing complete when opening
        # Tests can set this explicitly if needed
        if file_path not in self.processing_status:
            self.processing_status[file_path] = False

    async def close_document(self, file_path: str):
        """Mock close document."""
        if file_path in self.open_documents:
            del self.open_documents[file_path]

    async def get_hover(self, file_path: str, line: int, character: int):
        """Mock get hover."""
        return self.hover_response

    async def get_completions(self, file_path: str, line: int, character: int):
        """Mock get completions."""
        return self.completion_response

    async def get_definition(self, file_path: str, line: int, character: int):
        """Mock get definition."""
        return self.definition_response

    async def get_highlights(self, file_path: str, line: int, character: int):
        """Mock get highlights."""
        return self.highlights_response

    def get_cached_diagnostics(self, file_path: str) -> List[Dict[str, Any]]:
        """Mock get cached diagnostics."""
        return self.diagnostics_cache.get(file_path, [])

    def is_processing_complete(self, file_path: str) -> bool:
        """Mock processing complete check."""
        return self.processing_status.get(file_path, False)

    async def notify(self, method: str, params: Dict[str, Any]):
        """Mock notify."""
        pass

    async def request(self, method: str, params: Dict[str, Any]):
        """Mock request."""
        return {}


@pytest.fixture
def mock_lsp_client():
    """Provide a mock LSP client."""
    return MockLSPClient()


@pytest.fixture
def sample_hover_response():
    """Sample LSP hover response."""
    return {
        "contents": {
            "kind": "markdown",
            "value": "**my_const** :: nat\n\nDefined as: `my_const = 42`"
        },
        "range": {
            "start": {"line": 4, "character": 11},
            "end": {"line": 4, "character": 19}
        }
    }


@pytest.fixture
def sample_completion_response():
    """Sample LSP completion response."""
    return {
        "isIncomplete": False,
        "items": [
            {
                "label": "lemma",
                "kind": 14,  # Keyword
                "detail": "Isabelle keyword",
                "documentation": "Start a lemma proof"
            },
            {
                "label": "theorem",
                "kind": 14,
                "detail": "Isabelle keyword",
                "documentation": "Start a theorem proof"
            },
            {
                "label": "apply",
                "kind": 14,
                "detail": "Proof method",
                "documentation": "Apply a proof method"
            }
        ]
    }


@pytest.fixture
def sample_definition_response():
    """Sample LSP definition response."""
    return [
        {
            "uri": "file:///path/to/Test.thy",
            "range": {
                "start": {"line": 4, "character": 11},
                "end": {"line": 4, "character": 19}
            }
        }
    ]


@pytest.fixture
def sample_highlights_response():
    """Sample LSP highlights response."""
    return [
        {
            "range": {
                "start": {"line": 4, "character": 11},
                "end": {"line": 4, "character": 19}
            },
            "kind": 1  # Text
        },
        {
            "range": {
                "start": {"line": 7, "character": 20},
                "end": {"line": 7, "character": 28}
            },
            "kind": 2  # Read
        }
    ]


@pytest.fixture
def sample_diagnostics():
    """Sample diagnostics."""
    return [
        {
            "range": {
                "start": {"line": 4, "character": 0},
                "end": {"line": 4, "character": 10}
            },
            "severity": 1,  # Error
            "message": "Type error: expected nat, got bool"
        },
        {
            "range": {
                "start": {"line": 7, "character": 0},
                "end": {"line": 7, "character": 5}
            },
            "severity": 2,  # Warning
            "message": "Unused variable"
        }
    ]


@pytest.fixture
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()
