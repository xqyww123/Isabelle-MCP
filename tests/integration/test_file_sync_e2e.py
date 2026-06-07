"""End-to-end regression test for the event-driven file-sync against a REAL
Isabelle ``vscode_server``.

Drives the actual server code paths (not reimplementations):
  - ``server._file_change_sink``   — the inotify event sink (Layer 1)
  - ``server._ensure_lsp_started`` — the tool-call backstop (Layer 2 + Layer 3)

and observes the effect through real diagnostics on the host theory.

Marked ``integration`` (deselected by default: ``-m "not integration"``) and skipped
unless ``isabelle`` is on PATH and an inotify-backed watcher actually starts — the
headline behaviour under test is the event-driven push, which needs inotify.

Run with the vendored Isabelle2024:
    PATH=contrib/Isabelle2024/bin:$PATH pytest tests/integration -m integration
"""
import asyncio
import os
import shutil
import time

import pytest

from isabelle_mcp.file_watcher import FileWatcher
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.utils import LSPCharacter, LSPLine

pytestmark = pytest.mark.integration

if shutil.which("isabelle") is None:
    pytest.skip("isabelle not on PATH", allow_module_level=True)


# ── theory sources ─────────────────────────────────────────────────────

def _helper_src(v: int) -> str:
    return f"val helper_val = {v};\n"


def _lib_src(v: int) -> str:
    return (f"theory Lib\nimports Main\nbegin\n"
            f"definition lib_const :: nat where \"lib_const = {v}\"\nend\n")


def _host_src(host_ok: bool) -> str:
    rhs = "2" if host_ok else "3"
    return (
        "theory E2E\n"
        "imports Lib\n"
        "begin\n"
        "ML_file ‹Helper.ML›\n"
        "ML ‹val _ = if helper_val = 1 then () else error \"ML_STALE\"›\n"
        "lemma dep_lemma: \"lib_const = 1\" by (simp add: lib_const_def)\n"
        f"lemma host_lemma: \"(2::nat) = {rhs}\" by simp\n"
        "end\n"
    )


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _write_atomic(path: str, text: str) -> None:
    """Write via os.replace — an atomic rename, exactly how Claude Edit/Write saves.
    inotify surfaces this only as a ``moved`` event (the case the old watcher missed)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ── diagnostic observation (Isabelle proof failures carry severity=None) ─

def _msgs(client: IsabelleLSPClient, host: str) -> list[str]:
    return [d.get("message", "") or ""
            for d in client.get_cached_diagnostics(host) if isinstance(d, dict)]


def _host_error(client: IsabelleLSPClient, host: str) -> bool:
    return any("Failed to finish proof" in m for m in _msgs(client, host))


def _ml_stale(client: IsabelleLSPClient, host: str) -> bool:
    return any("ML_STALE" in m for m in _msgs(client, host))


async def _wait_until(pred, timeout: float = 30.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.25)
    return False


@pytest.mark.asyncio
async def test_file_sync_end_to_end(tmp_path):
    import isabelle_mcp.server as server

    d = str(tmp_path)
    host = os.path.join(d, "E2E.thy")
    lib = os.path.join(d, "Lib.thy")
    helper = os.path.join(d, "Helper.ML")
    _write(helper, _helper_src(1))            # valid
    _write(lib, _lib_src(1))                  # valid
    _write(host, _host_src(host_ok=False))    # host_lemma broken

    client = IsabelleLSPClient(logic="HOL")
    fw = FileWatcher()
    fw.start()
    if not fw._inotify_enabled:
        pytest.skip("inotify-backed watcher unavailable in this environment")

    # Wire exactly like server_lifespan, and exercise the real server globals.
    saved = (server._lsp_client, server._file_watcher)
    server._lsp_client = client
    server._file_watcher = fw
    fw.set_sink(asyncio.get_running_loop(), server._file_change_sink)
    client.file_watcher = fw

    try:
        await client.start()
        assert client.vscode_load_delay > 0  # read from `isabelle options`

        await client.open_document(host, wait_for_diagnostics=True, diagnostic_timeout=10.0)
        await client.set_caret(host, LSPLine(7), LSPCharacter(0))
        # open_document registered the parent dir with the watcher.
        assert d in fw._watched_dirs

        # S0 — baseline: processing actually reaches the broken host_lemma.
        assert await _wait_until(lambda: _host_error(client, host), timeout=120.0), \
            "host_lemma never errored — processing/perspective problem"

        # S1 — Layer 1: atomic-rename edit on disk, NO tool call → inotify push clears it.
        _write_atomic(host, _host_src(host_ok=True))
        assert await _wait_until(lambda: not _host_error(client, host), timeout=20.0), \
            "Layer 1 (inotify atomic-rename push) did not clear the error"

        # S2 — Layer 2: watcher OFF, break on disk → tool-call stat backstop must catch it.
        fw.stop()
        _write_atomic(host, _host_src(host_ok=False))
        await asyncio.sleep(1.0)
        assert not _host_error(client, host)   # stale model: no error yet
        await server._ensure_lsp_started()      # the real tool-call entry backstop
        assert await _wait_until(lambda: _host_error(client, host), timeout=20.0), \
            "Layer 2 (stat backstop) did not push the edit with inotify disabled"

        # restore the host to clean (via the backstop) before the dependency checks
        _write_atomic(host, _host_src(host_ok=True))
        await server._ensure_lsp_started()
        await client.set_caret(host, LSPLine(7), LSPCharacter(0))
        assert await _wait_until(lambda: not _host_error(client, host), timeout=20.0)

        # S3 — dependencies are the SERVER's job: edit the .ML blob the MCP never opened,
        # with NO MCP action; Isabelle's own File_Watcher must sync it both ways.
        _write(helper, _helper_src(2))
        assert await _wait_until(lambda: _ml_stale(client, host), timeout=30.0), \
            "server File_Watcher did not pick up the .ML break"
        _write(helper, _helper_src(1))
        assert await _wait_until(lambda: not _ml_stale(client, host), timeout=30.0), \
            "server File_Watcher did not pick up the .ML fix"

        # S4 — Layer 3 detection: a fresh dep edit yields a wait == vscode_load_delay.
        from isabelle_mcp.evaluation import _dependency_freshness_wait
        await _dependency_freshness_wait(client)   # prime sigs (no 'prev' → no wait)
        _write(helper, _helper_src(2))
        wait = await _dependency_freshness_wait(client)
        assert wait == pytest.approx(client.vscode_load_delay), \
            f"Layer 3 should wait vscode_load_delay on a fresh dep edit, got {wait}"
        _write(helper, _helper_src(1))
    finally:
        try:
            await asyncio.wait_for(client.shutdown(), timeout=30.0)
        except Exception:
            pass
        fw.stop()
        server._lsp_client, server._file_watcher = saved
