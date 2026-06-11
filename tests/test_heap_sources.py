"""Tests for the launch-time build probe (`isabelle build -n -b -v -l`):
precompiled-heap enumeration and the build-status verdict."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from isabelle_mcp.evaluation import evaluate_to, format_evaluation_result
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.utils import IsabelleToolError
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

# What `isabelle build -n -b -v -l` actually prints for an unfinished chain:
# none of the -v extras is two-space-indented, and all of it is on stdout.
VERBOSE_LISTING = """\
Started at Thu Jun 11 19:00:00 GMT+8 2026 (polyml-5.9.2_x86_64-linux on host)
ISABELLE_TOOL_JAVA_OPTIONS="-Xms512m -Xmx4g"

ML_PLATFORM="x86_64-linux"
ML_OPTIONS="--minheap 1000"

Session Pure/Pure
  /opt/Isabelle/src/Pure/Pure.thy
Session FOL/FOL
  /opt/Isabelle/src/FOL/IFOL.thy
Skipping FOL ...
Unfinished session(s): FOL
Finished at Thu Jun 11 19:00:05 GMT+8 2026
0:00:05 elapsed time
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

    def test_verbose_preamble_lines_ignored(self):
        # -v adds Started at/ML_*=/Skipping/Unfinished/elapsed lines — none
        # two-space-indented, so the file-path heuristic must be unaffected.
        paths = IsabelleLSPClient.parse_build_sources(VERBOSE_LISTING)
        assert paths == {
            os.path.realpath("/opt/Isabelle/src/Pure/Pure.thy"),
            os.path.realpath("/opt/Isabelle/src/FOL/IFOL.thy"),
        }


class TestParseUnfinishedSessions:
    def test_extracts_names(self):
        line = "Unfinished session(s): HOL-Computational_Algebra, HOL-Number_Theory\n"
        assert IsabelleLSPClient.parse_unfinished_sessions(line) == [
            "HOL-Computational_Algebra", "HOL-Number_Theory",
        ]

    def test_verbose_listing(self):
        assert IsabelleLSPClient.parse_unfinished_sessions(VERBOSE_LISTING) == ["FOL"]

    def test_absent_line_means_empty(self):
        assert IsabelleLSPClient.parse_unfinished_sessions(LISTING) == []
        assert IsabelleLSPClient.parse_unfinished_sessions("") == []


@pytest.mark.asyncio
class TestEnumerateHeapSources:
    """The probe itself: verdict recording and fail-closed error paths."""

    def _proc(self, returncode: int, stdout: str) -> MagicMock:
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout.encode(), b""))
        return proc

    async def test_built_and_current(self):
        client = IsabelleLSPClient(logic="FOL", session_dirs=["/proj"])
        proc = self._proc(0, LISTING)
        spawn = AsyncMock(return_value=proc)
        with patch("asyncio.create_subprocess_exec", spawn):
            await client.enumerate_heap_sources()
        assert client.heap_built is True
        assert client.unfinished_sessions == []
        assert client.heap_sources  # listing parsed as before
        cmd = spawn.call_args[0]
        assert cmd[:6] == ("isabelle", "build", "-n", "-b", "-v", "-l")
        assert cmd[6:] == ("-d", "/proj", "FOL")  # session name last

    async def test_unfinished_chain(self):
        client = IsabelleLSPClient(logic="FOL")
        client.heap_built = True  # stale verdict from a previous launch
        proc = self._proc(1, VERBOSE_LISTING)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            await client.enumerate_heap_sources()
        assert client.heap_built is False
        assert client.unfinished_sessions == ["FOL"]

    async def test_spawn_failure_is_fail_closed(self):
        client = IsabelleLSPClient(logic="FOL")
        client.heap_built = False  # must be reset, not left over
        spawn = AsyncMock(side_effect=OSError("no isabelle on PATH"))
        with patch("asyncio.create_subprocess_exec", spawn):
            with pytest.raises(IsabelleToolError, match="build status"):
                await client.enumerate_heap_sources()
        assert client.heap_built is None

    async def test_timeout_is_fail_closed_and_kills_probe(self):
        client = IsabelleLSPClient(logic="FOL")
        proc = MagicMock()
        proc.returncode = None  # probe still running when the timeout hits
        proc.pid = 4242
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
                patch("isabelle_mcp.lsp_client.os.killpg") as killpg:
            with pytest.raises(IsabelleToolError, match="timed out"):
                await client.enumerate_heap_sources()
        # The whole process group dies (bash wrapper + java child), not just
        # the wrapper — a surviving child would hold the pipes open.
        killpg.assert_called_once()
        assert killpg.call_args[0][0] == 4242
        assert client.heap_built is None


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
