"""Tests for the unicode guard: ASCII-or-nothing policy on the MCP push paths.

A file whose glyphs all convert to Isabelle ASCII is atomically rewritten on
disk (compare-and-replace); a file with non-convertible non-ASCII is left
untouched and only warned about (deduplicated). Warnings ride the next tool
response via the server middleware.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from isabelle_mcp import unicode_guard
from isabelle_mcp.lsp_client import IsabelleLSPClient, _stat_sig
from isabelle_mcp.unicode_guard import (
    _replace_if_unchanged,
    drain_warnings,
    record_warning,
    sanitize_read,
)


@pytest.fixture(autouse=True)
def _clear_guard_state():
    unicode_guard._pending.clear()
    unicode_guard._last_nonascii_sig.clear()
    yield
    unicode_guard._pending.clear()
    unicode_guard._last_nonascii_sig.clear()


def _sanitize_and_queue(path: str) -> str:
    """Run sanitize_read and queue its bullet like the lsp_client hooks do."""
    text, bullet = sanitize_read(path)
    if bullet is not None:
        record_warning(path, bullet)
    return text


class TestSanitizeRead:
    def test_ascii_passthrough_no_write_no_warning(self, tmp_path):
        f = tmp_path / "Foo.thy"
        text = 'theory Foo begin lemma "1 = 1" by simp end'
        f.write_text(text)
        sig = _stat_sig(str(f))
        assert _sanitize_and_queue(str(f)) == text
        assert _stat_sig(str(f)) == sig          # file untouched
        assert drain_warnings() is None

    def test_fully_convertible_rewrites_disk(self, tmp_path):
        f = tmp_path / "Foo.thy"
        f.write_text('theory Foo begin lemma "α = α ⟹ True" by simp end\n',
                     encoding="utf-8")
        result = _sanitize_and_queue(str(f))
        expected = (
            'theory Foo begin lemma '
            '"\\<alpha> = \\<alpha> \\<Longrightarrow> True" by simp end\n'
        )
        assert result == expected
        assert f.read_text(encoding="utf-8") == expected
        warning = drain_warnings()
        assert warning is not None
        assert str(f) in warning
        assert "α→\\<alpha> (×2)" in warning
        assert "⟹→\\<Longrightarrow> (×1)" in warning
        assert "REWRITTEN" in warning
        assert "MUST write Isabelle ASCII" in warning
        assert drain_warnings() is None          # drained

    def test_subsup_glyphs_convert(self, tmp_path):
        f = tmp_path / "Foo.thy"
        f.write_text('lemma "x₁ = x₁"\n', encoding="utf-8")
        result = _sanitize_and_queue(str(f))
        assert result == 'lemma "x\\<^sub>1 = x\\<^sub>1"\n'

    def test_bom_stripped_when_rest_converts(self, tmp_path):
        f = tmp_path / "Foo.thy"
        f.write_text('\ufefflemma "α"\n', encoding="utf-8")
        result = _sanitize_and_queue(str(f))
        assert result == 'lemma "\\<alpha>"\n'
        assert f.read_text(encoding="utf-8") == result
        warning = drain_warnings()
        assert warning is not None
        assert "BOM" in warning and "REWRITTEN" in warning

    def test_unconvertible_not_rewritten_but_warned(self, tmp_path):
        f = tmp_path / "Foo.thy"
        text = "(* 中文注释 *)\nlemma t: True by simp\n"
        f.write_text(text, encoding="utf-8")
        sig = _stat_sig(str(f))
        assert _sanitize_and_queue(str(f)) == text
        assert _stat_sig(str(f)) == sig          # never written
        warning = drain_warnings()
        assert warning is not None
        assert "NOT rewritten" in warning
        assert "line 1" in warning
        assert "中" in warning

    def test_mixed_file_is_not_rewritten(self, tmp_path):
        """ASCII-or-nothing: a convertible α next to CJK leaves the file alone."""
        f = tmp_path / "Foo.thy"
        text = '(* 注 *)\nlemma "α = α"\n'
        f.write_text(text, encoding="utf-8")
        sig = _stat_sig(str(f))
        assert _sanitize_and_queue(str(f)) == text   # original pushed
        assert _stat_sig(str(f)) == sig              # disk untouched
        warning = drain_warnings()
        assert warning is not None
        assert "NOT rewritten" in warning
        assert "α→\\<alpha>" in warning and "yourself" in warning
        assert "line 1" in warning and "注" in warning

    def test_warn_only_deduplicated_until_glyph_set_changes(self, tmp_path):
        f = tmp_path / "Foo.thy"
        f.write_text("(* 中 *)\n", encoding="utf-8")
        _sanitize_and_queue(str(f))
        assert drain_warnings() is not None
        # Same non-ASCII set (an edit elsewhere): no new warning.
        f.write_text("(* 中 *)\nlemma t: True by simp\n", encoding="utf-8")
        _sanitize_and_queue(str(f))
        assert drain_warnings() is None
        # New non-ASCII char appears: warns again.
        f.write_text("(* 中文 *)\n", encoding="utf-8")
        _sanitize_and_queue(str(f))
        assert drain_warnings() is not None
        # File goes clean, then dirty again: warns again.
        f.write_text("clean\n")
        _sanitize_and_queue(str(f))
        f.write_text("(* 中文 *)\n", encoding="utf-8")
        _sanitize_and_queue(str(f))
        assert drain_warnings() is not None

    def test_glyph_sample_capped_per_line(self, tmp_path):
        f = tmp_path / "Foo.thy"
        f.write_text("(* 一二三四五六七八 *)\n", encoding="utf-8")
        _sanitize_and_queue(str(f))
        warning = drain_warnings()
        assert warning is not None
        assert "characters: 一二三四五…" in warning
        assert "六" not in warning

    def test_write_failure_pushes_original(self, tmp_path, monkeypatch):
        def boom(path, new_text, expected):
            raise OSError("disk full")
        monkeypatch.setattr(unicode_guard, "_replace_if_unchanged", boom)
        f = tmp_path / "Foo.thy"
        text = 'lemma "α = α"\n'
        f.write_text(text, encoding="utf-8")
        # Disk stays the source of truth: original content is pushed.
        assert _sanitize_and_queue(str(f)) == text
        assert f.read_text(encoding="utf-8") == text
        warning = drain_warnings()
        assert warning is not None
        assert "FAILED" in warning and "disk full" in warning
        assert "yourself" in warning

    def test_raced_out_returns_latest_content_silently(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            unicode_guard, "_replace_if_unchanged", lambda *a, **kw: False,
        )
        f = tmp_path / "Foo.thy"
        f.write_text('lemma "α"\n', encoding="utf-8")
        text, bullet = sanitize_read(str(f))
        assert text == 'lemma "α"\n'                 # latest disk content
        assert bullet is None
        assert f.read_text(encoding="utf-8") == 'lemma "α"\n'

    def test_multiple_files_one_bullet_each_latest_wins(self, tmp_path):
        a, b = tmp_path / "A.thy", tmp_path / "B.thy"
        a.write_text('lemma "α"\n', encoding="utf-8")
        b.write_text('lemma "β"\n', encoding="utf-8")
        _sanitize_and_queue(str(a))
        _sanitize_and_queue(str(b))
        a.write_text('lemma "γ"\n', encoding="utf-8")
        _sanitize_and_queue(str(a))
        warning = drain_warnings()
        assert warning is not None
        assert warning.count(str(a)) == 1
        assert "γ→\\<gamma>" in warning and "α→" not in warning
        assert "β→\\<beta>" in warning


class TestReplaceIfUnchanged:
    def test_replaces_when_content_matches(self, tmp_path):
        f = tmp_path / "Foo.thy"
        f.write_text("old")
        assert _replace_if_unchanged(str(f), "new", expected="old") is True
        assert f.read_text() == "new"

    def test_aborts_on_concurrent_change(self, tmp_path):
        """The modified-since-read fence: a newer external write is never clobbered."""
        f = tmp_path / "Foo.thy"
        f.write_text("newer external write")
        assert _replace_if_unchanged(str(f), "converted-stale", expected="stale") is False
        assert f.read_text() == "newer external write"
        assert not list(tmp_path.glob(".isabelle-mcp-*"))   # tempfile cleaned up

    def test_preserves_file_mode(self, tmp_path):
        import os
        f = tmp_path / "Foo.thy"
        f.write_text("old")
        os.chmod(str(f), 0o600)
        _replace_if_unchanged(str(f), "new", expected="old")
        assert (os.stat(str(f)).st_mode & 0o777) == 0o600


def _mock_process_client() -> IsabelleLSPClient:
    client = IsabelleLSPClient()
    client.process = MagicMock()
    client.process.stdin = MagicMock()
    client.process.stdin.write = MagicMock()
    client.process.stdin.drain = AsyncMock()
    return client


class TestPushPathsConvert:
    @pytest.mark.asyncio
    async def test_open_document_converts_disk_and_model(self, tmp_path):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text('theory Foo begin lemma "α = α" oops end', encoding="utf-8")
        await client.open_document(str(f), wait_for_diagnostics=False)
        doc = client.open_documents[str(f)]
        assert "\\<alpha>" in doc.content and "α" not in doc.content
        assert doc.content == f.read_text(encoding="utf-8")
        assert doc.stat_sig == _stat_sig(str(f))   # sig taken AFTER the rewrite
        assert drain_warnings() is not None

    @pytest.mark.asyncio
    async def test_resync_pushes_ascii_and_rewrites_disk(self, tmp_path):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f), wait_for_diagnostics=False)
        f.write_text('theory Foo begin lemma "α = α" oops end', encoding="utf-8")
        client.notify = AsyncMock()
        await client.resync_changed_open_documents()
        client.notify.assert_called_once()
        method, params = client.notify.call_args[0]
        assert method == "textDocument/didChange"
        pushed = params["contentChanges"][0]["text"]
        assert "\\<alpha>" in pushed and "α" not in pushed
        assert f.read_text(encoding="utf-8") == pushed
        assert client.open_documents[str(f)].stat_sig == _stat_sig(str(f))
        # The rewrite settled: a second resync sends nothing.
        client.notify.reset_mock()
        await client.resync_changed_open_documents()
        client.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_resync_mixed_file_pushes_original(self, tmp_path):
        client = _mock_process_client()
        f = tmp_path / "Foo.thy"
        f.write_text("theory Foo begin end")
        await client.open_document(str(f), wait_for_diagnostics=False)
        text = 'theory Foo (* 注 *) begin lemma "α" oops end'
        f.write_text(text, encoding="utf-8")
        client.notify = AsyncMock()
        await client.resync_changed_open_documents()
        pushed = client.notify.call_args[0][1]["contentChanges"][0]["text"]
        assert pushed == text                        # original, disk untouched
        assert f.read_text(encoding="utf-8") == text
        assert drain_warnings() is not None


class TestUnicodeWarningMiddleware:
    @pytest.mark.asyncio
    async def test_appends_warning_then_passes_through(self, tmp_path):
        from fastmcp.tools.tool import ToolResult
        from mcp.types import TextContent

        from isabelle_mcp.server import UnicodeWarningMiddleware

        f = tmp_path / "Foo.thy"
        f.write_text('lemma "α"\n', encoding="utf-8")
        _sanitize_and_queue(str(f))

        middleware = UnicodeWarningMiddleware()

        async def call_next(context):
            return ToolResult(content=[TextContent(type="text", text="ok")])

        result = await middleware.on_call_tool(MagicMock(), call_next)
        assert len(result.content) == 2
        assert "NON-ASCII DETECTED" in result.content[1].text

        # Queue drained: the next call is untouched.
        result2 = await middleware.on_call_tool(MagicMock(), call_next)
        assert len(result2.content) == 1

    @pytest.mark.asyncio
    async def test_tool_error_keeps_queue_for_next_call(self, tmp_path):
        from fastmcp.tools.tool import ToolResult
        from mcp.types import TextContent

        from isabelle_mcp.server import UnicodeWarningMiddleware

        f = tmp_path / "Foo.thy"
        f.write_text('lemma "α"\n', encoding="utf-8")
        _sanitize_and_queue(str(f))

        middleware = UnicodeWarningMiddleware()

        async def failing(context):
            raise RuntimeError("tool failed")

        with pytest.raises(RuntimeError):
            await middleware.on_call_tool(MagicMock(), failing)

        async def call_next(context):
            return ToolResult(content=[TextContent(type="text", text="ok")])

        result = await middleware.on_call_tool(MagicMock(), call_next)
        assert len(result.content) == 2
        assert "NON-ASCII DETECTED" in result.content[1].text

    @pytest.mark.asyncio
    async def test_non_toolresult_passthrough_keeps_queue(self, tmp_path):
        """Task-augmented calls (CreateTaskResult) must not crash or eat the queue."""
        from isabelle_mcp.server import UnicodeWarningMiddleware

        f = tmp_path / "Foo.thy"
        f.write_text('lemma "α"\n', encoding="utf-8")
        _sanitize_and_queue(str(f))

        middleware = UnicodeWarningMiddleware()
        sentinel = object()

        async def call_next(context):
            return sentinel

        result = await middleware.on_call_tool(MagicMock(), call_next)
        assert result is sentinel
        assert drain_warnings() is not None          # queue preserved
