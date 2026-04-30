"""Integration tests requiring a running Isabelle installation."""


import pytest

from isa_lsp.lsp_client import IsabelleLSPClient


@pytest.mark.integration
class TestLSPClientIntegration:
    @pytest.fixture
    async def lsp_client(self):
        client = IsabelleLSPClient(logic="HOL")
        await client.start()
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
        await client.start()
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
        from isa_lsp.tools import hover_info
        await lsp_client.open_document(theory_file)
        result = await hover_info(lsp_client, theory_file, 5, 12)
        assert isinstance(result.line_context, str)

    @pytest.mark.asyncio
    async def test_diagnostics(self, lsp_client, theory_file):
        from isa_lsp.tools import diagnostic_messages
        await lsp_client.open_document(theory_file)
        result = await diagnostic_messages(lsp_client, theory_file)
        assert isinstance(result.items, list)

    @pytest.mark.asyncio
    async def test_definition(self, lsp_client, theory_file):
        from isa_lsp.tools import declaration_location
        await lsp_client.open_document(theory_file)
        result = await declaration_location(lsp_client, theory_file, 8, 10)
        assert isinstance(result.locations, list)
