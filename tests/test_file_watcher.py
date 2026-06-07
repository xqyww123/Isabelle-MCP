"""Tests for the event-driven FileWatcher.

Two layers of coverage:
- unit: _dispatch extension filtering + sink scheduling, add_watch/remove_watch
  bookkeeping (with a fake observer, no real inotify);
- integration: a real watchdog Observer over a tmp dir, asserting the
  **atomic-rename regression** (os.replace → inotify 'moved') is detected — the
  exact case the old modified/created-only watcher missed 100% of the time.
"""

import asyncio
import os

import pytest

from isabelle_mcp import file_watcher as fw_mod
from isabelle_mcp.file_watcher import FileWatcher


class _FakeWatch:
    def __init__(self, path):
        self.path = path


class _FakeObserver:
    """Minimal stand-in for a watchdog Observer (schedule/unschedule only)."""

    def __init__(self):
        self.scheduled: list[str] = []
        self.unscheduled: list[str] = []

    def schedule(self, handler, directory, recursive=False):
        self.scheduled.append(directory)
        return _FakeWatch(directory)

    def unschedule(self, watch):
        self.unscheduled.append(watch.path)


@pytest.fixture
def _plenty_of_inotify(monkeypatch):
    monkeypatch.setattr(fw_mod, "_inotify_instances_available", lambda: 100)


# ── Unit: _dispatch ────────────────────────────────────────────────────

class TestDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_schedules_sink_for_watched_ext(self, tmp_path):
        loop = asyncio.get_running_loop()
        got: list[str] = []

        async def sink(p):
            got.append(p)

        fw = FileWatcher()
        fw.set_sink(loop, sink)
        target = tmp_path / "Foo.thy"
        fw._dispatch(str(target))
        await asyncio.sleep(0.05)
        assert got == [os.path.realpath(str(target))]

    @pytest.mark.asyncio
    async def test_dispatch_ignores_unwatched_extensions(self, tmp_path):
        loop = asyncio.get_running_loop()
        got: list[str] = []

        async def sink(p):
            got.append(p)

        fw = FileWatcher()
        fw.set_sink(loop, sink)
        fw._dispatch(str(tmp_path / "Foo.txt"))     # not .thy/.ML
        fw._dispatch(str(tmp_path / "Foo.thy.tmp"))  # atomic-rename temp file
        fw._dispatch(str(tmp_path / "Foo.thy~"))     # editor backup
        await asyncio.sleep(0.05)
        assert got == []

    @pytest.mark.asyncio
    async def test_dispatch_without_sink_is_noop(self, tmp_path):
        fw = FileWatcher()
        fw._dispatch(str(tmp_path / "Foo.thy"))  # no loop/sink wired → must not raise


# ── Unit: add_watch / remove_watch ─────────────────────────────────────

class TestWatchSet:
    def test_add_and_remove_watch(self, _plenty_of_inotify):
        fw = FileWatcher()
        fw._inotify_enabled = True
        fw._observer = _FakeObserver()

        assert fw.add_watch("/tmp/d") is True
        assert "/tmp/d" in fw._watched_dirs
        assert fw._observer.scheduled == ["/tmp/d"]

        # Idempotent: a second add for the same dir does not re-schedule.
        assert fw.add_watch("/tmp/d") is True
        assert fw._observer.scheduled == ["/tmp/d"]

        fw.remove_watch("/tmp/d")
        assert "/tmp/d" not in fw._watched_dirs
        assert fw._observer.unscheduled == ["/tmp/d"]

    def test_add_watch_disabled_returns_false(self):
        fw = FileWatcher()  # inotify never started
        assert fw.add_watch("/tmp/d") is False
        assert fw._watched_dirs == {}

    def test_remove_unknown_watch_is_noop(self, _plenty_of_inotify):
        fw = FileWatcher()
        fw._inotify_enabled = True
        fw._observer = _FakeObserver()
        fw.remove_watch("/tmp/never-watched")  # must not raise
        assert fw._observer.unscheduled == []


# ── Integration: real inotify, atomic-rename regression ────────────────

def _start_real_watcher(loop, sink):
    fw = FileWatcher()
    fw.start()
    if not fw._inotify_enabled:
        pytest.skip("inotify/watchdog unavailable in this environment")
    fw.set_sink(loop, sink)
    return fw


class TestRealInotify:
    @pytest.mark.asyncio
    async def test_atomic_rename_is_detected(self, tmp_path):
        """os.replace (Claude Edit/Write, jEdit, sed) → 'moved' event → push."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[str] = asyncio.Queue()

        async def sink(p):
            await q.put(p)

        fw = _start_real_watcher(loop, sink)
        try:
            target = tmp_path / "Foo.thy"
            target.write_text("theory Foo begin end")
            fw.add_watch(str(tmp_path))
            await asyncio.sleep(0.1)  # let the watch settle

            tmp = tmp_path / "Foo.thy.tmp"
            tmp.write_text("theory Foo begin (*v2*) end")
            os.replace(tmp, target)  # atomic rename

            got = await asyncio.wait_for(q.get(), timeout=5.0)
            assert got == os.path.realpath(str(target))
        finally:
            fw.stop()

    @pytest.mark.asyncio
    async def test_in_place_modify_is_detected(self, tmp_path):
        """In-place rewrite (codex apply_patch) → 'modified' event → push."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[str] = asyncio.Queue()

        async def sink(p):
            await q.put(p)

        fw = _start_real_watcher(loop, sink)
        try:
            target = tmp_path / "Bar.thy"
            target.write_text("theory Bar begin end")
            fw.add_watch(str(tmp_path))
            await asyncio.sleep(0.1)

            with open(target, "a", encoding="utf-8") as f:
                f.write("\n(* appended *)\n")

            got = await asyncio.wait_for(q.get(), timeout=5.0)
            assert got == os.path.realpath(str(target))
        finally:
            fw.stop()
