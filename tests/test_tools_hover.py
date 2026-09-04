import pytest

from isabelle_mcp.evaluation import NOT_EVALUATED_MESSAGE
from isabelle_mcp.tools.hover import hover_info
from isabelle_mcp.utils import IsabelleToolError, MCPLine


@pytest.fixture(autouse=True)
async def _evaluated_up_front(evaluated_theory_file):
    """Queries never evaluate: every test here starts from an evaluated file."""


async def _evaluated(client, path: str) -> str:
    await client.open_document(path, evaluation_target=True)
    return path


class TestHoverTool:
    @pytest.mark.asyncio
    async def test_basic(self, mock_lsp_client, temp_theory_file, sample_hover_response):
        mock_lsp_client.hover_response = sample_hover_response
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
        assert result.symbol == "my_const"
        assert len(result.results) >= 1
        assert isinstance(result.results[0].info, str)
        assert result.line_context != ""

    @pytest.mark.asyncio
    async def test_markdown_contents(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = {
            "contents": {"kind": "markdown", "value": "**Symbol**: `my_const`\n\nType: `nat`"}
        }
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
        assert "my_const" in result.results[0].info

    @pytest.mark.asyncio
    async def test_plaintext_contents(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = {"contents": "Simple text content"}
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
        assert result.results[0].info == "Simple text content"

    @pytest.mark.asyncio
    async def test_null_response(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = None
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
        assert len(result.results) == 1
        assert result.results[0].info == ""

    @pytest.mark.asyncio
    async def test_an_unevaluated_file_is_not_opened_but_refused(
        self, mock_lsp_client, temp_theory_with_errors,
    ):
        # Queries never evaluate — and never open a file the prover does not
        # hold. The answer is the one approved sentence.
        assert temp_theory_with_errors not in mock_lsp_client.open_documents
        mock_lsp_client.hover_response = {"contents": "test"}
        with pytest.raises(IsabelleToolError) as exc:
            await hover_info(mock_lsp_client, temp_theory_with_errors, MCPLine(5), "lemma")
        assert str(exc.value) == NOT_EVALUATED_MESSAGE.format(
            file=temp_theory_with_errors, line=5)
        assert temp_theory_with_errors not in mock_lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_with_diagnostics(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.diagnostics_cache[temp_theory_file] = [{
            "range": {"start": {"line": 4, "character": 0}, "end": {"line": 4, "character": 10}},
            "severity": 1, "message": "Type error",
        }]
        mock_lsp_client.hover_response = {"contents": "test"}
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), "definition")
        assert len(result.diagnostics) == 1
        assert result.diagnostics[0].severity == "error"
        assert result.diagnostics[0].message == "Type error"

    @pytest.mark.asyncio
    async def test_beyond_file_end(self, mock_lsp_client, temp_theory_file):
        await mock_lsp_client.open_document(temp_theory_file)
        mock_lsp_client.hover_response = None
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(1000), "x")
        assert result.symbol == "x"
        assert result.results == []
        assert result.line_context == ""

    @pytest.mark.asyncio
    async def test_a_path_that_does_not_exist_is_not_evaluated(self, mock_lsp_client):
        # A mistyped path is, truthfully, a position nothing has evaluated;
        # the missing-file signal comes back at the isabelle_evaluate_to the
        # sentence points to.
        with pytest.raises(IsabelleToolError) as exc:
            await hover_info(mock_lsp_client, "/nonexistent/file.thy", MCPLine(1), "x")
        assert str(exc.value) == NOT_EVALUATED_MESSAGE.format(
            file="/nonexistent/file.thy", line=1)

    @pytest.mark.asyncio
    async def test_array_contents(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.hover_response = {
            "contents": [{"language": "isabelle", "value": "definition"}, "Additional info"]
        }
        result = await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), "my_const")
        assert "definition" in result.results[0].info
        assert "Additional info" in result.results[0].info

    @pytest.mark.asyncio
    async def test_symbol_not_found(self, mock_lsp_client, temp_theory_file):
        from isabelle_mcp.utils import IsabelleToolError
        with pytest.raises(IsabelleToolError, match="not found on line"):
            await hover_info(mock_lsp_client, temp_theory_file, MCPLine(5), "nonexistent_sym")

    @pytest.mark.asyncio
    async def test_multi_occurrence_dedup(self, mock_lsp_client, tmp_path):
        f = tmp_path / "Multi.thy"
        f.write_text("lemma x_y: x = x\n")
        await _evaluated(mock_lsp_client, str(f))
        mock_lsp_client.hover_response = {"contents": "same info"}
        result = await hover_info(mock_lsp_client, str(f), MCPLine(1), "x")
        assert len(result.results) == 1
        assert len(result.results[0].occurrences) == 2
        assert result.results[0].occurrences == [1, 2]

    @pytest.mark.asyncio
    async def test_multi_occurrence_different_info(self, mock_lsp_client, tmp_path):
        f = tmp_path / "Diff.thy"
        f.write_text("x = x\n")
        await _evaluated(mock_lsp_client, str(f))

        def position_hover(fp, line, char):
            if int(char) == 0:
                return {"contents": "info A"}
            return {"contents": "info B"}

        mock_lsp_client.hover_response = position_hover
        result = await hover_info(mock_lsp_client, str(f), MCPLine(1), "x")
        assert len(result.results) == 2

    @pytest.mark.asyncio
    async def test_lsp_error_skips_occurrence(self, mock_lsp_client, tmp_path):
        f = tmp_path / "Err.thy"
        f.write_text("x = x\n")
        await _evaluated(mock_lsp_client, str(f))
        call_count = 0

        def error_on_first(fp, line, char):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient LSP error")
            return {"contents": "ok"}

        mock_lsp_client.hover_response = error_on_first
        result = await hover_info(mock_lsp_client, str(f), MCPLine(1), "x")
        assert len(result.results) == 1
        assert result.results[0].info == "ok"

    @pytest.mark.asyncio
    async def test_isabelle_tool_error_propagates(self, mock_lsp_client, tmp_path):
        f = tmp_path / "Fatal.thy"
        f.write_text("x = x\n")
        await _evaluated(mock_lsp_client, str(f))

        def fatal_error(fp, line, char):
            raise IsabelleToolError("LSP process crashed")

        mock_lsp_client.hover_response = fatal_error
        with pytest.raises(IsabelleToolError, match="LSP process crashed"):
            await hover_info(mock_lsp_client, str(f), MCPLine(1), "x")

    @pytest.mark.asyncio
    async def test_note_from_evaluation_guard(self, mock_lsp_client, tmp_path):
        """Issue 9: note field from check_evaluation_guard should propagate."""
        from unittest.mock import AsyncMock, patch
        f = tmp_path / "Note.thy"
        f.write_text("hello world\n")
        # The guard is patched out below, and the guard is what opens the file.
        await mock_lsp_client.open_document(str(f))
        mock_lsp_client.hover_response = {"contents": "test"}
        with patch("isabelle_mcp.tools.hover.check_evaluation_guard", new_callable=AsyncMock) as mock_guard:
            mock_guard.return_value = (
                f"The command at {f}:1 is still being executed; its output may be incomplete.")
            result = await hover_info(mock_lsp_client, str(f), MCPLine(1), "hello")
        assert result.note is not None
        assert "still being executed" in result.note
