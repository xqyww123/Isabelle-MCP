import pytest

from isabelle_mcp.evaluation import (
    cancel_evaluation,
    evaluate_to,
    evaluation_state,
    evaluation_status,
)
from isabelle_mcp.utils import IsabelleToolError


class MockProcessingTracker:
    def __init__(self, *, all_processed: bool = True):
        self._all_processed = all_processed

    def range_processed(self, start_line, end_line):
        return self._all_processed

    def line_reached(self, line):
        return self._all_processed

    def line_running(self, line):
        return False

    @property
    def all_processed(self):
        return self._all_processed

    def require_fresh_update(self):
        pass

    def get_running_ranges(self):
        return []

    def get_running_ranges_with_onset(self):
        return []

    def get_unprocessed_ranges(self):
        return []

    async def wait_until_processed_bounded(
        self, start_line, end_line, timeout=5.0, health_check=None, check_interval=5.0,
    ):
        return self._all_processed


class TestEvaluateTo:
    @pytest.mark.asyncio
    async def test_completes_immediately(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=True,
        )
        result = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert result.status == "complete"
        assert result.destination_line == 5
        assert "complete" in result.message.lower()

    @pytest.mark.asyncio
    async def test_times_out(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        mock_lsp_client.wait_for_processing_bounded = (
            lambda *a, **kw: _async_return(False)
        )
        result = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert result.status == "in_progress"
        assert result.destination_line == 5
        assert "evaluation_status" in result.message

    @pytest.mark.asyncio
    async def test_while_active_fails(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.wait_for_processing_bounded = (
            lambda *a, **kw: _async_return(False)
        )
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert evaluation_state.active

        with pytest.raises(IsabelleToolError, match="already in progress"):
            await evaluate_to(mock_lsp_client, temp_theory_file, 10)

    @pytest.mark.asyncio
    async def test_collects_errors(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {
                "range": {"start": {"line": 4, "character": 0}, "end": {"line": 4, "character": 10}},
                "severity": 1,
                "message": "Type error",
            },
        ]
        result = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert len(result.errors) == 1
        assert result.errors[0].message == "Type error"

    @pytest.mark.asyncio
    async def test_negative_line(self, temp_theory_file, mock_lsp_client):
        result = await evaluate_to(mock_lsp_client, temp_theory_file, -1)
        assert result.status == "complete"
        assert result.destination_line is not None
        assert result.destination_line >= 11

    @pytest.mark.asyncio
    async def test_after_text_same_line(self, temp_theory_file, mock_lsp_client):
        # after_text resolves on its own line; destination stays on that line.
        result = await evaluate_to(
            mock_lsp_client, temp_theory_file, 5, after_text="my_const",
        )
        assert result.status == "complete"
        assert result.destination_line == 5

    @pytest.mark.asyncio
    async def test_after_text_spans_to_later_line(self, temp_theory_file, mock_lsp_client):
        # Snippet begins on line 8 ("... = 42\"") and ends with `by` on line 9, so
        # the resolved caret — and the destination — land on line 9.
        result = await evaluate_to(
            mock_lsp_client, temp_theory_file, 8, after_text='= 42" by',
        )
        assert result.status == "complete"
        assert result.destination_line == 9

    @pytest.mark.asyncio
    async def test_after_text_not_found(self, temp_theory_file, mock_lsp_client):
        with pytest.raises(IsabelleToolError, match="not found on line 5"):
            await evaluate_to(
                mock_lsp_client, temp_theory_file, 5, after_text="no_such_token_zzz",
            )


class TestEvaluationStatus:
    @pytest.mark.asyncio
    async def test_no_evaluation(self, mock_lsp_client):
        result = await evaluation_status(mock_lsp_client)
        assert result.status == "no_evaluation"

    @pytest.mark.asyncio
    async def test_completes(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.wait_for_processing_bounded = (
            lambda *a, **kw: _async_return(False)
        )
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        await evaluate_to(mock_lsp_client, temp_theory_file, 5)

        mock_lsp_client.wait_for_processing_bounded = (
            lambda *a, **kw: _async_return(True)
        )
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=True,
        )
        result = await evaluation_status(mock_lsp_client)
        assert result.status == "complete"

    @pytest.mark.asyncio
    async def test_incremental_errors(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.wait_for_processing_bounded = (
            lambda *a, **kw: _async_return(False)
        )
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [
            {
                "range": {"start": {"line": 2, "character": 0}, "end": {"line": 2, "character": 5}},
                "severity": 1,
                "message": "Error A",
            },
        ]
        r1 = await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert len(r1.errors) == 1

        mock_lsp_client.diagnostics_cache[temp_theory_file].append({
            "range": {"start": {"line": 3, "character": 0}, "end": {"line": 3, "character": 5}},
            "severity": 1,
            "message": "Error B",
        })
        r2 = await evaluation_status(mock_lsp_client)
        assert len(r2.errors) == 1
        assert r2.errors[0].message == "Error B"


class TestCancelEvaluation:
    @pytest.mark.asyncio
    async def test_no_evaluation(self, mock_lsp_client):
        result = await cancel_evaluation(mock_lsp_client)
        assert result.status == "no_evaluation"

    @pytest.mark.asyncio
    async def test_cancel_in_progress(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client.wait_for_processing_bounded = (
            lambda *a, **kw: _async_return(False)
        )
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert evaluation_state.active

        result = await cancel_evaluation(mock_lsp_client)
        assert result.status == "cancelled"
        assert not evaluation_state.active

    @pytest.mark.asyncio
    async def test_cancel_active_evaluation(self, temp_theory_file, mock_lsp_client):
        mock_lsp_client._processing_trackers[temp_theory_file] = MockProcessingTracker(
            all_processed=False,
        )
        await evaluate_to(mock_lsp_client, temp_theory_file, 5)
        assert evaluation_state.active

        result = await cancel_evaluation(mock_lsp_client)
        assert result.status == "cancelled"
        assert not evaluation_state.active


async def _async_return(value):
    return value
