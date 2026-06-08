"""Watch directories for .thy/.ML changes and push them to Isabelle immediately.

Event-driven, no polling and no dirty-set: a watchdog observer fires a handler on
its own thread, and we hand the changed path to a *sink* coroutine scheduled onto
the MCP event loop (``run_coroutine_threadsafe``). The watch-set is maintained by
``add_watch``/``remove_watch`` hooks called from the LSP client's
``open_document``/``close_document`` — only the parent directories of currently
editor-opened files are watched. Dependency files (``.ML`` blobs, imported ``.thy``)
are handled by Isabelle's own vscode_server File_Watcher, not here.
"""

import asyncio
import logging
import os
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

WATCHED_EXTENSIONS = {".thy", ".ML"}
MAX_WATCHED_DIRS = 200


def _inotify_instances_available() -> int:
    """Estimate how many inotify instances are still available for this user."""
    try:
        with open("/proc/sys/fs/inotify/max_user_instances") as f:
            limit = int(f.read().strip())
    except (OSError, ValueError):
        return 0
    used = 0
    uid = os.getuid()
    try:
        for entry in os.scandir("/proc"):
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            try:
                stat = os.stat(entry.path)
                if stat.st_uid != uid:
                    continue
                fd_dir = os.path.join(entry.path, "fd")
                for fd_entry in os.scandir(fd_dir):
                    try:
                        target = os.readlink(fd_entry.path)
                        if "inotify" in target:
                            used += 1
                    except OSError:
                        pass
            except (OSError, PermissionError):
                pass
    except OSError:
        return 0
    return max(0, limit - used)


# A sink takes a canonical changed path and returns a coroutine that syncs it.
Sink = Callable[[str], Coroutine[Any, Any, None]]


class FileWatcher:
    """Event-driven directory watcher for editor-opened ``.thy``/``.ML`` files.

    The inotify observer is best-effort: it is skipped entirely when the system is
    near the inotify instance limit (to avoid starving Isabelle's own File_Watcher).
    When disabled, the tool-call stat backstop (Layer 2) still catches every edit.
    """

    def __init__(self) -> None:
        self._observer: Any = None
        self._handler: Any = None
        self._inotify_enabled = False
        # directory -> watchdog ObservedWatch (kept so we can unschedule it)
        self._watched_dirs: dict[str, Any] = {}
        # Event loop + per-path async sink, wired once via set_sink().
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sink: Sink | None = None

    def set_sink(self, loop: asyncio.AbstractEventLoop, sink: Sink) -> None:
        """Wire the event loop and the per-path async sink (called once at startup).

        On each relevant change the watcher schedules ``sink(path)`` onto *loop*.
        """
        self._loop = loop
        self._sink = sink

    def start(self) -> None:
        avail = _inotify_instances_available()
        # Reserve at least 10 instances for Isabelle and other processes.
        if avail < 10:
            logger.warning(
                "Only %d inotify instances available (need 10+ headroom); "
                "disabling filesystem watcher. Tool-call stat backstop still syncs.",
                avail,
            )
            return

        try:
            from watchdog.events import FileSystemEvent, FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.warning("watchdog not installed; filesystem watcher disabled")
            return

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_modified(self, event: FileSystemEvent) -> None:
                # In-place saves (codex apply_patch, editors that rewrite the file).
                if not event.is_directory and isinstance(event.src_path, str):
                    watcher._dispatch(event.src_path)

            def on_created(self, event: FileSystemEvent) -> None:
                # vim and friends recreate the file on save.
                if not event.is_directory and isinstance(event.src_path, str):
                    watcher._dispatch(event.src_path)

            def on_deleted(self, event: FileSystemEvent) -> None:
                if not event.is_directory and isinstance(event.src_path, str):
                    watcher._dispatch(event.src_path)

            def on_moved(self, event: FileSystemEvent) -> None:
                # Atomic-rename saves (Claude Edit/Write, jEdit, sed) surface ONLY
                # here: dest_path is the real, updated file. This is the case the
                # old (modified/created-only) watcher missed 100% of the time.
                dest = getattr(event, "dest_path", None)
                if isinstance(dest, str):
                    watcher._dispatch(dest)
                if not event.is_directory and isinstance(event.src_path, str):
                    watcher._dispatch(event.src_path)

        self._observer = Observer()
        self._observer.daemon = True
        self._handler = _Handler()
        self._observer.start()
        self._inotify_enabled = True
        logger.info(
            "Filesystem watcher started (%d inotify instances available, dir limit=%d)",
            avail, min(MAX_WATCHED_DIRS, avail - 10),
        )

    def stop(self) -> None:
        if self._observer is not None and self._inotify_enabled:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._inotify_enabled = False
        self._watched_dirs.clear()

    def add_watch(self, directory: str) -> bool:
        """Watch *directory* (non-recursive). Idempotent; best-effort under limits.

        Called from ``open_document`` with the file's parent directory. We watch the
        directory, not the file inode, so atomic-rename saves (which replace the
        inode) are still observed.
        """
        if not self._inotify_enabled or self._observer is None:
            return False
        if directory in self._watched_dirs:
            return True
        if len(self._watched_dirs) >= MAX_WATCHED_DIRS:
            logger.warning("Global dir watch limit reached (%d), skipping %s",
                           MAX_WATCHED_DIRS, directory)
            return False
        avail = _inotify_instances_available()
        if avail < 10:
            logger.warning("inotify headroom low (%d), skipping watch for %s",
                           avail, directory)
            return False
        try:
            watch = self._observer.schedule(self._handler, directory, recursive=False)
        except OSError as e:
            logger.warning("Failed to watch %s: %s", directory, e)
            return False
        self._watched_dirs[directory] = watch
        logger.info("Watching %s [%d/%d dirs]",
                    directory, len(self._watched_dirs), MAX_WATCHED_DIRS)
        return True

    def remove_watch(self, directory: str) -> None:
        """Stop watching *directory*. Called from ``close_document``/``shutdown``."""
        watch = self._watched_dirs.pop(directory, None)
        if watch is None or self._observer is None:
            return
        try:
            self._observer.unschedule(watch)
            logger.info("Unwatched %s", directory)
        except (KeyError, OSError) as e:
            logger.debug("unschedule %s failed: %s", directory, e)

    def clear_watches(self) -> None:
        """Drop all directory watches without stopping the observer.

        Called on ``isabelle_terminate``: ``shutdown()`` clears ``open_documents``
        directly (bypassing ``close_document``), so without this the watched dirs
        would accumulate across launch/terminate cycles toward ``MAX_WATCHED_DIRS``.
        """
        for directory in list(self._watched_dirs):
            self.remove_watch(directory)

    def _dispatch(self, path: str) -> None:
        """Schedule the sink for *path* if it is a watched extension (thread-safe).

        Runs on the watchdog observer thread, so it must not touch the event loop
        directly — it hands the coroutine to the loop via run_coroutine_threadsafe.
        """
        if os.path.splitext(path)[1] not in WATCHED_EXTENSIONS:
            return
        loop, sink = self._loop, self._sink
        if loop is None or sink is None:
            return
        abs_path = os.path.realpath(path)
        try:
            future = asyncio.run_coroutine_threadsafe(sink(abs_path), loop)
        except RuntimeError:
            # Loop is closed or not running (shutdown in flight) — drop the event.
            return
        # Consume any exception so it isn't logged as "never retrieved" at GC.
        future.add_done_callback(_swallow_future_exception)
        logger.debug("File change dispatched: %s", abs_path)


def _swallow_future_exception(future: "Any") -> None:
    try:
        future.result()
    except Exception:  # noqa: BLE001 — sink logs its own errors; just don't leak.
        logger.debug("File-sync sink raised", exc_info=True)
