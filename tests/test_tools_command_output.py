from typing import Any, cast

import pytest

from isa_lsp.tools.command_output import command_output
from isa_lsp.utils import IsabelleToolError


class TestCommandOutputTool:
    @pytest.mark.asyncio
    async def test_empty(self, mock_lsp_client, temp_theory_file):
        result = await command_output(mock_lsp_client, temp_theory_file, 8)
        assert result.messages == []
        assert result.line_context != ""

    @pytest.mark.asyncio
    async def test_with_output(self, mock_lsp_client, temp_theory_file):
        mock_lsp_client.dynamic_output_response = '<div class="writeln">Success</div>'
        result = await command_output(mock_lsp_client, temp_theory_file, 8)
        assert len(result.messages) == 1
        assert result.messages[0].kind == "writeln"
        assert result.messages[0].message == "Success"

    @pytest.mark.asyncio
    async def test_uses_first_non_space_character(self, temp_theory_file):
        class Client:
            open_documents = {temp_theory_file: {}}

            def __init__(self):
                self.calls: list[tuple[int, int]] = []

            async def get_dynamic_output(
                self, file_path: str, line: int, character: int = 0, timeout: float = 2.0,
            ):
                self.calls.append((line, character))
                return '<span class="writeln_message">True</span>'

        client = Client()
        result = await command_output(cast(Any, client), temp_theory_file, 9)

        assert result.messages[0].message == "True"
        assert client.calls == [(8, 2)]

    @pytest.mark.asyncio
    async def test_falls_back_to_command_body_character(self, tmp_path):
        theory_file = tmp_path / "ScratchLike.thy"
        theory_file.write_text(
            'theory ScratchLike\n'
            'imports Main\n'
            'begin\n'
            'ML \\<open>getenv "RPC_Host"\\<close>\n'
            'end\n'
        )

        class Client:
            open_documents = {str(theory_file): {}}

            def __init__(self):
                self.calls: list[tuple[int, int]] = []

            async def get_dynamic_output(
                self, file_path: str, line: int, character: int = 0, timeout: float = 2.0,
            ):
                self.calls.append((line, character))
                if character == 3:
                    return (
                        '<pre class="source"><span class="writeln_message">'
                        '<span class="block"><span class="block">val<span class="break"> </span>'
                        '<span class="block">it</span><span class="break"> </span>=</span>'
                        '<span class="break"> </span>&quot;&quot;:<span class="break"> </span>'
                        '<span class="block">string</span></span></span></pre>'
                    )
                return '<pre class="source"/>'

        client = Client()
        result = await command_output(cast(Any, client), str(theory_file), 4)

        assert result.messages[0].kind == "writeln"
        assert result.messages[0].message == 'val it = "": string'
        assert client.calls == [(3, 0), (3, 2), (3, 3)]

    @pytest.mark.asyncio
    async def test_invalid_line(self, mock_lsp_client, temp_theory_file):
        with pytest.raises(IsabelleToolError, match="line must be >= 1"):
            await command_output(mock_lsp_client, temp_theory_file, 0)
