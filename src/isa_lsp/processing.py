"""Tracks PIDE processing status per file based on PIDE/decoration notifications."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from isa_lsp.utils.core import LSPLine

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
        self._initialized: bool = False
        self._condition: asyncio.Condition = asyncio.Condition()

    async def update(self, parsed: dict[str, list[tuple[int, int, int, int]]]) -> None:
        """Merge decoration ranges from a (possibly incremental) push."""
        async with self._condition:
            if "background_unprocessed1" in parsed:
                self._unprocessed = parsed["background_unprocessed1"]
            if "background_running1" in parsed:
                self._running = parsed["background_running1"]
            self._initialized = True
            self._condition.notify_all()

    def range_processed(self, start_line: LSPLine, end_line: LSPLine) -> bool:
        """True if no unprocessed/running range overlaps [start_line, end_line]."""
        if not self._initialized:
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
        return self._initialized and not self._unprocessed and not self._running

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

    async def reset(self) -> None:
        """Clear all state (e.g. when the document is closed)."""
        async with self._condition:
            self._unprocessed.clear()
            self._running.clear()
            self._initialized = False
            self._condition.notify_all()
