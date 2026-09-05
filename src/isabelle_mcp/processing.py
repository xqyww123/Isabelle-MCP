"""Tracks PIDE processing status per file based on PIDE/decoration notifications,
and the freshness rule that decides when that cache may be trusted."""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections.abc import Callable

from isabelle_mcp.utils.core import LSPLine

logger = logging.getLogger(__name__)


# ── Document versions and freshness ─────────────────────────────────────────
#
# Every picture the server sends (a PIDE/decoration push, a PIDE/theory_status
# reply) carries `document_version`: the id of the PIDE Document.Version it was
# rendered from. Every exchange that changes the document (PIDE/flush,
# PIDE/cancel_evaluation) replies with an assigned version that contains every
# edit the client had sent before the request. The client trusts a picture iff
# its stamp is at least as new as the newest version it has seen and no content
# it wrote is still uncovered by a flush reply — no clock, no grace window.
#
# Ids tick DOWNWARD: the JVM counter starts at 0 and decrements
# (Pure/Concurrent/counter.scala: "unique identifiers < 0 ... JVM ticks
# backwards"); Document_ID.none = 0 is Version.init's id, the oldest version
# there is. So NEWER MEANS NUMERICALLY SMALLER. The direction lives in the two
# functions below and nowhere else (I-7): no other module writes a relational
# operator, max, min or sorted on a document version.

def at_least_as_new(stamp: int, reference: int) -> bool:
    """True iff document version *stamp* is at least as new as *reference*."""
    return stamp <= reference


def newer_of(a: int, b: int) -> int:
    """The newer of two document versions."""
    return min(a, b)


class FreshnessState:
    """The client's freshness state, shared by reference with every tracker.

    ``newest_document_version`` is the client's high-water mark: the newest
    version seen in any picture stamp or reply version since the prover
    started (0 = Version.init until the first stamp). ``content_sends`` counts
    the didOpen/didChange messages written to the wire, ``content_sends_flushed``
    how many of them a flush reply has covered; the two counters are plain
    integers compared with ``!=``, never document versions. ``condition`` is the
    ONE client-level condition every freshness wait parks on; it is notified by
    a folded decoration push, a close, a flush reply and a content send.
    """

    def __init__(self) -> None:
        self.newest_document_version: int = 0
        self.content_sends: int = 0
        self.content_sends_flushed: int = 0
        self.condition: asyncio.Condition = asyncio.Condition()

    @property
    def unflushed_content(self) -> bool:
        return self.content_sends != self.content_sends_flushed

    def advance(self, document_version: int) -> None:
        """Fold a stamp or reply version into the newest version."""
        self.newest_document_version = newer_of(
            self.newest_document_version, document_version)

    async def notify(self) -> None:
        async with self.condition:
            self.condition.notify_all()

    def reset(self) -> None:
        """A new server process restarts everything."""
        self.newest_document_version = 0
        self.content_sends = 0
        self.content_sends_flushed = 0


_TRACKED_TYPES = frozenset({
    "background_unprocessed1", "background_running1", "background_canceled",
    "background_bad", "background_sorry", "text_overview_error",
    "text_overview_warning",
})


def is_full_decoration_push(parsed: dict[str, list[tuple[int, int, int, int]]]) -> bool:
    """Whether a parsed push names every tracked type — the mark of a FULL push.

    CROSS-LANGUAGE INVARIANT, enforced by :meth:`ProcessingTracker.update` (I-5)
    and mirrored in the Scala fork (``vscode_rendering.scala`` ``decorations`` —
    "list of canonical length and order"; ``vscode_model.scala`` ``publish``;
    ``vscode_resources.scala`` ``close_model``): the server publishes decorations
    in three shapes. A **full** push carries the canonical list, every type present,
    empty ones included; it is sent exactly when ``published_decorations`` is
    empty, which every open and every reopen guarantees (``close_model`` clears
    the baseline). A **differential** push carries only the entries that changed
    since the last publish. An **acknowledgement** push carries no entries at all:
    "re-rendered at this version, nothing changed".

    So "names all of ``_TRACKED_TYPES``" separates a picture of the whole
    document from a slice of one.
    """
    return _TRACKED_TYPES <= parsed.keys()

# The state of the command(s) covering one position, judged from the decoration
# cache alone. Fixed vocabulary — the agent-facing words of isabelle_command_status
# are rendered from these and must not acquire a second meaning anywhere.
PROCESSED = "processed"
RUNNING = "running"
NOT_EVALUATED = "not_evaluated"
CANCELLED = "cancelled"
UNKNOWN = "unknown"


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


def clip_line_range(
    start_line: int, end_line: int, n_lines: int,
) -> tuple[int, int] | None:
    """Clamp a 0-indexed ``[start_line, end_line]`` to a document of *n_lines*.

    Returns the clamped ``(start, end)``, or ``None`` when the range begins past
    EOF. Shared by the per-file snapshot and the running-command collector so a
    transiently stale decoration tracker (whose ranges may outlive a file shrink)
    never reports lines beyond the current content.
    """
    if start_line >= n_lines:
        return None
    return (start_line, min(end_line, n_lines - 1))


class ProcessingTracker:
    """Tracks whether PIDE has finished processing specific lines of a file.

    Updated by the LSP client whenever a ``PIDE/decoration`` notification
    arrives.  Tools call :meth:`wait_until_processed` to block until a
    target line or range has been processed.

    All line numbers are 0-indexed (LSP convention).
    """

    def __init__(self, freshness: FreshnessState | None = None) -> None:
        # The client's freshness state, by reference. A tracker built without it
        # (the null-object sites in debugger.py and tools/command_status.py) is
        # never initialized and never fresh.
        self._freshness = freshness
        self._document_version: int | None = None
        self._unprocessed: list[tuple[int, int, int, int]] = []
        self._running: list[tuple[int, int, int, int]] = []
        self._running_onset: dict[tuple[int, int, int, int], float] = {}
        # Problem decorations (full-replace per type, same as _unprocessed):
        #   _bad           — background_bad      (the prover's "bad" commands: failed
        #                    proofs, sorry, and benign members like `back`; read only
        #                    as a sign of content, never rendered as errors)
        #   _sorry         — background_sorry    (the fork's own type: the ranges of
        #                    `sorry` / `\<proof>`, classified server-side by the
        #                    "Skipped proof" message; rendered as the sorry row)
        #   _overview_error — text_overview_error (errors on the overview ruler; THE
        #                    error channel of every report)
        #   _overview_warning — text_overview_warning (warnings on the ruler; never
        #                    reported, kept as a sign of content)
        self._bad: list[tuple[int, int, int, int]] = []
        self._sorry: list[tuple[int, int, int, int]] = []
        self._overview_error: list[tuple[int, int, int, int]] = []
        self._overview_warning: list[tuple[int, int, int, int]] = []
        # background_canceled — commands whose execution was interrupted
        # (Markup.CANCELED, i.e. Isabelle's own "canceled" spelling). Read only by
        # position_state: such a command also carries `failed`, so the per-file
        # snapshot already counts and locates it via _bad/_overview_error.
        self._canceled: list[tuple[int, int, int, int]] = []
        self._initialized: bool = False
        self._condition: asyncio.Condition = asyncio.Condition()

    @property
    def initialized(self) -> bool:
        """True once a FULL push has been folded in (I-5): the cache is a picture
        of the whole document, not one differential slice of it."""
        return self._initialized

    @property
    def document_version(self) -> int | None:
        """The picture stamp of the last folded push; None before the first."""
        return self._document_version

    async def update(
        self, parsed: dict[str, list[tuple[int, int, int, int]]], document_version: int,
    ) -> None:
        """Merge decoration ranges from a push stamped *document_version*.

        Every push for an open document is folded, whatever its shape. Only a
        FULL push initializes the tracker (I-5, enforced HERE so no call site can
        make a picture out of a slice): content folded into an uninitialized
        tracker is never served (`fresh` requires initialization; `range_state`
        answers `not_evaluated`) and is overwritten whole by the full push, which
        names every tracked type.
        """
        async with self._condition:
            if self._freshness is None:
                raise AssertionError("a tracker without freshness state cannot fold pushes")
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
            if "background_canceled" in parsed:
                self._canceled = parsed["background_canceled"]
            if "background_bad" in parsed:
                self._bad = parsed["background_bad"]
            if "background_sorry" in parsed:
                self._sorry = parsed["background_sorry"]
            if "text_overview_error" in parsed:
                self._overview_error = parsed["text_overview_error"]
            if "text_overview_warning" in parsed:
                self._overview_warning = parsed["text_overview_warning"]
            self._document_version = document_version
            self._initialized = self._initialized or is_full_decoration_push(parsed)
            self._condition.notify_all()

    def stamp_at_least_as_new_as(self, version: int) -> bool:
        """This is a FULL picture (I-5) stamped at least as new as *version*.

        The one place the picture-versus-a-reference-version half of the trust
        rule (I-4) lives: ``fresh`` asks it about the newest version the client
        has seen, and the report's stamp arbitration (``_build_file_snapshot``)
        and the freshness wait (``wait_until_fresh``) ask it about the version
        each is judging against. Answers False for a tracker that never folded a
        full push. Does NOT read the content counters — those are the whole
        client's state, weighed by ``fresh`` alone."""
        return (
            self._initialized
            and self._document_version is not None
            and at_least_as_new(self._document_version, version)
        )

    @property
    def fresh(self) -> bool:
        """The trust rule (I-4): a picture of the whole document, rendered at a
        version at least as new as the newest one the client has seen, read while
        no content the client wrote is still uncovered by a flush reply."""
        return (
            self._freshness is not None
            and self.stamp_at_least_as_new_as(self._freshness.newest_document_version)
            and not self._freshness.unflushed_content
        )

    def range_processed(self, start_line: LSPLine, end_line: LSPLine) -> bool:
        """True if no unprocessed/running range overlaps [start_line, end_line]."""
        if not self.fresh:
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
        return self.fresh and not self._unprocessed and not self._running

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
                        self._condition.wait(), timeout=min(remaining, check_interval),
                    )
                except asyncio.TimeoutError:
                    if _time.monotonic() >= deadline:
                        return False
                    health_check()
        return True

    async def wait_until_line_reached_bounded(
        self,
        line: LSPLine,
        timeout: float,
        health_check: Callable[[], None],
        check_interval: float = 5.0,
    ) -> bool:
        """Like :meth:`wait_until_processed_bounded`, but the predicate is
        :meth:`line_reached` — wake as soon as the execution frontier passes *line*
        (that line leaves the unprocessed set), regardless of forks still running
        EARLIER in the prefix.

        The evaluation wait loop uses this to react the instant the frontier reaches
        the destination; the caller then decides complete vs in_progress from a
        separate prefix-quiet (:meth:`range_processed`) check, so trailing forks do
        not make this wait block. Returns False on timeout.
        """
        deadline = _time.monotonic() + timeout
        async with self._condition:
            while not self.line_reached(line):
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(
                        self._condition.wait(), timeout=min(remaining, check_interval),
                    )
                except asyncio.TimeoutError:
                    if _time.monotonic() >= deadline:
                        return False
                    health_check()
        return True

    def line_reached(self, line: int) -> bool:
        """True if *line* (0-indexed) is NOT inside any unprocessed range.

        Ignores running ranges — a forked proof means the eval chain has
        already passed this line.  Returns False while the picture is not
        fresh (see :attr:`fresh`).
        """
        if not self.fresh:
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

    def position_state(self, line: int) -> str:
        """State of the command(s) covering *line* (0-indexed): one of the five
        module constants.

        This is the definite answer :meth:`line_reached` cannot give: that method
        collapses "not processed yet" and "the cache is not trustworthy" into a
        single ``False``, so a caller acting on it re-evaluates lines that were
        finished long ago.

        The order of the tests is Isabelle's own precedence
        (``rendering.scala:515-518``): unprocessed, then running, then canceled.
        The three background colours are computed from one command status, so at a
        given offset they are mutually exclusive; the order only decides what a
        line covering SEVERAL commands is called, and there the least-finished
        command is the honest answer.

        Freshness is checked before the running and canceled scans, and that
        placement is load-bearing. An unfresh picture may still describe an OLDER
        document version; the familiar "a stale cache can only over-report work as
        unfinished" argument makes that safe only for a caller whose response is
        *do more work*. ``RUNNING`` and ``CANCELLED`` are SERVED — a caller acts
        on them by answering the query — and relative to the newest version the
        honest answer is ``UNKNOWN``: the picture is not fresh.

        ``NOT_EVALUATED`` keeps the conservative meaning, so the unprocessed scan
        may stay in front: the newest version is client-wide, so "not fresh" does
        not say this file was edited; what survives is that ``NOT_EVALUATED`` is
        the least-finished verdict and the caller answers it by evaluating.

        A tracker that has never received a decoration reports ``NOT_EVALUATED``:
        nothing has been processed, which is exactly what the caller must act on.
        """
        return self.range_state(line, line)[0]

    def range_state(self, start_line: int, end_line: int) -> tuple[str, float]:
        """State of the 0-indexed inclusive span ``[start_line, end_line]``, and
        for ``RUNNING`` the seconds it has been running.

        Same precedence and freshness rules as :meth:`position_state`, which is
        the one-line case of this; see its docstring for why the order is what it
        is. A span overlapping several decorations is called by the least-finished
        one, which is the honest answer for a whole command as much as for a line.

        The elapsed time is 0.0 for every state but ``RUNNING``.
        """
        for sl, _, el, _ in self._unprocessed:
            if _ranges_overlap(sl, el, start_line, end_line):
                return (NOT_EVALUATED, 0.0)
        if not self._initialized:
            return (NOT_EVALUATED, 0.0)
        if not self.fresh:
            return (UNKNOWN, 0.0)
        now = _time.monotonic()
        for r in self._running:
            sl, _, el, _ = r
            if _ranges_overlap(sl, el, start_line, end_line):
                onset = self._running_onset.get(r)
                return (RUNNING, 0.0 if onset is None else max(0.0, now - onset))
        for sl, _, el, _ in self._canceled:
            if _ranges_overlap(sl, el, start_line, end_line):
                return (CANCELLED, 0.0)
        return (PROCESSED, 0.0)

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

    def get_canceled_ranges(self) -> list[tuple[int, int, int, int]]:
        """Return a snapshot of background_canceled ranges (interrupted), 0-indexed."""
        return list(self._canceled)

    def get_bad_ranges(self) -> list[tuple[int, int, int, int]]:
        """Return a snapshot of background_bad ranges (the prover's "bad"
        commands: failed proofs, sorry, benign members like `back`), 0-indexed.
        A sign that the decoration carries content and that the file belongs
        in a report — never the source of an error row."""
        return list(self._bad)

    def get_sorry_ranges(self) -> list[tuple[int, int, int, int]]:
        """Return a snapshot of background_sorry ranges (`sorry` sites), 0-indexed."""
        return list(self._sorry)

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
            self._sorry.clear()
            self._canceled.clear()
            self._overview_error.clear()
            self._overview_warning.clear()
            self._document_version = None
            self._initialized = False
            self._condition.notify_all()
