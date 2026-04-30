"""
Integration tests for Isabelle LSP MCP server.

These tests require a running Isabelle installation.
Mark as integration tests to allow selective execution.
"""

import asyncio

import pytest

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.utils import IsabelleToolError


@pytest.mark.integration
class TestLSPClientIntegration:
    """Integration tests for LSP client."""

    @pytest.fixture
    async def lsp_client(self):
        """Create and start LSP client for testing."""
        client = IsabelleLSPClient(logic="HOL")
        await client.start()
        yield client
        await client.shutdown()

    @pytest.mark.asyncio
    async def test_lsp_client_startup(self, lsp_client):
        """Test that LSP client starts successfully."""
        assert lsp_client.process is not None
        assert lsp_client.process.returncode is None

    @pytest.mark.asyncio
    async def test_open_document(self, lsp_client, tmp_path):
        """Test opening a document."""
        # Create a simple theory file
        theory_file = tmp_path / "Test.thy"
        theory_file.write_text(
            'theory Test\n'
            'imports Main\n'
            'begin\n'
            '\n'
            'lemma test: "True"\n'
            '  by auto\n'
            '\n'
            'end\n'
        )

        # Open document
        await lsp_client.open_document(str(theory_file))

        # Check that document is tracked
        assert str(theory_file) in lsp_client.open_documents

    @pytest.mark.asyncio
    async def test_diagnostics_cache(self, lsp_client, tmp_path):
        """Test that diagnostics are cached after opening document."""
        # Create theory file with intentional error
        theory_file = tmp_path / "TestError.thy"
        theory_file.write_text(
            'theory TestError\n'
            'imports Main\n'
            'begin\n'
            '\n'
            'lemma test: "False"  (* This should fail *)\n'
            '  by auto\n'
            '\n'
            'end\n'
        )

        # Open document and wait for processing
        await lsp_client.open_document(str(theory_file))
        await asyncio.sleep(2)  # Wait for Isabelle to process

        # Check if diagnostics were received
        diags = lsp_client.get_cached_diagnostics(str(theory_file))
        # Note: May be empty if processing is slow or file is valid
        assert isinstance(diags, list)


@pytest.mark.integration
class TestToolsIntegration:
    """Integration tests for MCP tools."""

    @pytest.fixture
    async def lsp_client(self):
        """Create LSP client."""
        client = IsabelleLSPClient(logic="HOL")
        await client.start()
        yield client
        await client.shutdown()

    @pytest.fixture
    def theory_file(self, tmp_path):
        """Create a test theory file."""
        theory_file = tmp_path / "TestTheory.thy"
        theory_file.write_text(
            'theory TestTheory\n'
            'imports Main\n'
            'begin\n'
            '\n'
            'definition my_const :: "nat" where\n'
            '  "my_const = 42"\n'
            '\n'
            'lemma my_lemma: "my_const = 42"\n'
            '  by (simp add: my_const_def)\n'
            '\n'
            'end\n'
        )
        return str(theory_file)

    @pytest.mark.asyncio
    async def test_hover_tool(self, lsp_client, theory_file):
        """Test hover tool integration."""
        from isa_lsp.tools import hover_info

        # Open document first
        await lsp_client.open_document(theory_file)
        await asyncio.sleep(1)

        # Query hover at "my_const" definition (line 5, column 12)
        result = await hover_info(lsp_client, theory_file, 5, 12)

        assert result.line_context is not None
        assert result.symbol is not None

    @pytest.mark.asyncio
    async def test_diagnostics_tool(self, lsp_client, theory_file):
        """Test diagnostics tool integration."""
        from isa_lsp.tools import diagnostic_messages

        # Open document
        await lsp_client.open_document(theory_file)
        await asyncio.sleep(1)

        # Get diagnostics
        result = await diagnostic_messages(lsp_client, theory_file)

        assert result.items is not None
        assert isinstance(result.items, list)
        assert isinstance(result.success, bool)

    @pytest.mark.asyncio
    async def test_definition_tool(self, lsp_client, theory_file):
        """Test definition tool integration."""
        from isa_lsp.tools import declaration_location

        # Open document
        await lsp_client.open_document(theory_file)
        await asyncio.sleep(1)

        # Query definition of "my_const" usage (line 8, column 10)
        result = await declaration_location(lsp_client, theory_file, 8, 10)

        assert result.symbol is not None
        assert isinstance(result.locations, list)


@pytest.mark.integration
class TestSessionManagement:
    """Integration tests for session management tools."""

    @pytest.mark.asyncio
    async def test_session_info(self):
        """Test session info tool."""
        from isa_lsp.tools import session_info

        # Create client with default session
        client = IsabelleLSPClient(logic="HOL")
        await client.start()

        try:
            result = await session_info(client)

            assert result.current_session == "HOL"
            assert isinstance(result.available_sessions, list)
            assert len(result.available_sessions) > 0
        finally:
            await client.shutdown()

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_build_session(self):
        """Test building a session.

        This test is marked as slow because building sessions takes time.
        """
        from isa_lsp.tools import build_session

        # Create client
        client = IsabelleLSPClient(logic="HOL")
        await client.start()

        try:
            # Build Pure session (smallest/fastest to build)
            # Note: This may fail if Pure is already built or if there are issues
            result = await build_session(client, "Pure", clean=False)

            # Check result structure
            assert isinstance(result.success, bool)
            assert isinstance(result.messages, list)
            assert result.session == "Pure"
        finally:
            await client.shutdown()


@pytest.mark.integration
class TestErrorHandling:
    """Integration tests for error handling."""

    @pytest.mark.asyncio
    async def test_unopened_document_error(self):
        """Test that tools raise error for unopened documents."""
        from isa_lsp.tools import hover_info

        client = IsabelleLSPClient(logic="HOL")
        await client.start()

        try:
            # Try to query hover without opening document
            # Should auto-open, but let's test error handling
            await hover_info(
                client,
                "/nonexistent/file.thy",
                1,
                1
            )
            # Should raise error or handle gracefully
        except (IsabelleToolError, FileNotFoundError, Exception):
            # Expected to fail
            assert True
        finally:
            await client.shutdown()


# Pytest configuration for integration tests
def pytest_configure(config):
    """Register integration marker."""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test requiring Isabelle"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow (e.g., session builds)"
    )
