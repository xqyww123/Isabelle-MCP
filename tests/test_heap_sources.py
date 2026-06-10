"""Tests for precompiled-heap detection (`isabelle build -n -l` enumeration)."""

import os

import pytest

from isabelle_mcp.evaluation import evaluate_to, format_evaluation_result
from isabelle_mcp.lsp_client import IsabelleLSPClient
from tests.test_tools_evaluate import MockProcessingTracker

LISTING = """\
Session Pure/Pure
  /opt/Isabelle/src/Pure/Pure.thy
  /opt/Isabelle/src/Pure/ROOT.ML
Session FOL/FOL
  /opt/Isabelle/src/FOL/IFOL.thy
  /opt/Isabelle/src/FOL/FOL.thy
  /opt/Isabelle/src/FOL/fologic.ML
"""


class TestParseBuildSources:
    def test_parses_indented_file_lines(self):
        paths = IsabelleLSPClient.parse_build_sources(LISTING)
        assert os.path.realpath("/opt/Isabelle/src/FOL/IFOL.thy") in paths
        assert os.path.realpath("/opt/Isabelle/src/Pure/ROOT.ML") in paths
        assert len(paths) == 5

    def test_session_headers_excluded(self):
        paths = IsabelleLSPClient.parse_build_sources(LISTING)
        assert not any("Session" in p for p in paths)

    def test_no_session_header_means_failure(self):
        # e.g. *** Undefined session(s): "NoSuch" — must yield no warnings at all
        assert IsabelleLSPClient.parse_build_sources('*** Undefined session(s): "X"') == set()
        assert IsabelleLSPClient.parse_build_sources("") == set()


class TestHeapWarning:
    def _client(self, heap):
        c = IsabelleLSPClient(logic="FOL")
        c.heap_sources = {os.path.realpath(p) for p in heap}
        return c

    def test_warns_for_heap_file(self):
        c = self._client(["/opt/Isabelle/src/FOL/IFOL.thy"])
        w = c.heap_warning("/opt/Isabelle/src/FOL/IFOL.thy")
        assert w is not None and "PRECOMPILED" in w and "'FOL'" in w

    def test_silent_for_other_files(self):
        c = self._client(["/opt/Isabelle/src/FOL/IFOL.thy"])
        assert c.heap_warning("/tmp/Draft.thy") is None

    def test_silent_when_enumeration_empty(self):
        c = self._client([])
        assert c.heap_warning("/opt/Isabelle/src/FOL/IFOL.thy") is None


@pytest.mark.asyncio
class TestEvaluateHeapFile:
    async def test_heap_warning_attached_when_complete(
        self, mock_lsp_client, temp_theory_file
    ):
        mock_lsp_client._processing_trackers[temp_theory_file] = (
            MockProcessingTracker(all_processed=True)
        )
        mock_lsp_client.heap_sources = {os.path.realpath(temp_theory_file)}
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.status == "complete"
        assert view.heap_warning and "PRECOMPILED" in view.heap_warning
        assert "⚠️" in format_evaluation_result(view)

    async def test_no_warning_for_normal_file(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client._processing_trackers[temp_theory_file] = (
            MockProcessingTracker(all_processed=True)
        )
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.heap_warning is None
        assert "⚠️" not in format_evaluation_result(view)

    async def test_modified_heap_file_abandons_evaluation(
        self, mock_lsp_client, temp_theory_file, monkeypatch
    ):
        from isabelle_mcp import evaluation as ev

        # never processes → the (capped) wait loop returns in_progress
        mock_lsp_client._processing_trackers[temp_theory_file] = (
            MockProcessingTracker(all_processed=False)
        )
        mock_lsp_client.heap_sources = {os.path.realpath(temp_theory_file)}
        monkeypatch.setattr(ev, "HEAP_POLL_INTERVAL", 0.05)
        view = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert view.status == "cancelled"
        assert "never reprocess" in view.message
        # no evaluation left pending — the next evaluate_to must not be rejected
        assert not ev.evaluation_state.active
