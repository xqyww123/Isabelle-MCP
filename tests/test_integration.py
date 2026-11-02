"""Integration tests requiring a running Isabelle installation."""


import pytest

from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.utils import MCPLine


@pytest.mark.integration
class TestLSPClientIntegration:
    @pytest.fixture
    async def lsp_client(self):
        client = IsabelleLSPClient(logic="HOL")
        try:
            await client.start()
        except BaseException:
            # start() failing pre-handshake (e.g. HOL heap not built on this
            # machine) would otherwise leak a wedged vscode_server for the
            # whole test run.
            await client.shutdown()
            raise
        yield client
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_startup(self, lsp_client):
        assert lsp_client.process is not None
        assert lsp_client.process.returncode is None

    @pytest.mark.asyncio
    async def test_open_document(self, lsp_client, tmp_path):
        f = tmp_path / "Test.thy"
        f.write_text('theory Test\nimports Main\nbegin\nlemma test: "True"\n  by auto\nend\n')
        await lsp_client.open_document(str(f))
        assert str(f) in lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_diagnostics_cache(self, lsp_client, tmp_path):
        f = tmp_path / "TestError.thy"
        f.write_text('theory TestError\nimports Main\nbegin\nlemma test: "False"\n  by auto\nend\n')
        await lsp_client.open_document(str(f))
        diags = lsp_client.get_cached_diagnostics(str(f))
        assert isinstance(diags, list)


@pytest.mark.integration
class TestToolsIntegration:
    @pytest.fixture
    async def lsp_client(self):
        client = IsabelleLSPClient(logic="HOL")
        try:
            await client.start()
        except BaseException:
            # start() failing pre-handshake (e.g. HOL heap not built on this
            # machine) would otherwise leak a wedged vscode_server for the
            # whole test run.
            await client.shutdown()
            raise
        yield client
        await client.shutdown()

    @pytest.fixture
    def theory_file(self, tmp_path):
        f = tmp_path / "TestTheory.thy"
        f.write_text(
            'theory TestTheory\nimports Main\nbegin\n\n'
            'definition my_const :: "nat" where\n  "my_const = 42"\n\n'
            'lemma my_lemma: "my_const = 42"\n  by (simp add: my_const_def)\n\nend\n'
        )
        return str(f)

    @pytest.mark.asyncio
    async def test_hover(self, lsp_client, theory_file):
        from isabelle_mcp.tools import hover_info
        from tests.integration.test_query_tools_e2e import _evaluate_through
        assert await _evaluate_through(lsp_client, theory_file, -1)
        result = await hover_info(lsp_client, theory_file, MCPLine(5), "my_const")
        assert any(e.info == 'constant "TestTheory.my_const"' for e in result.results), result

    @pytest.mark.asyncio
    async def test_definition(self, lsp_client, theory_file):
        from isabelle_mcp.tools import declaration_location
        from tests.integration.test_query_tools_e2e import _evaluate_through
        assert await _evaluate_through(lsp_client, theory_file, -1)
        result = await declaration_location(lsp_client, theory_file, MCPLine(8), "my_const")
        assert [(l.file_path, l.line, l.column) for l in result.locations] == [(theory_file, 5, 12)]
