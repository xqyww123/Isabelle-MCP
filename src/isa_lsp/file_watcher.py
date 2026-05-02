"""Watch directories for .thy/.ML file changes and track dirty files."""

import logging
import os
import threading

logger = logging.getLogger(__name__)

WATCHED_EXTENSIONS = {".thy", ".ML"}
MAX_WATCHED_FILES = 1000
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


def _has_relevant_files(directory: str) -> bool:
    try:
        return any(
            not entry.is_dir()
            and os.path.splitext(entry.name)[1] in WATCHED_EXTENSIONS
            for entry in os.scandir(directory)
        )
    except (PermissionError, OSError):
        return False


def _count_direct_files(directory: str) -> int:
    n = 0
    try:
        for entry in os.scandir(directory):
            if (not entry.is_dir()
                    and os.path.splitext(entry.name)[1] in WATCHED_EXTENSIONS):
                n += 1
    except (PermissionError, OSError):
        pass
    return n


def _get_subdirs(directory: str) -> list[str]:
    result: list[str] = []
    try:
        for entry in os.scandir(directory):
            if entry.is_dir(follow_symlinks=False):
                result.append(entry.path)
    except (PermissionError, OSError):
        pass
    return result


class FileWatcher:
    """Track dirty .thy/.ML files via HTTP hook notifications + optional inotify.

    The HTTP hook (notify_file_changed) always works regardless of inotify.
    The inotify observer is best-effort: skipped when the system is near the
    inotify instance limit (to avoid starving Isabelle vscode_server).
    """

    def __init__(self) -> None:
        self._observer: "Observer | None" = None  # type: ignore[name-defined]
        self._watched_dirs: set[str] = set()
        self._dirty_files: set[str] = set()
        self._lock = threading.Lock()
        self._handler: "_Handler | None" = None  # type: ignore[name-defined]
        self._inotify_enabled = False
        self._total_dir_watches = 0
        self._total_file_watches = 0

    def start(self) -> None:
        avail = _inotify_instances_available()
        # Reserve at least 10 instances for Isabelle and other processes
        if avail < 10:
            logger.warning(
                "Only %d inotify instances available (need 10+ headroom); "
                "disabling filesystem watcher. HTTP hook notifications still work.",
                avail,
            )
            return

        try:
            from watchdog.events import FileSystemEvent, FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.warning("watchdog not installed; filesystem watcher disabled")
            return

        class _Handler(FileSystemEventHandler):
            def __init__(self_h, watcher: "FileWatcher") -> None:
                self_h._watcher = watcher

            def on_modified(self_h, event: FileSystemEvent) -> None:
                if not event.is_directory and isinstance(event.src_path, str):
                    self_h._watcher._on_file_changed(event.src_path)

            def on_created(self_h, event: FileSystemEvent) -> None:
                if not event.is_directory and isinstance(event.src_path, str):
                    self_h._watcher._on_file_changed(event.src_path)

        self._observer = Observer()
        self._observer.daemon = True
        self._handler = _Handler(self)
        self._observer.start()
        self._inotify_enabled = True
        effective_limit = min(MAX_WATCHED_DIRS, avail - 10)
        logger.info(
            "Filesystem watcher started (%d inotify instances available, dir limit=%d)",
            avail, effective_limit,
        )

    def stop(self) -> None:
        if self._observer is not None and self._inotify_enabled:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._inotify_enabled = False

    def notify_file_changed(self, file_path: str) -> None:
        """Called by the HTTP hook. Marks the file dirty and watches its directory."""
        abs_path = os.path.abspath(file_path)
        self._mark_dirty(abs_path)
        if self._inotify_enabled:
            self._watch_directory_of(abs_path)

    def pop_dirty_files(self) -> set[str]:
        with self._lock:
            result = self._dirty_files.copy()
            self._dirty_files.clear()
        return result

    def is_dirty(self, file_path: str) -> bool:
        with self._lock:
            return os.path.abspath(file_path) in self._dirty_files

    def clear_dirty(self, file_path: str) -> None:
        with self._lock:
            self._dirty_files.discard(os.path.abspath(file_path))

    def _mark_dirty(self, abs_path: str) -> None:
        ext = os.path.splitext(abs_path)[1]
        if ext in WATCHED_EXTENSIONS:
            with self._lock:
                self._dirty_files.add(abs_path)
            logger.info("Marked dirty: %s", abs_path)

    def _on_file_changed(self, path: str) -> None:
        self._mark_dirty(os.path.abspath(path))

    def _add_watch(self, directory: str, recursive: bool) -> bool:
        if self._observer is None:
            return False
        if directory in self._watched_dirs:
            return True
        avail = _inotify_instances_available()
        if avail < 10:
            logger.warning("inotify headroom low (%d), skipping watch for %s", avail, directory)
            return False
        if self._total_dir_watches >= MAX_WATCHED_DIRS:
            logger.warning("Global dir watch limit reached (%d), skipping %s",
                           MAX_WATCHED_DIRS, directory)
            return False
        try:
            self._observer.schedule(self._handler, directory, recursive=recursive)
            self._watched_dirs.add(directory)
            self._total_dir_watches += 1
            logger.info("Watching %s (recursive=%s) [%d/%d dirs]",
                        directory, recursive,
                        self._total_dir_watches, MAX_WATCHED_DIRS)
            return True
        except OSError as e:
            logger.warning("Failed to watch %s: %s", directory, e)
            return False

    def _is_already_watched(self, directory: str) -> bool:
        return any(directory.startswith(w + os.sep) or directory == w
                   for w in self._watched_dirs)

    def _watch_directory_of(self, file_path: str) -> None:
        directory = os.path.dirname(file_path)
        if self._is_already_watched(directory):
            return

        file_count = _count_direct_files(directory)
        self._total_file_watches += file_count
        self._add_watch(directory, recursive=False)

        # BFS downward: only expand into dirs containing .thy/.ML files
        current_level = [directory]
        while current_level:
            if (self._total_dir_watches >= MAX_WATCHED_DIRS
                    or self._total_file_watches >= MAX_WATCHED_FILES):
                break

            next_level: list[str] = []
            for d in current_level:
                for sub in _get_subdirs(d):
                    if not _has_relevant_files(sub):
                        continue
                    c = _count_direct_files(sub)
                    if self._total_file_watches + c > MAX_WATCHED_FILES:
                        continue
                    if self._add_watch(sub, recursive=False):
                        self._total_file_watches += c
                        next_level.append(sub)

            current_level = next_level
