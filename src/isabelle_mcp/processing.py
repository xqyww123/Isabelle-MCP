"""Tracks PIDE processing status per file based on PIDE/decoration notifications."""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections.abc import Callable

from isabelle_mcp.utils.core import LSPLine

logger = logging.getLogger(__name__)

_TRACKED_TYPES = frozenset({"background_unprocessed1", "background_running1"})


def parse_decoration_ranges(entries: list[dict]) -> dict[str, list[tuple[int, int, int, int]]]:
    """Extract tracked decoration ranges from PIDE/decoration entries.

    Returns a dict mapping decoration type to list of (start_line, start_col,
    end_line, end_col) tuples, all 0-indexed.  Only types in _TRACKED_TYPES
    are included.
    """
    result: dict[str, list[tuple[int, int, int, int]]] = {}
    for entry in entries:
        typ = entry.get("type", "")
        if typ not in _TRACKED_TYPES:
            continue
        ranges: list[tuple[int, int, int, int]] = []
        for item in entry.get("content", []):
            r = item.get("range")
            if isinstance(r, list) and len(r) == 4:
                ranges.append((r[0], r[1], r[2], r[3]))
        result[typ] = ranges
    return result


def _ranges_overlap(
    range_start: int, range_end: int,
    query_start: int, query_end: int,
) -> bool:
    return range_start <= query_end and range_end >= query_start


class ProcessingTracker:
    """Tracks whether PIDE has finished processing specific lines of a file.

    Updated by the LSP client whenever a ``PIDE/decoration`` notification
    arrives.  Tools call :meth:`wait_until_processed` to block until a
    target line or range has been processed.

    All line numbers are 0-indexed (LSP convention).
    """

    def __init__(self) -> None:
        self._unprocessed: list[tuple[int, int, int, int]] = []
        self._running: list[tuple[int, int, int, int]] = []
        self._running_onset: dict[tuple[int, int, int, int], float] = {}
        self._initialized: bool = False
        self._update_count: int = 0
        self._min_required_updates: int = 0
        self._condition: asyncio.Condition = asyncio.Condition()

    async def update(self, parsed: dict[str, list[tuple[int, int, int, int]]]) -> None:
        """Merge decoration ranges from a (possibly incremental) push."""
        async with self._condition:
            if "background_unprocessed1" in parsed:
                self._unprocessed = parsed["background_unprocessed1"]
            if "background_running1" in parsed:
                new_running = parsed["background_running1"]
                now = _time.monotonic()
                updated_onset: dict[tuple[int, int, int, int], float] = {}
                for r in new_running:
                    updated_onset[r] = self._running_onset.get(r, now)
                self._running = new_running
                self._running_onset = updated_onset
            self._initialized = True
            self._update_count += 1
            self._condition.notify_all()

    @property
    def _fresh(self) -> bool:
        return self._initialized and self._update_count >= self._min_required_updates

    def require_fresh_update(self) -> None:
        """Invalidate cached state until the next decoration update arrives.

        Call this after moving the caret to a new destination: PIDE
        decorations are perspective-aware, so the old decoration data
        may not cover the new target line.
        """
        self._min_required_updates = self._update_count + 1

    def range_processed(self, start_line: LSPLine, end_line: LSPLine) -> bool:
        """True if no unprocessed/running range overlaps [start_line, end_line]."""
        if not self._fresh:
            return False
        for sl, _, el, _ in self._unprocessed:
            if _ranges_overlap(sl, el, start_line, end_line):
                return False
        for sl, _, el, _ in self._running:
            if _ranges_overlap(sl, el, start_line, end_line):
                return False
        return True

    @property
    def all_processed(self) -> bool:
        return self._fresh and not self._unprocessed and not self._running

    async def wait_until_processed(
        self,
        start_line: LSPLine,
        end_line: LSPLine,
        health_check: Callable[[], None],
        check_interval: float = 5.0,
    ) -> None:
        """Block until [start_line, end_line] is fully processed."""
        async with self._condition:
            while not self.range_processed(start_line, end_line):
                try:
                    await asyncio.wait_for(
                        self._condition.wait(), timeout=check_interval,
                    )
                except asyncio.TimeoutError:
                    health_check()

    async def wait_until_processed_bounded(
        self,
        start_line: LSPLine,
        end_line: LSPLine,
        timeout: float,
        health_check: Callable[[], None],
        check_interval: float = 5.0,
    ) -> bool:
        """Like wait_until_processed, but returns False on timeout."""

        deadline = _time.monotonic() + timeout
        async with self._condition:
            while not self.range_processed(start_line, end_line):
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(
                        self._condition.wait(),
                        timeout=min(remaining, check_interval),
                    )
                except asyncio.TimeoutError:
                    if _time.monotonic() >= deadline:
                        return False
                    health_check()
        return True

    def line_reached(self, line: int) -> bool:
        """True if *line* (0-indexed) is NOT inside any unprocessed range.

        Ignores running ranges — a forked proof means the eval chain has
        already passed this line.  Returns False when the tracker is
        awaiting a fresh decoration update (see :meth:`require_fresh_update`).
        """
        if not self._fresh:
            return False
        for sl, _, el, _ in self._unprocessed:
            if sl <= line <= el:
                return False
        return True

    def line_running(self, line: int) -> bool:
        """True if *line* (0-indexed) IS inside a running range."""
        for sl, _, el, _ in self._running:
            if sl <= line <= el:
                return True
        return False

    def get_running_ranges(self) -> list[tuple[int, int, int, int]]:
        """Return a snapshot of currently-running ranges (0-indexed)."""
        return list(self._running)

    def get_running_ranges_with_onset(self) -> list[tuple[int, int, int, int, float]]:
        """Return running ranges with their onset timestamps."""
        return [
            (*r, self._running_onset.get(r, 0.0))
            for r in self._running
        ]

    def get_unprocessed_ranges(self) -> list[tuple[int, int, int, int]]:
        """Return a snapshot of unprocessed ranges (0-indexed)."""
        return list(self._unprocessed)

    async def reset(self) -> None:
        """Clear all state (e.g. when the document is closed)."""
        async with self._condition:
            self._unprocessed.clear()
            self._running.clear()
            self._running_onset.clear()
            self._initialized = False
            self._update_count = 0
            self._min_required_updates = 0
            self._condition.notify_all()
