import pytest

from isa_lsp.tools.completions import completions
from isa_lsp.utils import IsabelleToolError


class TestCompletionsTool:
    @pytest.mark.asyncio
    async def test_basic(self, mock_lsp_client, temp_theory_file, sample_completion_response):
        mock_lsp_client.completion_response = sample_completion_response
        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)
        assert len(result.items) == 3
        labels = [it.label for it in result.items]
        assert "lemma" in labels

    @pytest.mark.asyncio
    async def test_empty(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.completion_response = {"items": []}
        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)
        assert result.items == []

    @pytest.mark.asyncio
    async def test_max_completions(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.completion_response = {
            "items": [{"label": f"item_{i}", "kind": 1, "detail": "x"} for i in range(1000)]
        }
        result = await completions(mock_lsp_client, temp_theory_file, 8, 1, max_completions=100)
        assert len(result.items) == 100

    @pytest.mark.asyncio
    async def test_null_response(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.completion_response = None
        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)
        assert result.items == []

    @pytest.mark.asyncio
    async def test_unicode_labels(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.completion_response = {
            "items": [
                {"label": "∀", "kind": 1, "detail": "Universal"},
                {"label": "∃", "kind": 1, "detail": "Existential"},
                {"label": "⟹", "kind": 1, "detail": "Implies"},
            ]
        }
        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)
        assert len(result.items) == 3

    @pytest.mark.asyncio
    async def test_single_item(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.completion_response = {
            "items": [{"label": "only_one", "kind": 1, "detail": ""}]
        }
        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)
        assert len(result.items) == 1
        assert result.items[0].label == "only_one"

    @pytest.mark.asyncio
    async def test_lsp_list_response(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.completion_response = [
            {"label": "from_list", "kind": 14, "detail": "keyword"}
        ]
        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)
        assert len(result.items) == 1
        assert result.items[0].label == "from_list"

    @pytest.mark.asyncio
    async def test_invalid_max_completions(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="max_completions must be >= 1"):
            await completions(mock_lsp_client, temp_theory_file, 8, 1, max_completions=0)

    @pytest.mark.asyncio
    async def test_malformed_items_are_skipped_or_coerced(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.completion_response = {
            "items": [
                {"label": None, "kind": 14},
                {"label": "", "kind": 14},
                {
                    "label": "usable",
                    "kind": 14,
                    "textEdit": {"newText": 123},
                    "documentation": {"value": 42},
                },
            ]
        }
        result = await completions(mock_lsp_client, temp_theory_file, 8, 1)
        assert len(result.items) == 1
        assert result.items[0].label == "usable"
        assert result.items[0].insert_text == "123"
        assert result.items[0].documentation == "42"
