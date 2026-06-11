"""Tracks PIDE processing status per file based on PIDE/decoration notifications."""

from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from collections.abc import Callable

from isabelle_mcp.utils.core import LSPLine

logger = logging.getLogger(__name__)

# How long after sending a document edit (didChange) the cached decoration state
# is treated as stale. The server batches input (vscode_input_delay = 0.1s) and
# throttles decoration output (vscode_output_delay = 0.5s), so the push that
# reflects an edit arrives within ~0.6s; after this grace period the cache is
# current. The server stays silent forever when the recomputed decorations equal
# the published ones, so freshness must recover by clock — waiting for a push
# would latch "in progress" permanently on a no-op re-evaluation.
DECORATION_GRACE: float = float(
    os.environ.get("ISABELLE_MCP_DECORATION_GRACE", "1.0"),
)

_TRACKED_TYPES = frozenset({
    "background_unprocessed1", "background_running1",
    "background_bad", "text_overview_error", "text_overview_warning",
})


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
        # Problem decorations (full-replace per type, same as _unprocessed):
        #   _bad           — background_bad      (failed/killed commands AND sorry)
        #   _overview_error — text_overview_error (errors on the overview ruler)
        #   _overview_warning — text_overview_warning (warnings on the ruler)
        self._bad: list[tuple[int, int, int, int]] = []
        self._overview_error: list[tuple[int, int, int, int]] = []
        self._overview_warning: list[tuple[int, int, int, int]] = []
        self._initialized: bool = False
        # Monotonic time of the last didChange we sent for this file; -inf when
        # no edit has been sent (a tracker created by the first push is fresh).
        self._last_doc_update_sent: float = float("-inf")
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
            # Separate per-type branches (do NOT fold into a loop that also
            # touches _unprocessed/_running). An emptied type arrives as
            # ``content:[]`` → key present with empty list → cleared. This
            # full-replace is how a fixed error/warning/sorry disappears.
            if "background_bad" in parsed:
                self._bad = parsed["background_bad"]
            if "text_overview_error" in parsed:
                self._overview_error = parsed["text_overview_error"]
            if "text_overview_warning" in parsed:
                self._overview_warning = parsed["text_overview_warning"]
            self._initialized = True
            self._condition.notify_all()

    @property
    def _fresh(self) -> bool:
        return self._initialized and self._grace_remaining() == 0.0

    def _grace_remaining(self) -> float:
        """Seconds left until the post-edit grace window elapses (0 if elapsed)."""
        return max(
            0.0, DECORATION_GRACE - (_time.monotonic() - self._last_doc_update_sent),
        )

    def note_doc_update_sent(self) -> None:
        """Record that a didChange for this file was just sent to the server.

        Until :data:`DECORATION_GRACE` seconds pass, the cached decoration state
        is treated as stale — it may still describe the pre-edit document, and
        trusting it would let an empty unprocessed list pass for "evaluation
        complete" before the server even assimilated the edit. Caret-only moves
        need no stamp: they can only shrink processing requirements, so a stale
        cache errs toward "not yet processed" (waits longer, never lies done).
        """
        self._last_doc_update_sent = _time.monotonic()

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
                        self._condition.wait(),
                        timeout=self._wait_timeout(check_interval),
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
                        timeout=min(remaining, self._wait_timeout(check_interval)),
                    )
                except asyncio.TimeoutError:
                    if _time.monotonic() >= deadline:
                        return False
                    health_check()
        return True

    def _wait_timeout(self, check_interval: float) -> float:
        """Wait-slice for the condition loops: wake when the grace window elapses.

        No push arrives when decorations did not change, so a wait gated only on
        the condition variable would sleep the full *check_interval* past the
        moment the cache became trustworthy again.
        """
        grace = self._grace_remaining()
        if grace > 0.0:
            return min(check_interval, grace + 0.05)
        return check_interval

    def line_reached(self, line: int) -> bool:
        """True if *line* (0-indexed) is NOT inside any unprocessed range.

        Ignores running ranges — a forked proof means the eval chain has
        already passed this line.  Returns False inside the post-edit grace
        window (see :meth:`note_doc_update_sent`).
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

    def get_bad_ranges(self) -> list[tuple[int, int, int, int]]:
        """Return a snapshot of background_bad ranges (failed/killed/sorry), 0-indexed."""
        return list(self._bad)

    def get_overview_error_ranges(self) -> list[tuple[int, int, int, int]]:
        """Return a snapshot of text_overview_error ranges (0-indexed)."""
        return list(self._overview_error)

    def get_overview_warning_ranges(self) -> list[tuple[int, int, int, int]]:
        """Return a snapshot of text_overview_warning ranges (0-indexed)."""
        return list(self._overview_warning)

    async def reset(self) -> None:
        """Clear all state (e.g. when the document is closed)."""
        async with self._condition:
            self._unprocessed.clear()
            self._running.clear()
            self._running_onset.clear()
            self._bad.clear()
            self._overview_error.clear()
            self._overview_warning.clear()
            self._initialized = False
            self._last_doc_update_sent = float("-inf")
            self._condition.notify_all()
