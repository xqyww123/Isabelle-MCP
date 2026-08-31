"""Async evaluation lifecycle for Isabelle theories (v0.3.0).

Separates *evaluation* (telling Isabelle what to process) from *querying*
(reading hover/goal/diagnostic results).  Three MCP tools manage
evaluation; query tools call :func:`check_evaluation_guard` to ensure
the target region has been processed.

v0.3.0 leverages PIDE/theory_status for dependency-aware completion and
PIDE/cancel_evaluation for cancellation (stanch, retire, retract -- see
ISABELLE_MCP_CANCELLATION_REDESIGN_PLAN.md section 3).
"""

from __future__ import annotations

import anyio
import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from isabelle_mcp.lsp_client import (
    PRECOMPILED_MODIFIED_ERROR,
    IsabelleLSPClient,
    _canon,
    _stat_sigs,
)
from isabelle_mcp.models import (
    EvaluationView,
    FileSnapshot,
    RunningCommand,
    TheoryStatus,
)
from isabelle_mcp.processing import (
    CANCELLED,
    NOT_EVALUATED,
    PROCESSED,
    RUNNING,
    UNKNOWN,
    _grace_remaining,
    clip_line_range,
    note_edit_sent,
)
from isabelle_mcp.utils import (
    IsabelleCatastrophe,
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPLine,
    plural,
    resolve_caret,
)

logger = logging.getLogger(__name__)

EVAL_POLL_INTERVAL: float = float(
    os.environ.get("ISABELLE_MCP_EVAL_POLL_INTERVAL", "10"),
)

# Wait cap for files precompiled into the running heap: an unmodified one
# completes instantly (PIDE replays its markup from the build database), a
# modified one never completes (PIDE refuses to reprocess loaded theories) —
# there is nothing to wait for either way.
HEAP_POLL_INTERVAL: float = 3.0

# During a long evaluation, re-stat open docs at most this often (seconds) so an
# edit landing mid-evaluation is still pushed, without stat'ing on every wakeup.
_LONG_EVAL_RESTAT_INTERVAL: float = 3.0

# Per-document close budget during cancel cleanup: each close is shielded from a
# re-delivered cancel so every auto-opened doc is actually closed (not orphaned), but
# bounded so a stalled stdin.drain() cannot hang an already-cancelled request.
_CLOSE_TIMEOUT: float = 5.0

# One sentence for every way a run is stopped by someone else — the agent's own
# cancel and a session teardown alike — as seen from the evaluate_to side.
CANCELLED_MESSAGE = "Evaluation cancelled."

# Approved (R-D4 ②): an evaluation still in flight when the prover is gone --
# torn down by isabelle_terminate, by a relaunch, or by the catastrophe
# handler.  ``client.process is None`` is the only marker.
SESSION_GONE_MESSAGE = (
    "Evaluation stopped: the Isabelle session is no longer running. "
    "Call isabelle_launch to start a new one."
)

# The three outcomes of PIDE/cancel_evaluation, keyed by the server's ``outcome``
# field (ISABELLE_MCP_CANCELLATION_REDESIGN_PLAN.md section 3.1.4).  The server
# stanches the prover, retracts every perspective and retires the interrupted
# commands until none is left. The server's third outcome, ``aborted``, never
# reaches this module: force_interrupt turns it into IsabelleCatastrophe.
CANCEL_OUTCOME_RETIRED = "retired"
CANCEL_OUTCOME_NOTHING_RUNNING = "nothing_running"

# The whole cancel request -- the LSP request (135 s: the server's 120 s budget
# plus dispatch queueing) and the wrap-up -- runs under ONE deadline; expiry is
# the catastrophe. This is the bound on how long the evaluation-state lock is
# held by a cancellation (R5); the teardown that follows a catastrophe runs
# outside it and is bounded by its own segments.
CANCEL_TOTAL_BUDGET: float = 150.0

# Approved copy (R-D4): the two success sentences.
CANCEL_MESSAGES = {
    CANCEL_OUTCOME_RETIRED:
        "Evaluation cancelled. The interrupted commands are back to unevaluated.",
    CANCEL_OUTCOME_NOTHING_RUNNING:
        "Evaluation cancelled. Nothing was running.",
}
# The lines after the main sentence (R-D4 ⑤). Every command entry is rendered
# the same way by _cancel_item: "file:line (keyword)". Commands the server
# excluded (gone / reassigned) are not rendered: they are back to unevaluated
# all the same, just not by this request.
CANCEL_RESET_LINE = "Reset to unevaluated: {items}."
CANCEL_WAIVED_LINE = "Also reset by the same re-parse: the commands following {items}."

# The evaluation target whitelist (R-D3 (v)): only a .thy file can be evaluated.
# A .ML/.sml file is compiled by the load command (ML_file and kin) that loads
# it; with exactly one such command known, isabelle_evaluate_to is redirected to
# it and says so in its first line.  Wording approved verbatim; {kind} is the
# actual suffix and {keyword} the load command's actual keyword.
LOAD_TARGET_SUFFIXES = (".ML", ".sml")
LOAD_TARGET_POINTER = (
    "{file} is a {kind} file, which cannot be an evaluation target. "
    "Evaluate to {loader} (the {keyword} command that loads this file) instead."
)
LOAD_TARGET_POINTER_MANY = (
    "{file} is a {kind} file, which cannot be an evaluation target. "
    "Evaluate to {loaders} (the {keyword} commands that load this file) instead."
)
LOAD_TARGET_GENERIC = "{file} is a {kind} file, which cannot be an evaluation target."
NON_THEORY_TARGET = (
    "{file} is not a theory (.thy) file and cannot be an evaluation target."
)
REDIRECTED_LINE = (
    "Redirected: {file} is a {kind} file loaded by the {loader_command} command "
    "at {loader}; evaluated through that command instead."
)

# Position state judged with the document itself missing — the one answer the
# decoration cache cannot give, so it lives here rather than in `processing`.
FILE_NOT_OPEN = "file_not_open"

# A running command is worth naming individually only once it has been running
# this long; below it, it is ordinary progress and not something to act on. One
# constant, shared by the evaluation result and the footer, so the two can never
# disagree about which commands are worth mentioning.
RUNNING_REPORT_THRESHOLD: float = 10.0

# The evaluation target, said the same way everywhere: these are both the leading
# sentence of an evaluation result and the footer's main sentence.
TOWARDS_SENTENCE = "Evaluating towards {target}:{line}."
ARRIVED_SENTENCE = "Evaluation has arrived at {target}:{line}."
COMPLETED_SENTENCE = "Evaluation has completed up to {target}:{line}."

CHECK_PROGRESS_CALL = "Call isabelle_evaluation_status to check progress."
FOOTER_DETAILS_CALL = "Call isabelle_evaluation_status for details."

# isabelle_evaluation_status when no run is outstanding and nothing is running.
# The first line says whether the sections below hold errors; {failed} is
# ``1 command`` / ``2 commands``.
IDLE_CLEAN_SENTENCE = (
    "No evaluation in progress. Nothing is running and no errors remain."
)
IDLE_FAILED_SENTENCE = (
    "No evaluation in progress. Nothing is running, but {failed} failed."
)

# ---- Agent-facing guard text -------------------------------------------------
# Every string an agent can see when a query is refused or served with a caveat.
# Kept together so the vocabulary stays consistent and reviewable.

RUNNING_NOTE = (
    "The command at {file}:{line} is still being executed; "
    "its output may be incomplete."
)

INTERRUPTED_NOTE = (
    "The evaluation of the command at {file}:{line} was interrupted; "
    "its output may be incomplete."
)

# "a file", not "this file": the distrust comes from a GLOBAL edit clock, so the
# change that armed it may have been to a different file.
UNKNOWN_POSITION_MESSAGE = (
    "Cannot tell whether {file}:{line} has been evaluated: a file changed a "
    "moment ago, so the processing state is not yet trustworthy. Retry in a few "
    "seconds."
)

# Refusals while an evaluation of ANOTHER file is running (wording approved
# verbatim). A request for the file under evaluation is never refused for
# being busy: it joins the running evaluation and can only move its target
# forward (see EvaluationState.join_or_start).
# {activity} is _activity_clause's product: empty, or ", where …".
EVALUATE_TO_REFUSAL = (
    "An evaluation is running towards {target}:{target_line}{activity}. "
    "You cannot evaluate another file until it finishes, or you cancel it with "
    "isabelle_cancel_evaluation."
)
# The query tools' guard, for a file that is not open as much as for a line
# that has not been evaluated.
NOT_EVALUATED_REFUSAL = (
    "{file}:{line} has not been evaluated, and it cannot be while an evaluation "
    "is running towards {target}:{target_line}. Wait for it to finish, or cancel "
    "it with isabelle_cancel_evaluation."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_line(value: int, total_lines: int) -> MCPLine:
    if value < 0:
        return MCPLine(max(1, total_lines + 1 + value))
    return MCPLine(value)


def _parse_theory_status(raw: dict) -> TheoryStatus:
    return TheoryStatus(
        node_name=raw.get("node_name", ""),
        theory_name=raw.get("theory_name", ""),
        external=raw.get("external", False),
        imports=[imp["theory_name"] for imp in raw.get("imports", [])],
        ok=raw.get("ok", True),
        total=raw.get("total", 0),
        unprocessed=raw.get("unprocessed", 0),
        running=raw.get("running", 0),
        warned=raw.get("warned", 0),
        failed=raw.get("failed", 0),
        finished=raw.get("finished", 0),
        canceled=raw.get("canceled", False),
        consolidated=raw.get("consolidated", False),
        percentage=raw.get("percentage", 0),
    )


def _find_theory_name(file_path: str, theories: list[TheoryStatus]) -> str | None:
    return next((t.theory_name for t in theories if t.node_name == file_path), None)


def _get_recursive_dependencies(
    target: str, theories: list[TheoryStatus],
) -> set[str]:
    theory_map = {t.theory_name: t for t in theories}
    visited: set[str] = set()
    queue: deque[str] = deque()
    t = theory_map.get(target)
    if t is None:
        return set()
    for imp in t.imports:
        if imp not in visited:
            visited.add(imp)
            queue.append(imp)
    while queue:
        name = queue.popleft()
        dep = theory_map.get(name)
        if dep is None:
            continue
        for imp in dep.imports:
            if imp not in visited:
                visited.add(imp)
                queue.append(imp)
    return visited


def evaluation_theory_set(
    target: str, auto_opened: set[str], theories: list[TheoryStatus],
) -> set[str]:
    """The current evaluation's theory set (Phase D, approved 2026-08-19
    after two adversarial verification rounds): the files a hit can belong
    to for THIS run. The union of (a) the target, (b) the auto-opened
    dependencies, (c) the import closure's node_names (deps absent from the
    snapshot drop out — heap-precompiled code cannot hit), and (d) external
    entries whose theory_name is EMPTY — exactly the ML_file-loaded blobs
    ("all external" was refuted: the flag is never cleared, so it converges
    on everything not currently open). All realpathed."""
    out = {os.path.realpath(target)}
    out |= set(auto_opened)   # already canonical (_canon at registration)
    theory_map = {t.theory_name: t for t in theories}
    target_name = _find_theory_name(target, theories)
    if target_name is not None:
        for dep in _get_recursive_dependencies(target_name, theories):
            t = theory_map.get(dep)
            if t is not None and t.node_name:
                out.add(os.path.realpath(t.node_name))
    for t in theories:
        if t.external and not t.theory_name and t.node_name:
            out.add(os.path.realpath(t.node_name))
    return out


def _dependency_done(t: TheoryStatus) -> bool:
    if t.canceled:
        return True
    if t.consolidated:
        return True
    if t.running == 0 and t.unprocessed == 0:
        return True
    if t.running == 0 and not t.ok:
        return True
    return False


def _frontier_reached(
    file_path: str,
    dest_line: MCPLine,
    client: IsabelleLSPClient,
    theories: list[TheoryStatus],
) -> bool:
    """The execution frontier has passed *dest_line* and every import is done.

    Checks only that dest_line itself left the unprocessed set; it deliberately
    ignores forks still running EARLIER in the prefix — that is :func:`_prefix_quiet`'s
    job. Reaching the frontier is the trigger to decide complete vs in_progress.
    """
    tracker = client.get_processing_tracker(file_path)
    if not tracker or not tracker.line_reached(dest_line.to_lsp()):
        return False
    target_name = _find_theory_name(file_path, theories)
    if target_name is None:
        return False
    deps = _get_recursive_dependencies(target_name, theories)
    theory_map = {t.theory_name: t for t in theories}
    return all(_dependency_done(theory_map[d]) for d in deps if d in theory_map)


def _prefix_quiet(
    file_path: str,
    dest_line: MCPLine,
    client: IsabelleLSPClient,
) -> bool:
    """No unprocessed/running command overlaps the evaluated prefix ``[0, dest]``.

    True only once trailing forked proofs in the prefix have joined. At that instant
    any failure decoration is already present: PIDE delivers "leave running/
    unprocessed" and "become bad/error" in the SAME decoration push (verified
    empirically), so a quiet prefix can never hide a just-failed command.
    """
    tracker = client.get_processing_tracker(file_path)
    if tracker is None:
        return False
    return tracker.range_processed(LSPLine(0), dest_line.to_lsp())


def _is_evaluation_complete(
    file_path: str,
    dest_line: MCPLine,
    client: IsabelleLSPClient,
    theories: list[TheoryStatus],
) -> bool:
    """Strict completion: frontier reached AND the evaluated prefix is fully quiet."""
    return _frontier_reached(
        file_path, dest_line, client, theories,
    ) and _prefix_quiet(file_path, dest_line, client)


def _target_sentence(
    template: str, target: str, line: int, root: str | None,
) -> str:
    return template.format(target=relativize(target, root), line=int(line))


def _still_running_sentence(running_commands: list[RunningCommand]) -> str:
    """``1 command is still running.`` / ``2 commands are still running.``"""
    n = len(running_commands)
    return f"{plural(n, 'command')} {'is' if n == 1 else 'are'} still running."


def _activity_sentences(
    running_commands: list[RunningCommand], n_failed: int,
) -> list[str]:
    """What the prover is doing, in whole sentences.

    Callers pass ``n_failed = 0`` when no evaluation is outstanding. An error
    decoration persists until the file is edited and re-evaluated, so reporting
    it while nothing is under evaluation would repeat the same count on every
    call and train the agent to stop reading; while an evaluation IS outstanding
    the same count is progress information about that evaluation.
    """
    sentences = []
    n_slow = sum(
        1 for c in running_commands
        if c.elapsed_seconds >= RUNNING_REPORT_THRESHOLD
    )
    if n_slow:
        verb = "has" if n_slow == 1 else "have"
        sentences.append(
            f"{plural(n_slow, 'command')} {verb} been running for over "
            f"{int(RUNNING_REPORT_THRESHOLD)}s.",
        )
    if n_failed:
        sentences.append(f"{plural(n_failed, 'command')} failed.")
    return sentences


def _activity_clause(running_commands: list[RunningCommand]) -> str:
    """The ``{activity}`` clause of EVALUATE_TO_REFUSAL: empty when no command
    has run for RUNNING_REPORT_THRESHOLD yet; otherwise how many have, and the
    longest. Whole seconds, rounded down."""
    slow = sorted(
        (int(c.elapsed_seconds) for c in running_commands
         if c.elapsed_seconds >= RUNNING_REPORT_THRESHOLD),
        reverse=True,
    )
    if not slow:
        return ""
    if len(slow) == 1:
        return f", where one command has been running for {slow[0]}s"
    return (
        f", where {len(slow)} commands have been running for over "
        f"{int(RUNNING_REPORT_THRESHOLD)}s, the longest for {slow[0]}s"
    )


# ---------------------------------------------------------------------------
# EvaluationState
# ---------------------------------------------------------------------------

# The recorded reasons an evaluation run can end. "abandoned" is the
# heap-divergence ending: the target file differs from its precompiled copy,
# so PIDE will never reprocess it (section 6 of the fix plan).
Outcome = Literal["complete", "cancelled", "abandoned"]


@dataclass(eq=False)
class Evaluation:
    """One evaluation run. The object reference is its identity.

    ``outcome`` records WHY the run ended, which the shared ``active`` boolean
    cannot: a cancel, a session teardown, a heap abandonment and an
    ``evaluation_status`` call that observed the run *succeed* all merely
    clear that flag.
    """

    outcome: Outcome | Literal[""] = ""
    # evaluate_to requests that joined this run and have not given up on it.
    # A request that returns in_progress has NOT given up: it will poll again,
    # so it keeps its place here and a later abort must not end the run under
    # it. Born at 0 with the run, so a request of an earlier run can never
    # reach a live run's count.
    riders: int = 0


@dataclass
class EvaluationState:
    active: bool = False
    file_path: str = ""
    destination_line: MCPLine = MCPLine(1)
    auto_opened_files: set[str] = field(default_factory=set)
    current: Evaluation | None = None

    def start(self, file_path: str, destination_line: MCPLine) -> Evaluation:
        self.active = True
        self.file_path = file_path
        self.destination_line = destination_line
        self.auto_opened_files = set()
        self.current = Evaluation()
        return self.current

    def advance(self, file_path: str, destination_line: MCPLine) -> None:
        """Move the running evaluation's target forward — never back — for a
        second request on the same file. The one writer of that monotonicity.

        The caller must hold ``_evaluation_state_lock``: the footer's
        "target unchanged, so stamp complete" decision relies on the target
        not moving under it.
        """
        assert self.active and self.file_path == file_path and self.current is not None
        self.destination_line = max(self.destination_line, destination_line)

    def busy_with_another_file(self, file_path: str) -> bool:
        """An evaluation of a file other than *file_path* is running.

        The refusal predicate both entry points ask before touching the run;
        its falsity is what join_or_start's ``assert not self.active`` leans on.
        """
        return self.active and self.file_path != file_path

    def join_or_start(
        self, file_path: str, destination_line: MCPLine,
    ) -> tuple[Evaluation, bool]:
        """Register one evaluate_to on the run for *file_path*, starting a run
        if none is going. Returns ``(run, is_target)``: *is_target* says this
        request is at or ahead of the old target and is therefore allowed to
        move the caret; a request behind the target leaves the caret alone,
        since pulling it back would strand the run before its target.

        Joining does NOT start() a new run — that would orphan the in-flight
        loop's _finish_if_owner and leak the auto-opened dependencies. The
        caller holds ``_evaluation_state_lock``.
        """
        if self.active and self.file_path == file_path:
            is_target = destination_line >= self.destination_line
            self.advance(file_path, destination_line)
        else:
            # Any other file was refused before this call.
            assert not self.active
            self.start(file_path, destination_line)
            is_target = True
        assert self.current is not None
        self.current.riders += 1
        return self.current, is_target

    @staticmethod
    def leave(evaluation: Evaluation) -> bool:
        """This request gives up on *evaluation*; True when it must also END it.

        Only the last request that had not given up on the run may end it —
        anyone still inside the wait, and anyone that returned in_progress
        meaning to poll again, is left the run and its auto-opened
        dependencies. On a run that already ended this can still say True;
        _finish_if_owner turns that into the no-op it is.
        """
        evaluation.riders -= 1
        return evaluation.riders == 0

    def owns(self, evaluation: Evaluation) -> bool:
        """Whether *evaluation* is still the run this state describes.

        ``current`` is deliberately never reset: a run that ended with no
        successor must still recognise itself as the owner and run its cleanup.
        Only a later ``start()`` takes ownership away.
        """
        return self.current is evaluation

    def _finish(self, outcome: Outcome) -> None:
        # The one terminal transition: flag down, stamp write-once (a later
        # cancel of a lingering fork must not rewrite a finished run's story).
        # Folding the flag clear in here keeps the flag's single clearer and
        # makes recording an outcome verbatim the shortest call. The
        # load-bearing rule is that _finish_if_owner must never dispatch on
        # the outcome: the old `if outcome == "complete" ... else cancel()`
        # there coerced the third outcome ("abandoned") into a cancel —
        # restoring it turns three tests red (D-B11).
        self.active = False
        cur = self.current
        if cur is not None and not cur.outcome:
            cur.outcome = outcome

    def complete(self) -> None:
        self._finish("complete")

    def cancel(self) -> None:
        self._finish("cancelled")


evaluation_state = EvaluationState()


def last_evaluation_was_cancelled() -> bool:
    """Whether the most recent evaluation ended by being cancelled.

    ``current`` is never reset, only replaced by a later ``start()``, so this
    keeps describing the last run for as long as no new one has begun. It is what
    lets a query reply name the cause of a missing proof state instead of
    guessing at it.
    """
    current = evaluation_state.current
    return current is not None and current.outcome == "cancelled"
# Serializes the short evaluation-state transitions (evaluate_to start / guard)
# and the document content/version mutations and caret-target resolution that
# must stay atomic with them — NOT the whole evaluation. The event-driven
# file-sync push and the tool-call stat backstop also take it so a concurrent
# sync cannot interleave with a start/stop. The one long holder is
# cancel_evaluation (R-D8): it keeps the lock for the whole cancel request, so
# every other tool call — evaluation_status and terminate included — queues
# behind a cancellation; every await under the lock there has an explicit bound.
_evaluation_state_lock = asyncio.Lock()

# Sentinel for "dependency never stat'd before" (its recorded value may be None).
_UNSEEN: object = object()


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------

async def _build_status_snapshot(
    client: IsabelleLSPClient,
    evaluation_state: EvaluationState,
) -> tuple[list[TheoryStatus], list[RunningCommand]]:
    """Pull theory_status, auto-open failed theories, collect running commands.

    Auto-opening a not-ok theory (load-bearing side effect) gives it a decoration
    tracker so the snapshot can report its problems with line numbers. No diagnostics
    are read — the snapshot is built from decoration + theory_status (see
    :func:`_build_file_snapshot`).
    """
    raw_theories = await client.request_theory_status()
    theories = [_parse_theory_status(t) for t in raw_theories]

    for t in theories:
        if not t.ok and t.node_name:
            # Canonicalize so the open_documents membership check (keyed by _canon)
            # and auto_opened_files agree with open_document/close_document, which
            # both apply _canon — otherwise a symlinked node_name desyncs them.
            node = _canon(t.node_name)
            if (node not in evaluation_state.auto_opened_files
                    and client.open_documents.get(node) is None):
                # Track BEFORE the await: open_document registers the node in
                # client.open_documents and sends didOpen before its trailing
                # wait_for_first_diagnostics await, where anyio may re-deliver a
                # cancel. Adding only after would orphan a server-opened doc that
                # _cleanup_auto_opened (it closes only this set) never closes.
                evaluation_state.auto_opened_files.add(node)
                try:
                    await client.open_document(node)
                except OSError:
                    # open failed before didOpen (e.g. unreadable path) → untrack.
                    evaluation_state.auto_opened_files.discard(node)

    running_commands = client.get_all_running_commands()
    return theories, running_commands


# ---------------------------------------------------------------------------
# Per-file snapshot (decoration primary, theory_status fallback)
# ---------------------------------------------------------------------------

def _line_spans(
    ranges: list[tuple[int, int, int, int]], n_lines: int | None = None,
) -> list[tuple[int, int]]:
    """0-indexed decoration tuples → sorted 1-indexed (start_line, end_line) spans.

    When *n_lines* is given, ranges that begin past EOF are dropped and end lines
    are clamped to the current content (see :func:`clip_line_range`) — a stale
    tracker outliving a file shrink must not surface phantom spans past EOF.
    """
    spans: list[tuple[int, int]] = []
    for r in ranges:
        if n_lines is not None:
            clipped = clip_line_range(r[0], r[2], n_lines)
            if clipped is None:
                continue
            s0, e0 = clipped
        else:
            s0, e0 = r[0], r[2]
        spans.append((int(LSPLine(s0).to_mcp()), int(LSPLine(e0).to_mcp())))
    return sorted(spans)


def _pending_spans(
    ranges: list[tuple[int, int, int, int]],
    dest_lsp: int,
    n_lines: int | None,
) -> list[tuple[int, int]]:
    """Unprocessed decoration ranges clipped to the evaluated prefix ``[0, dest_lsp]``.

    0-indexed LSP tuples → sorted, merged 1-indexed ``(start, end)`` line spans.
    Ranges beginning past *dest_lsp* are dropped; ends are capped at *dest_lsp* (and
    at EOF via :func:`clip_line_range` when *n_lines* is given) so the unevaluated
    tail past the destination is never reported as pending work.
    """
    spans: list[tuple[int, int]] = []
    for r in ranges:
        if r[0] > dest_lsp:
            continue
        s0, e0 = r[0], min(r[2], dest_lsp)
        if n_lines is not None:
            clipped = clip_line_range(s0, e0, n_lines)
            if clipped is None:
                continue
            s0, e0 = clipped
        spans.append((int(LSPLine(s0).to_mcp()), int(LSPLine(e0).to_mcp())))
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge line spans that share a line into one (dedupes the two error channels)."""
    if not spans:
        return []
    spans = sorted(spans)
    merged = [spans[0]]
    for s, e in spans[1:]:
        ls, le = merged[-1]
        if s <= le:  # overlap (inclusive) → same problem from both channels
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def _build_file_snapshot(
    client: IsabelleLSPClient,
    file_path: str,
    ts_map: dict[str, TheoryStatus],
    dest_line: MCPLine | None = None,
) -> FileSnapshot:
    """One file's problem snapshot. Decoration if current, else theory_status counts.

    Built fully synchronously (no await between getter reads) so the union/merge sees
    one consistent tracker state. *dest_line* (set only for the evaluation target)
    surfaces the still-unprocessed prefix ``[0, dest]`` as ``pending`` so an
    in_progress snapshot never renders a bare "clean" while work remains.
    """
    ts = ts_map.get(file_path)
    tracker = client.get_processing_tracker(file_path)
    doc = client.open_documents.get(file_path)
    # "+1": a decoration anchored at the end of the document sits on the line
    # after the final newline; this count keeps clip_line_range from dropping
    # it. Do not unify with evaluate_to's count of real lines.
    n_lines = (doc.content.count("\n") + 1) if doc else None

    if tracker is not None:
        bad = tracker.get_bad_ranges()
        oerr = tracker.get_overview_error_ranges()
        owarn = tracker.get_overview_warning_ranges()
        running = tracker.get_running_ranges()
        unproc = tracker.get_unprocessed_ranges()
        deco_has_content = bool(bad or oerr or owarn or running or unproc)
        # theory_status reports a problem/activity the decoration should reflect.
        ts_active_or_problem = ts is not None and (
            ts.unprocessed > 0 or ts.running > 0 or ts.failed or ts.warned
        )
        # Trust decoration when it carries content, or when theory_status agrees
        # there is nothing to show. Only fall back when theory_status reports a
        # problem/activity that the (stale) decoration does NOT reflect — e.g. a
        # dependency re-invalidated by an edit, whose decoration lags.
        if deco_has_content or not ts_active_or_problem:
            errors = _merge_spans(
                _line_spans(oerr, n_lines) + _line_spans(bad, n_lines)
            )
            warnings = _line_spans(owarn, n_lines)
            running_spans = _line_spans(running, n_lines)
            pending_spans = (
                _pending_spans(unproc, int(dest_line.to_lsp()), n_lines)
                if dest_line is not None else []
            )
            if errors or warnings:
                state = "problems"
            elif running_spans or pending_spans:
                state = "in_progress"
            else:
                state = "clean"
            return FileSnapshot(
                file_path=file_path, lined=True, state=state,
                errors=errors, warnings=warnings, running=running_spans,
                pending=pending_spans,
                error_count=len(errors), warning_count=len(warnings),
                running_count=len(running_spans), pending_count=len(pending_spans),
            )

    # theory_status fallback (counts only, no line numbers)
    if ts is None:
        return FileSnapshot(file_path=file_path, lined=False, state="in_progress")
    if ts.unprocessed > 0 or ts.running > 0 or not ts.consolidated:
        state = "in_progress"
    elif ts.failed or ts.warned:
        state = "problems"
    else:
        state = "clean"
    return FileSnapshot(
        file_path=file_path, lined=False, state=state,
        error_count=ts.failed, warning_count=ts.warned, running_count=ts.running,
    )


def _open_document_snapshots(client: IsabelleLSPClient) -> list[FileSnapshot]:
    """One snapshot per open document -- the single place that enumerates them
    for a document-wide sweep.

    Fully synchronous (no await anywhere in the sweep), so every snapshot is
    read from the same tracker state: the sweep is atomic with respect to the
    event loop.
    """
    return [
        _build_file_snapshot(client, path, {})
        for path in list(client.open_documents)
    ]


def _relevant_files(
    client: IsabelleLSPClient, target: str, auto_opened: set[str],
) -> list[str]:
    """Target ∪ auto-opened deps ∪ open docs with any problem/running marker."""
    # A session that never evaluated has no target ("" -- only start() writes
    # file_path): seeding it would snapshot the empty path, and relativize("")
    # renders the project root as a file.
    files: list[str] = [target] if target else []
    for f in auto_opened:
        if f not in files:
            files.append(f)
    for path in list(client.open_documents):
        if path in files:
            continue
        tr = client.get_processing_tracker(path)
        if tr is not None and (
            tr.get_bad_ranges() or tr.get_overview_error_ranges()
            or tr.get_overview_warning_ranges() or tr.get_running_ranges()
        ):
            files.append(path)
    return files


def _snapshot_files(
    client: IsabelleLSPClient,
    target: str,
    theories: list[TheoryStatus],
    auto_opened: set[str],
    dest_line: MCPLine | None = None,
) -> list[FileSnapshot]:
    ts_map = {t.node_name: t for t in theories}
    # Only the evaluation target gets dest_line — pending is the prefix [0, dest] of
    # the file actually being evaluated; deps/other files have no destination.
    return [
        _build_file_snapshot(client, f, ts_map, dest_line if f == target else None)
        for f in _relevant_files(client, target, auto_opened)
    ]


async def _cleanup_auto_opened(
    client: IsabelleLSPClient, state: EvaluationState,
) -> None:
    # Snapshot the paths synchronously, BEFORE the first await: anyio re-delivers the
    # cancel at every checkpoint and a concurrent evaluate_to may flip ``active`` and
    # re-register files via start() — neither must race the read. Callers flip active
    # (via _finish) immediately before this call with no await in between, so the
    # snapshot is atomic w.r.t. the active flip.
    #
    # Each close is SHIELDED (anyio is level-triggered: a cancel is re-delivered at
    # every checkpoint, so without shielding the first close would abort the loop and
    # orphan the not-yet-closed docs — a later start() wipes the set, leaking them on
    # the server). The shield is bounded by _CLOSE_TIMEOUT so a stalled stdin.drain()
    # can never hang an already-cancelled request. Discard each path after its attempt.
    #
    # Bind the set OBJECT once: start() rebinds the attribute to a fresh set, so a
    # cleanup overlapping a newly started run would otherwise discard from the new
    # run's set.
    opened = state.auto_opened_files
    for path in list(opened):
        try:
            with anyio.move_on_after(_CLOSE_TIMEOUT, shield=True):
                await client.close_document(path)
        except Exception:
            logger.warning("Failed to close auto-opened file %s", path, exc_info=True)
        finally:
            opened.discard(path)


async def _finish_if_owner(
    client: IsabelleLSPClient, evaluation: Evaluation, outcome: Outcome,
    *, judged_dest: MCPLine | None,
) -> bool:
    """End *evaluation* — flag, outcome stamp and cleanup — if it still owns the
    state and, for a completion, the target is still the one it was judged at.

    Only the run that started this state may end it. Without the ownership test a
    finishing run closes the auto-opened documents of a *later* run that started
    while it was waiting (evaluate_to holds no lock across the wait).

    *judged_dest* is keyword-only with no default on purpose: every new caller
    must decide, because omitting it would silently disarm the guard. Only a
    completion passes a target — the target its verdict was computed against;
    a target that advanced meanwhile means the verdict belongs to the old
    target while the run drives on, so the stamp is refused. Every other
    ending (the cancel family, a heap abandonment) passes ``None``: it ends
    the run wherever its target stands.
    (``cancel_evaluation`` itself deliberately does not go through this path at
    all — it resets unconditionally in a ``finally``, otherwise a failed cancel
    would wedge every later evaluation.)

    The tests and the flag flip are synchronous with no await between them, so
    _cleanup_auto_opened's atomicity guarantee and the "reset first, then close"
    cancellation discipline are preserved.
    """
    if not evaluation_state.owns(evaluation):
        return False
    if judged_dest is not None and evaluation_state.destination_line != judged_dest:
        return False
    evaluation_state._finish(outcome)
    await _cleanup_auto_opened(client, evaluation_state)
    return True


# ---------------------------------------------------------------------------
# Wait loop
# ---------------------------------------------------------------------------

class _HitWatch:
    """The evaluation wait's third exit condition (design section 6.1): a
    hit in the current evaluation's theory set ends the wait; a hit
    elsewhere stays a debugger notice; an unattributable hit fails open
    (ends the wait, as any exit does). The theory set is computed lazily —
    only when a hit must be classified — from the iteration's snapshot;
    a would-be-"elsewhere" hit re-fetches theory_status once and recomputes
    before the verdict is final (the auto-open awaits can leave the
    iteration's snapshot seconds stale).

    The classified set starts EMPTY: evaluate_to's entry refusal proved the
    hit table empty, so every hit this loop ever sees is this run's to
    classify by construction — including one that lands during the awaits
    between the refusal and the loop (open_document, the fence's
    theory_status round trip). Seeding from the live table here would
    silently exempt exactly those hits (2026-08-19 review)."""

    def __init__(
        self, client: IsabelleLSPClient, target: str, state: EvaluationState,
    ) -> None:
        self._client = client
        self._target = target
        self._state = state
        self._enabled = client.debug
        if self._enabled:
            from isabelle_mcp import debugger
            self._registry = debugger.registry
            self._classified: set[str] = set()

    async def hit_led_exit(self, theories: list[TheoryStatus]) -> bool:
        if not self._enabled:
            return False
        client = self._client
        self._registry.sync_hits(client)
        theory_set: set[str] | None = None
        for hit_id, hit in list(self._registry.hits.items()):
            if hit_id in self._classified:
                continue
            frame0 = hit.stack[0] if hit.stack else {}
            file = frame0.get("file")
            if not file:
                return True   # unattributable: fail open
            try:
                real = os.path.realpath(file)
            except ValueError:
                return True   # malformed path: fail open, never teardown
            if theory_set is None:
                theory_set = evaluation_theory_set(
                    self._target, self._state.auto_opened_files, theories)
            if real in theory_set:
                return True
            # Would-be "elsewhere": re-fetch once and recompute before the
            # verdict is final.
            try:
                raw = await client.request_theory_status()
            except IsabelleToolError:
                pass   # keep the stale verdict; the notice still delivers
            else:
                theories = [_parse_theory_status(t) for t in raw]
                theory_set = evaluation_theory_set(
                    self._target, self._state.auto_opened_files, theories)
                if real in theory_set:
                    return True
            self._classified.add(hit_id)   # elsewhere: the notice stands
        return False


async def _evaluation_wait_loop(
    client: IsabelleLSPClient,
    file_path: str,
    state: EvaluationState,
    evaluation: Evaluation,
    timeout: float,
) -> tuple[str, list[TheoryStatus], list[RunningCommand]]:
    """Wait until the frontier reaches the run's target, or *timeout*.

    The target is ``state.destination_line``, read afresh each round right
    before the frontier decision: a second request on the same file advances
    it while this loop is waiting, and a copy taken at entry would judge the
    old target.
    """
    deadline = time.monotonic() + timeout
    last_restat = time.monotonic()
    theories: list[TheoryStatus] = []
    hit_watch = _HitWatch(client, file_path, state)
    while True:
        if evaluation.outcome or not state.active:
            # Someone else ended this run. Report the recorded reason rather than
            # guessing, and carry out what we last saw instead of empty lists,
            # which would render the file sections as "no theories at all".
            # get_all_running_commands is a synchronous read of local state.
            return (
                evaluation.outcome or "cancelled",
                theories,
                client.get_all_running_commands(),
            )
        now = time.monotonic()
        if now - last_restat >= _LONG_EVAL_RESTAT_INTERVAL:
            last_restat = now
            # Push any edit that landed mid-evaluation; PIDE re-checks incrementally.
            await resync_changed_open_documents_locked(client)
        theories, running_commands = await _build_status_snapshot(client, state)
        # The third exit condition, BEFORE the frontier decision: a parked
        # fork keeps the prefix busy, and returning a plain in_progress
        # there would bury the hit in a notice (section 6.1 wants the
        # result to lead with the hit report).
        if await hit_watch.hit_led_exit(theories):
            return "hit", theories, client.get_all_running_commands()
        # Decide the instant the frontier reaches dest: prefix quiet → complete;
        # otherwise return in_progress NOW (no grace). Trailing forks are reported
        # (running/pending lines), not waited on — the caller polls to convergence.
        # No await between this read and the returns below, so the decision is
        # about one target.
        dest_line = state.destination_line
        if _frontier_reached(file_path, dest_line, client, theories):
            if _prefix_quiet(file_path, dest_line, client):
                return "complete", theories, running_commands
            return "in_progress", theories, running_commands
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "in_progress", theories, running_commands
        tracker = client.get_processing_tracker(file_path)
        if tracker:
            # Wake when the frontier reaches dest (not when the whole prefix is
            # quiet), so the decision above is prompt and never a pseudo-grace.
            await tracker.wait_until_line_reached_bounded(
                dest_line.to_lsp(),
                timeout=min(remaining, 5.0),
                health_check=lambda: client._check_server_health(client.STALL_TIMEOUT),
            )
        else:
            await asyncio.sleep(min(remaining, 2.0))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_loader(loader: dict, root: str | None) -> str:
    where = relativize(loader.get("file", ""), root)
    line = loader.get("line")
    return f"{where}:{line}" if line is not None else where


def render_loaders(loaders: list[dict], root: str | None) -> str:
    """``A`` / ``A or B`` / ``A, B or C`` — the approved pointer shape."""
    names = [render_loader(x, root) for x in loaders]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " or " + names[-1]


def _load_target_refusal(
    rel: str, kind: str, loaders: list[dict], root: str | None,
) -> str:
    """The R-D3 (iv) refusal for a .ML/.sml target: pointer, pointer with several
    loaders, or the generic sentence when no loader is known."""
    if not loaders or (len(loaders) == 1 and loaders[0].get("line") is None):
        # zero loaders, or one whose theory is not in the document model yet
        return LOAD_TARGET_GENERIC.format(file=rel, kind=kind)
    keywords = sorted({x.get("command") or "ML_file" for x in loaders})
    keyword = " or ".join(keywords)
    if len(loaders) == 1:
        return LOAD_TARGET_POINTER.format(
            file=rel, kind=kind, keyword=keyword,
            loader=render_loader(loaders[0], root))
    return LOAD_TARGET_POINTER_MANY.format(
        file=rel, kind=kind, keyword=keyword, loaders=render_loaders(loaders, root))


async def evaluation_target(
    client: IsabelleLSPClient, file_path: str, *, redirect: bool,
) -> dict | None:
    """Apply the evaluation target whitelist (R-D3 (v)).

    A .thy file passes: returns None. A .ML/.sml file with exactly one known
    load command that has a line is returned as that loader when *redirect* is
    allowed (evaluate_to evaluates to the loader instead); otherwise it is
    refused with the pointer/generic sentence. Any other suffix is refused.
    Raises IsabelleToolError for every refusal. The loader round trip happens on
    the non-.thy path only.
    """
    if file_path.endswith(".thy"):
        return None
    rel = relativize(file_path, client.project_root)
    kind = next((k for k in LOAD_TARGET_SUFFIXES if file_path.endswith(k)), None)
    if kind is None:
        raise IsabelleToolError(NON_THEORY_TARGET.format(file=rel))
    loaders = await client.request_loaders(file_path)
    if redirect and len(loaders) == 1 and loaders[0].get("line") is not None:
        return loaders[0]
    raise IsabelleToolError(
        _load_target_refusal(rel, kind, loaders, client.project_root))


async def evaluate_to(
    client: IsabelleLSPClient,
    file_path: str,
    line: int,
    after_text: str | None = None,
) -> EvaluationView:
    from isabelle_mcp import debugger
    redirected_line: str | None = None
    loader = await evaluation_target(client, file_path, redirect=True)
    if loader is not None:
        # R-D3 (v) ②: evaluate to the load command instead; the caller's line
        # and after_text are dropped, the load command's own keyword pins the
        # target on that command.
        kind = next(k for k in LOAD_TARGET_SUFFIXES if file_path.endswith(k))
        redirected_line = REDIRECTED_LINE.format(
            file=relativize(file_path, client.project_root), kind=kind,
            loader_command=loader.get("command") or "ML_file",
            loader=render_loader(loader, client.project_root))
        file_path = os.path.realpath(loader["file"])
        line = int(loader["line"])
        after_text = loader.get("command")
    async with _evaluation_state_lock:
        # Pinned ordering (Phase D): the hits-live refusal runs BEFORE the
        # active-evaluation refusal — the latter tells the agent to cancel,
        # which would destroy the very hits it is being refused over. The
        # refusal lives here, not in the tool wrapper, so the query tools'
        # auto-start path inherits it and the caret never moves on a hit.
        hits_refusal = debugger.hits_live_refusal(client)
        if hits_refusal is not None:
            raise IsabelleToolError(hits_refusal)
        if evaluation_state.busy_with_another_file(file_path):
            raise IsabelleToolError(EVALUATE_TO_REFUSAL.format(
                target=relativize(evaluation_state.file_path, client.project_root),
                target_line=int(evaluation_state.destination_line),
                activity=_activity_clause(client.get_all_running_commands()),
            ))

        await client.open_document(file_path)
        heap_warning = client.heap_warning(file_path)
        doc = client.open_documents.get(file_path)
        # Lines that actually exist (a trailing newline ends the last line, it
        # does not start another), so -1 resolves to the real last line. This is
        # deliberately NOT the "+1" count used for clipping decorations in
        # _build_file_snapshot.
        total_lines = len(doc.content.removesuffix("\n").split("\n")) if doc else 1
        anchor_line = _resolve_line(line, total_lines)
        if anchor_line < 1:
            raise IsabelleToolError(f"line must be >= 1, got {anchor_line}")
        lines = doc.content.split("\n") if doc else []
        # resolve_caret anchors the caret INSIDE the command at the line (or just
        # past after_text). With a multi-line after_text the caret may land on a
        # later line, which then becomes the real evaluation destination.
        caret_line, caret_char = resolve_caret(
            lines, int(anchor_line.to_lsp()), after_text, line,
        )
        dest_line = LSPLine(caret_line).to_mcp()
        lsp_char = LSPCharacter(caret_char)

        # The forgotten-re-enable fence (section 5), before the run
        # starts: the warning describes what THIS run will miss. It also
        # queues itself as a debugger notice.
        fence_line = await debugger.forgotten_arming_fence(client, file_path)

        # A request for the file under evaluation (any other file was refused
        # above) joins the running evaluation; otherwise a run starts. Whoever
        # observes completion first finishes the shared run; a second finish is
        # a no-op.
        evaluation, is_target = evaluation_state.join_or_start(file_path, dest_line)

        # Sent under the lock, as its last act: cancel_evaluation writes
        # PIDE/cancel_evaluation only after taking THIS lock, so the caret
        # notification is on the wire ahead of any cancel request by
        # construction -- not by there happening to be no checkpoint between
        # the release and the write. Bounded by SEND_TIMEOUT.
        # No freshness invalidation here: every edit-send path calls
        # note_edit_sent (didOpen/didChange/dep change), and a caret-only move
        # cannot make stale decorations claim "processed" for work that isn't
        # (see note_edit_sent's docstring).
        if is_target:
            try:
                await client.set_caret(file_path, dest_line.to_lsp(), lsp_char)
            except BaseException:
                # Same rule as the wait handler below: this request gives up,
                # and ends the run only if it was the last request that had not
                # given up on it. A lone starter must end it — the anti-wedge
                # invariant needs ``active`` cleared on every failure path.
                if evaluation_state.leave(evaluation):
                    await _finish_if_owner(client, evaluation, "cancelled", judged_dest=None)
                raise

    try:
        status, theories, running_commands = await _evaluation_wait_loop(
            client, file_path, evaluation_state, evaluation,
            HEAP_POLL_INTERVAL if heap_warning else EVAL_POLL_INTERVAL,
        )

        # Own the state and carry no outcome stamp ⇒ still active: _finish is
        # the only clearer of the flag and it always stamps.
        if (heap_warning and status not in ("complete", "hit")
                and not evaluation.outcome and evaluation_state.owns(evaluation)):
            # The miss may be only the post-edit grace gate (a concurrent edit
            # re-armed it inside the short heap budget) — an unmodified precompiled
            # file replays instantly once the gate opens. Re-check past the gate
            # before declaring the file divergent and telling the agent not to retry.
            grace = _grace_remaining()
            if grace > 0:
                status, theories, running_commands = await _evaluation_wait_loop(
                    client, file_path, evaluation_state, evaluation,
                    grace + 0.2,
                )
    except BaseException:
        # CancelledError is a BaseException (the old ``except Exception`` missed it)
        # and anyio re-delivers it at EVERY checkpoint, so it can fire on either
        # wait-loop await. _finish_if_owner resets synchronously FIRST — awaiting a
        # coroutine is not itself a checkpoint, so its ownership test and flag flip
        # always run — then closes the detached snapshot; _cleanup_auto_opened
        # shields each close so it completes despite the re-delivered cancel and
        # cannot hang (bounded by _CLOSE_TIMEOUT). Mirrors isabelle_launch's
        # cancellation cleanup.
        # leave() decides who may end the shared run: only the last request
        # that had not given up on it.
        if evaluation_state.leave(evaluation):
            await _finish_if_owner(client, evaluation, "cancelled", judged_dest=None)
        raise

    if evaluation_state.owns(evaluation):
        # The target may have been advanced while this call waited; what was
        # observed is the run's real target, so report that one. From the
        # loop's own read of the target through this re-read to the
        # _finish_if_owner below there is no await, so a completion is stamped
        # for the target it was judged at.
        dest_line = evaluation_state.destination_line
    dest = int(dest_line)
    # The stamp is authoritative. ``status`` is what the loop saw; the stamp is
    # what actually happened, including a terminal transition that landed inside
    # an iteration's awaits (the loop samples ``active`` only at the top).
    if evaluation.outcome:
        status = evaluation.outcome
    auto_opened = set(evaluation_state.auto_opened_files)
    # Build the snapshot BEFORE cleanup closes the auto-opened deps (which would
    # drop their decoration trackers).
    files = _snapshot_files(client, file_path, theories, auto_opened, dest_line)
    if client.process is None:
        # The prover is gone from under this run: torn down by isabelle_terminate,
        # a relaunch, or the catastrophe handler. That fact, not
        # the recorded outcome, is what the agent needs; checked before the hit
        # and heap branches, which would report on a prover that no longer exists.
        await _finish_if_owner(client, evaluation, "cancelled", judged_dest=None)
        status = "cancelled"
        message = SESSION_GONE_MESSAGE
    elif evaluation.outcome == "cancelled":
        # Stopped by isabelle_cancel_evaluation or by a session teardown. Say only
        # that; naming any other cause (a heap divergence, a timeout) would
        # fabricate one. The cleanup repeats because a dependency may have been
        # auto-opened after the canceller's own cleanup ran.
        await _finish_if_owner(client, evaluation, "cancelled", judged_dest=None)
        message = CANCELLED_MESSAGE
    elif status == "hit":
        # The hit-led exit (section 6.1) — a DISTINCT internal outcome,
        # checked BEFORE the heap-abandonment branch (pinned ordering: that
        # branch would abandon a run that is merely paused). The run stays
        # active — evaluation is paused, not finished — and the
        # agent-visible status stays the in_progress family (approved: the
        # bare status word never reaches the output anyway).
        message = await debugger.hit_report(client)
        status = "in_progress"
    elif heap_warning and status != "complete":
        # The file differs from its precompiled copy, so PIDE will never
        # reprocess it — abandon the evaluation instead of leaving it pending.
        # No-stamp-yet means THIS request makes the abandonment decision (the
        # read and the stamp share one synchronous stretch, so exactly one
        # rider is first): it refuses with the way out (D-C7). A rider that
        # merely read a peer's stamp must not raise — its own loop may have
        # watched the target arrive (section 6A landing note) — so it keeps
        # the reporting shape, with the same sentence: one event, one wording.
        first = not evaluation.outcome
        await _finish_if_owner(client, evaluation, "abandoned", judged_dest=None)
        message = PRECOMPILED_MODIFIED_ERROR.format(
            file=file_path, logic=client.logic)
        if first:
            raise IsabelleToolError(message)
        status = "abandoned"
    else:
        if status == "complete":
            # One completion vocabulary: internal complete ⇒ the COMPLETED
            # sentence, whatever failed or still runs elsewhere. Failures are
            # below, per line, in the file sections; running commands keep
            # their running: rows. A second wording-level completion judgement
            # is exactly problem 8 (D-B2). The sentence is unconditional while
            # the stamp is best-effort: judged_dest is the dest_line re-read
            # under owns() above with no await since, so the guard's one job
            # is to turn a run replaced meanwhile into a no-op — this reply
            # keeps its own COMPLETED and its own target line (10A must-fix 1).
            await _finish_if_owner(
                client, evaluation, "complete", judged_dest=dest_line,
            )
            message = _target_sentence(
                COMPLETED_SENTENCE, file_path, dest, client.project_root,
            )
        elif _frontier_reached(file_path, dest_line, client, theories):
            # The frontier passed the destination but the prefix is not quiet yet
            # (a trailing fork). isabelle_evaluation_status and the footer both
            # call that "arrived"; saying "evaluating towards" here would have two
            # tools contradict each other about the same instant.
            message = _target_sentence(
                ARRIVED_SENTENCE, file_path, dest, client.project_root,
            )
        else:
            message = _target_sentence(
                TOWARDS_SENTENCE, file_path, dest, client.project_root,
            )
    if fence_line:
        message = message + "\n\n" + fence_line
    if redirected_line:
        message = redirected_line + "\n" + message
    return EvaluationView(
        status=status,
        target_file=file_path,
        destination_line=dest,
        message=message,
        files=files,
        running_commands=running_commands,
        heap_warning=heap_warning,
    )


def _no_evaluation_view() -> EvaluationView:
    return EvaluationView(
        status="no_evaluation",
        message="No evaluation in progress.",
    )


def _no_pending_work(client: IsabelleLSPClient) -> bool:
    """Whether there is genuinely nothing left to watch or cancel.

    ``evaluate_to`` clears ``evaluation_state.active`` as soon as the execution
    frontier reaches the target line — but the command there may have forked
    background work (``value``, an asynchronous proof) that keeps running. Such a
    fork is invisible to ``active`` and shows up only in the running-command
    list, so both ``evaluation_status`` and ``cancel_evaluation`` consult that
    list before reporting that nothing is in progress.
    """
    return not evaluation_state.active and not client.get_all_running_commands()


def _idle_view(client: IsabelleLSPClient) -> EvaluationView:
    """The status when no run is outstanding and nothing is running: every
    open document that still shows errors or warnings, with line numbers.

    theory_status is deliberately NOT requested: ``_build_status_snapshot``
    auto-opens every not-ok theory into ``auto_opened_files``, which only the
    end of a run clears -- with no run, those documents would leak for the
    session. With an empty ts_map the theory_status fallback can never
    contradict the decoration, so every file that has a tracker takes the lined
    decoration branch; one that has never received a decoration contributes no
    counts and drops out of the filter. The first line's count is
    taken from the very snapshots rendered below it, so the two cannot disagree.
    """
    files = [
        fs for fs in _open_document_snapshots(client)
        if fs.error_count or fs.warning_count
    ]
    n_failed = sum(fs.error_count for fs in files)
    message = (
        IDLE_FAILED_SENTENCE.format(failed=plural(n_failed, "command"))
        if n_failed else IDLE_CLEAN_SENTENCE
    )
    return EvaluationView(status="no_evaluation", message=message, files=files)


async def evaluation_status(
    client: IsabelleLSPClient,
) -> EvaluationView:
    # Everything this tool answers is read from the decoration cache, which
    # describes the pre-edit document for DECORATION_GRACE after an edit (the
    # tool entry's own resync may have just sent one). Debounce: wait until the
    # window has passed with no further edit -- under continuous editing the
    # tool deliberately waits for the edits to stop rather than answer from a
    # cache known to be stale. Polling during a run sends no edit, so it pays
    # nothing here.
    while (grace := _grace_remaining()) > 0:
        await asyncio.sleep(grace)
    if _no_pending_work(client):
        return _idle_view(client)

    # Capture the run handle BEFORE the await: the round trip below is the one
    # window in which another run can take over, and the stamp must go to the
    # run this call judged, never to a successor (D-B12).
    evaluation = evaluation_state.current
    theories, running_commands = await _build_status_snapshot(
        client, evaluation_state,
    )
    dest_line = evaluation_state.destination_line
    dest = int(dest_line)
    target = evaluation_state.file_path
    auto_opened = set(evaluation_state.auto_opened_files)

    complete = _is_evaluation_complete(target, dest_line, client, theories)
    files = _snapshot_files(client, target, theories, auto_opened, dest_line)
    # Only the active evaluation owns the complete→cleanup transition; once
    # ``active`` is False the cleanup already ran and we are merely surfacing a
    # lingering fork, which must stay visible (not collapse back to "complete").
    # _finish_if_owner is the ONE termination implementation (no hand-rolled
    # complete()+cleanup here), and its verdict gates the sentence: a run
    # replaced during the round trip is not stamped, and the in_progress
    # branches below describe the current run truthfully — the next poll
    # completes it.
    if (evaluation_state.active and complete and evaluation is not None
            and await _finish_if_owner(client, evaluation, "complete",
                                       judged_dest=dest_line)):
        return EvaluationView(
            status="complete",
            target_file=target,
            destination_line=dest,
            message=_target_sentence(
                COMPLETED_SENTENCE, target, dest, client.project_root,
            ),
            files=files,
            running_commands=running_commands,
        )

    if not evaluation_state.active:
        # No evaluation is outstanding; what is left is a fork still settling, or
        # work the agent did not start (a re-evaluation triggered by a save).
        # There is no target to name, so name only the activity — and without the
        # footer's 10s threshold, which exists to keep an ambient line quiet. This
        # tool's whole job is to report status, so it says something either way.
        # No call to action either: this IS the tool one would be pointed at.
        message = _still_running_sentence(running_commands)
    elif _frontier_reached(target, dest_line, client, theories):
        message = _target_sentence(
            ARRIVED_SENTENCE, target, dest, client.project_root,
        )
    else:
        message = _target_sentence(
            TOWARDS_SENTENCE, target, dest, client.project_root,
        )
    return EvaluationView(
        status="in_progress",
        target_file=target if evaluation_state.active else None,
        destination_line=dest,
        message=message,
        files=files,
        running_commands=running_commands,
    )


async def sync_file_locked(client: IsabelleLSPClient, path: str) -> None:
    """Push one editor-opened file's on-disk content to Isabelle (event-driven).

    The sink the FileWatcher schedules on every relevant change. Holds
    ``_evaluation_state_lock`` so the push cannot interleave with an evaluate_to /
    cancel start/stop transition. A no-op if *path* is not an open editor document
    (e.g. a dependency file — those are the server File_Watcher's job). Pushing while
    an evaluation is active is intentional: PIDE re-checks incrementally.
    """
    async with _evaluation_state_lock:
        await client.sync_dirty_files({path})


async def resync_changed_open_documents_locked(client: IsabelleLSPClient) -> None:
    """Layer 2 under the lock: re-stat all open docs and didChange the changed ones."""
    async with _evaluation_state_lock:
        await client.resync_changed_open_documents()


async def _dependency_freshness_wait(client: IsabelleLSPClient) -> float:
    """Layer 3 detection: how long to wait for the server to notice a fresh dep edit.

    Dependency files (external imports + ``.ML`` blobs, identified by ``external`` in
    ``theory_status`` and not themselves editor-opened) are synced by Isabelle's own
    File_Watcher, which has a ``vscode_load_delay`` debounce. If such a dep changed
    since our last check **and** its mtime is within that debounce window, return the
    delay so the caller waits before querying; otherwise return ``0``. Stat'ing runs
    off the event loop. The dep set is bounded to the document model's non-heap nodes.
    """
    raw = await client.request_theory_status()
    dep_nodes = [
        t.get("node_name", "")
        for t in raw
        if t.get("external") and t.get("node_name")
        and t.get("node_name") not in client.open_documents
    ]
    if not dep_nodes:
        client._dep_stat_sigs.clear()
        return 0.0

    sigs = await asyncio.to_thread(_stat_sigs, dep_nodes)
    delay = client.vscode_load_delay
    now = time.time()
    need_wait = False
    for node, sig in sigs.items():
        prev = client._dep_stat_sigs.get(node, _UNSEEN)
        if prev is not _UNSEEN and sig != prev:
            # A dep changed — or was deleted (sig None) — on disk: the server's
            # File_Watcher will didChange it internally; an edit like any other,
            # so start the decoration grace.
            note_edit_sent()
            # Phase D bookkeeping: the blob's serials may be dead — mark it
            # for the next reconciliation pass.
            from isabelle_mcp import debugger
            debugger.registry.mark_dirty(node)
            # sig = (ino, size, mtime_ns, ctime_ns); recent edit ⇒ within the debounce.
            if sig is not None and (now - sig[2] / 1e9) < delay:
                need_wait = True
        client._dep_stat_sigs[node] = sig
    for gone in set(client._dep_stat_sigs) - set(sigs):
        del client._dep_stat_sigs[gone]
    return delay if need_wait else 0.0


async def resync_and_check_freshness(client: IsabelleLSPClient) -> None:
    """Tool-call entry backstop: Layer 2 (open docs) + Layer 3 (dependency) freshness.

    Runs at the start of every tool call (see ``_ensure_lsp_started``). Only Layer 2
    holds ``_evaluation_state_lock`` — it mutates document content/version. Layer 3
    runs **lock-free**: it only issues a read-only ``theory_status`` request and
    maintains its own ``_dep_stat_sigs``, touching no lock-protected state, so it must
    not block (or be blocked by) the event-driven push path.
    """
    await resync_changed_open_documents_locked(client)   # Layer 2 (locked)
    wait = await _dependency_freshness_wait(client)        # Layer 3 (lock-free)
    if wait > 0:
        logger.info(
            "Dependency changed <%.2fs ago; waiting %.2fs for the server to notice it",
            wait, wait,
        )
        await asyncio.sleep(wait)


async def cancel_evaluation(
    client: IsabelleLSPClient,
) -> EvaluationView:
    """One PIDE/cancel_evaluation request, under the evaluation-state lock for
    its whole duration (R-D8), and the agent-facing view of its outcome.

    Two outcomes come back: retired, nothing_running. Everything else -- the
    server's aborted outcome, a transport failure, the total budget running
    out, anything throwing in the wrap-up -- is the catastrophe: it leaves here
    as IsabelleCatastrophe and the tool boundary terminates the session (plan
    section 3.1.5). A genuine cancellation of the tool call (CancelledError)
    passes through untouched: state reset, attribution withdrawn, no teardown.
    """
    async with _evaluation_state_lock:
        if _no_pending_work(client):
            return _no_evaluation_view()

        from isabelle_mcp import debugger
        dest = int(evaluation_state.destination_line)
        # Attribute the coming retirements BEFORE the interrupt, so they
        # happen silently (section 6.4). finish_cancel_sweep rolls the
        # attribution back when nothing was running; every other exit
        # withdraws it below.
        swept_hits = debugger.mark_hits_swept(client)
        message = ""
        # move_on_after, not fail_after: a deadline that passes inside a shielded
        # segment (a close below) with no unshielded checkpoint after it raises
        # nothing, so the scope's cancel_called flag is the one reliable witness.
        # Inside the scope, expiry looks like a cancellation (CancelledError at
        # the current await, finally blocks run, shielded awaits complete).
        try:
            with anyio.move_on_after(CANCEL_TOTAL_BUDGET) as budget:
                try:
                    payload = await client.force_interrupt()
                finally:
                    # The state reset is UNCONDITIONAL (the anti-wedge
                    # invariant): a failed or cancelled request must still
                    # leave ``active`` False, else every later evaluate_to is
                    # refused. The auto-opened documents are closed on every
                    # exit but a spent budget (self-shielding, 5 s per file):
                    # the prover lives on after a cancelled tool call.
                    evaluation_state.cancel()
                    if not budget.cancel_called:
                        await _cleanup_auto_opened(client, evaluation_state)
                swept_line = await debugger.finish_cancel_sweep(
                    client, swept_hits, payload)
                message = render_cancel_outcome(payload, client.project_root)
                if swept_line:
                    message += "\n" + swept_line
            if budget.cancel_called:
                raise IsabelleCatastrophe(
                    f"cancellation exceeded its {CANCEL_TOTAL_BUDGET:g}s budget")
        except BaseException as exc:
            # a request that produced no success outcome: the attribution must
            # not stand (the teardown, if one follows, retires the hits itself)
            debugger.withdraw_sweep_attribution(swept_hits)
            if isinstance(exc, (IsabelleCatastrophe, asyncio.CancelledError)):
                raise
            raise IsabelleCatastrophe(f"cancellation failed: {exc!r}") from exc
        return EvaluationView(
            status="cancelled",
            destination_line=dest,
            message=message,
        )


def _cancel_item(entry: dict, root: str | None) -> str:
    where = relativize(entry.get("file", ""), root)
    line = entry.get("line")
    name = entry.get("command") or ""
    loc = f"{where}:{line}" if line is not None else where
    return f"{loc} ({name})" if name else loc


def render_cancel_outcome(payload: dict, root: str | None) -> str:
    """The agent-facing text for one successful PIDE/cancel_evaluation reply
    (force_interrupt admits no other)."""
    lines = [CANCEL_MESSAGES[payload["outcome"]]]
    retired = payload.get("retired") or []
    if retired:
        lines.append(CANCEL_RESET_LINE.format(
            items=", ".join(_cancel_item(r, root) for r in retired)))
    excluded = payload.get("excluded") or []
    if excluded:
        logger.info("cancel_evaluation: not retired by this request: %s", excluded)
    waived = payload.get("waived") or []
    if waived:
        lines.append(CANCEL_WAIVED_LINE.format(
            items=", ".join(_cancel_item(w, root) for w in waived)))
    return "\n".join(lines)


def position_state(
    client: IsabelleLSPClient, file_path: str, line: MCPLine,
) -> str:
    """State of the command(s) at *line*: one of the :mod:`processing` constants
    or ``FILE_NOT_OPEN``. Pure local computation — no request, no I/O.

    The open check comes first because a decoration tracker can outlive its
    document: closing drops the client's caches, but nothing guarantees the
    order, and reporting a closed file's stale cache as ``processed`` would let
    a query be served from a document the server no longer holds for us.
    """
    if client.open_documents.get(file_path) is None:
        return FILE_NOT_OPEN
    tracker = client.get_processing_tracker(file_path)
    if tracker is None:
        return NOT_EVALUATED
    return tracker.position_state(int(line.to_lsp()))


async def _settled_position_state(
    client: IsabelleLSPClient, file_path: str, line: MCPLine,
) -> str:
    """:func:`position_state`, but ``unknown`` is waited out rather than returned.

    ``unknown`` means only that an edit landed within the last
    ``DECORATION_GRACE`` seconds, so the cache cannot be trusted yet. Refusing on
    it would be unhelpful (the agent can do nothing but retry) and re-evaluating
    on it would be wasteful (the line may have finished minutes ago), so the
    guard simply waits the window out — at most two seconds — and asks again.
    The tracker's own wait wakes at expiry, or earlier if the line is reached.
    """
    async with _evaluation_state_lock:
        state = position_state(client, file_path, line)
    if state != UNKNOWN:
        return state

    tracker = client.get_processing_tracker(file_path)
    grace = _grace_remaining()
    if tracker is not None and grace > 0:
        await tracker.wait_until_line_reached_bounded(
            line.to_lsp(),
            timeout=grace + 0.1,
            health_check=lambda: client._check_server_health(client.STALL_TIMEOUT),
        )
    async with _evaluation_state_lock:
        return position_state(client, file_path, line)


def _failed_count(client: IsabelleLSPClient) -> int:
    """Failed commands across every open document, from the same snapshots the
    file sections are rendered from."""
    return sum(fs.error_count for fs in _open_document_snapshots(client))


async def evaluation_footer(client: IsabelleLSPClient) -> str:
    """Ambient context for a query-tool result: what the server is working
    toward, and what the prover is doing around it. Empty when there is nothing
    to say.

    Everything here is a read of the local decoration cache — no request, no
    round trip — with **one** exception: once the target line is reached, the
    verdict "complete" also requires every recursively imported theory to be
    done, which only ``theory_status`` knows. That request runs on every footer
    in the reached-but-not-complete window (one round trip per query — the same
    request evaluation_status's poll sends anyway) until the completion is
    stamped, which ends the run.

    That transition is not a display concern that happens to mutate: observing
    completion is a state change the server has to make somewhere, and today
    only ``isabelle_evaluation_status`` makes it — so an evaluation that
    finished quietly kept every query tool blocked until someone polled.
    """
    running = client.get_all_running_commands()
    from isabelle_mcp import debugger
    paused = debugger.paused_lead(client)

    def activity(running_commands: list[RunningCommand], n_failed: int) -> list[str]:
        # D-B15: while a hit is live, "has been running for Ns" is misleading —
        # the pause line replaces the whole activity clause.
        if paused is not None:
            return [paused]
        return _footer_activity(running_commands, n_failed)

    if not evaluation_state.active:
        # No target to name. The main sentence is dropped rather than paired with
        # a contradicting one: "Nothing is under evaluation." followed by
        # "2 commands have been running…" argues with itself. The call to action
        # stays: the agent is being told work is running, so it needs somewhere
        # to look.
        return " ".join(activity(running, 0))

    # Capture the handle HERE, with the target it belongs to and before any
    # await. Re-reading `current` at finish time would make _finish_if_owner's
    # ownership test a tautology, and the round trip below is exactly the window
    # in which another run can take over — a footer computed for one run would
    # then stamp its successor "complete".
    evaluation = evaluation_state.current
    target = evaluation_state.file_path
    dest = evaluation_state.destination_line
    root = client.project_root
    towards = _target_sentence(TOWARDS_SENTENCE, target, int(dest), root)

    if position_state(client, target, dest) == UNKNOWN:
        # A file changed a moment ago. Say the target and nothing else: the counts
        # would come from the same cache that is not trusted for the position.
        return towards

    tracker = client.get_processing_tracker(target)
    if tracker is None or not tracker.line_reached(dest.to_lsp()):
        return " ".join([towards, *activity(running, _failed_count(client))])

    theories = [
        _parse_theory_status(t) for t in await client.request_theory_status()
    ]
    if _is_evaluation_complete(target, dest, client, theories):
        # Count BEFORE finishing: the finish closes the auto-opened dependency
        # documents, whose failures then leave _failed_count — this suffix is
        # their only notification (D-C3).
        n_failed = _failed_count(client)
        # Under the lock, like the other terminal transitions: the stamp, the
        # flag and the cleanup travel together. Two gates on the sentence.
        # The outcome test: the run already ended for another reason during
        # the round trip above (a cancel or a heap abandonment),
        # and a COMPLETED sentence would contradict that reply; a completion
        # stamped by a concurrent observer passes — same verdict, same
        # sentence — re-finishing an ended run it owns re-stamps nothing
        # (write-once) and only re-closes a dependency auto-opened since. The
        # _finish_if_owner verdict: the target moved on (a same-file advance
        # in that round trip) or the run was replaced, so the verdict belongs
        # to the old target. Either gate failing falls to ARRIVED, true for
        # the target this footer judged; the next footer names the new state.
        async with _evaluation_state_lock:
            if (evaluation is not None and evaluation.outcome in ("", "complete")
                    and await _finish_if_owner(client, evaluation, "complete",
                                               judged_dest=dest)):
                return " ".join([
                    _target_sentence(COMPLETED_SENTENCE, target, int(dest), root),
                    *activity([], n_failed),
                ])
    if _frontier_reached(target, dest, client, theories):
        return " ".join([
            _target_sentence(ARRIVED_SENTENCE, target, int(dest), root),
            *activity(running, _failed_count(client)),
        ])
    return " ".join([towards, *_footer_activity(running, _failed_count(client))])


def _footer_activity(
    running: list[RunningCommand], n_failed: int,
) -> list[str]:
    """The footer's suffix sentences, with the call to action that earns them.

    *n_failed* is hardwired to 0 only on the ambient path with no run behind
    the footer (an error decoration outlives every run that produced it, so
    repeating its count forever would train the agent to stop reading);
    elsewhere the count — zero or not — belongs to the run this footer is
    judging or finishing, including the instant that run ends.
    """
    sentences = _activity_sentences(running, n_failed)
    if sentences:
        sentences.append(FOOTER_DETAILS_CALL)
    return sentences


async def check_evaluation_guard(
    client: IsabelleLSPClient,
    file_path: str,
    line: MCPLine,
) -> "EvaluationView | str | None":
    """Ensure *line* has been evaluated; raise, warn, or auto-start evaluation.

    (Auto-starts *evaluation of unevaluated lines* on an already-running session —
    it does not start the prover; the session must first be launched via
    ``isabelle_launch``.)

    The decision is made about the REQUESTED POSITION, not about the global
    evaluation flag: a position that is already processed is served even while an
    evaluation is outstanding elsewhere. Every query tool is position-explicit and
    moves no caret, so serving one competes with the evaluation for nothing.

    Returns:
      - ``None``: line is fully processed, caller can proceed.
      - ``str``: the command there is still executing, or was interrupted; the caller can
        proceed but should set ``result.note`` to this warning string.
      - ``EvaluationView``: auto-evaluation started but didn't complete; the caller
        renders it (``format_evaluation_result``) and raises it.
    Raises :class:`IsabelleToolError` when the position cannot be served.
    """
    await evaluation_target(client, file_path, redirect=False)
    state = await _settled_position_state(client, file_path, line)

    if state == PROCESSED:
        return None
    rel = relativize(file_path, client.project_root)
    if state == RUNNING:
        return RUNNING_NOTE.format(file=rel, line=int(line))
    if state == CANCELLED:
        return INTERRUPTED_NOTE.format(file=rel, line=int(line))
    if state == UNKNOWN:
        # Still inside the grace window after waiting it out — a further edit
        # landed. Do not auto-start (that would relocate the caret on a guess)
        # and do not claim the line was not reached (it may have finished long
        # ago); say only what is true.
        raise IsabelleToolError(
            UNKNOWN_POSITION_MESSAGE.format(file=rel, line=int(line)),
        )

    # NOT_EVALUATED or FILE_NOT_OPEN: work is needed. An evaluation of another
    # file has the prover; a request for the file under evaluation goes on to
    # evaluate_to, which advances the running target when the line is beyond
    # it and otherwise waits for it.
    async with _evaluation_state_lock:
        if evaluation_state.busy_with_another_file(file_path):
            raise IsabelleToolError(
                NOT_EVALUATED_REFUSAL.format(
                    file=rel,
                    line=int(line),
                    target=relativize(
                        evaluation_state.file_path, client.project_root,
                    ),
                    target_line=int(evaluation_state.destination_line),
                ),
            )

    result = await evaluate_to(client, file_path, int(line))
    if result.status == "complete":
        return None
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def relativize(path: str, root: str | None) -> str:
    real = os.path.realpath(path)
    if root is None:
        return real
    try:
        rel = os.path.relpath(real, root)
    except ValueError:
        return real
    # Only relativize when the file actually lives under root; otherwise relpath
    # produces ugly ../../.. traversals (e.g. project_root=cwd but the .thy is
    # elsewhere) — fall back to the absolute path in that case.
    return real if rel.startswith("..") else rel


def _fmt_spans(spans: list[tuple[int, int]]) -> str:
    """``line 45`` / ``lines 45-47`` / ``lines 45, 88-90``.

    The unit word is not decoration: without it ``warnings: 12`` reads as
    "12 warnings" rather than "a warning on line 12".
    """
    body = ", ".join(f"{s}" if s == e else f"{s}-{e}" for s, e in spans)
    single = len(spans) == 1 and spans[0][0] == spans[0][1]
    return f"{'line' if single else 'lines'} {body}"


def _snippet(text: str) -> str:
    """First line of a range's text, truncated. Need not be a whole command: the
    error rows are a line-deduped union of two markup kinds."""
    first = text.split("\n", 1)[0].strip()
    return (first[:60] + "...") if len(first) > 60 else first


def _count_bits(fs: FileSnapshot) -> str:
    parts = []
    if fs.error_count:
        parts.append(f"{fs.error_count} error" + ("s" if fs.error_count != 1 else ""))
    if fs.warning_count:
        parts.append(f"{fs.warning_count} warning" + ("s" if fs.warning_count != 1 else ""))
    if fs.running_count:
        parts.append(f"{fs.running_count} running")
    return ", ".join(parts)


def _format_file_snapshot(
    fs: FileSnapshot,
    root: str | None,
    running_commands: list[RunningCommand] | None = None,
) -> str:
    name = relativize(fs.file_path, root)
    if fs.lined:
        rows = []
        # `running` is the one row carrying something a line range cannot say —
        # how long the command has been at it — so it, and only it, nests. The
        # nested lines drop the file name: they belong to this section already.
        if fs.running:
            slow = sorted(
                (c for c in (running_commands or [])
                 if c.elapsed_seconds >= RUNNING_REPORT_THRESHOLD),
                key=lambda c: c.start_line,
            )
            if slow:
                rows.append("  running:")
                rows.extend(
                    f"    line {c.start_line} ({c.elapsed_seconds:.0f}s)"
                    f" {_snippet(c.text)}"
                    for c in slow
                )
            else:
                rows.append(f"  running: {_fmt_spans(fs.running)}")
        if fs.pending:
            rows.append(f"  pending: {_fmt_spans(fs.pending)}")
        if fs.errors:
            rows.append(f"  errors: {_fmt_spans(fs.errors)}")
        if fs.warnings:
            rows.append(f"  warnings: {_fmt_spans(fs.warnings)}")
        if not rows:
            return f"{name}: clean"
        return f"{name}:\n" + "\n".join(rows)
    # theory_status fallback (counts only)
    if fs.state == "in_progress":
        bits = _count_bits(fs)
        return f"{name}: in progress" + (f" ({bits} so far)" if bits else "")
    if fs.state == "clean":
        return f"{name}: clean"
    return f"{name}: {_count_bits(fs)} (no line info)"


def _worth_watching(view: EvaluationView) -> bool:
    """Whether there is anything to come back for: a command past the reporting
    threshold, or a failure. Nothing else justifies telling the agent to poll."""
    if any(
        c.elapsed_seconds >= RUNNING_REPORT_THRESHOLD for c in view.running_commands
    ):
        return True
    return any(fs.error_count for fs in view.files)


def format_evaluation_result(
    view: EvaluationView,
    root: str | None = None,
    *,
    call_to_action: bool = True,
) -> str:
    """Render an EvaluationView as the agent-facing plain-text snapshot.

    *call_to_action* is False for ``isabelle_evaluation_status``: it is the tool
    being called, so pointing at it is a self-reference with no next step in it.
    """
    running_by_file: dict[str, list[RunningCommand]] = {}
    for cmd in view.running_commands:
        running_by_file.setdefault(cmd.file_path, []).append(cmd)
    blocks: list[str] = []
    if view.heap_warning:
        blocks.append("⚠️ " + view.heap_warning)
    if view.message:
        blocks.append(view.message.rstrip("\n"))
    blocks.extend(
        _format_file_snapshot(fs, root, running_by_file.get(fs.file_path, []))
        for fs in view.files
    )
    if call_to_action and _worth_watching(view):
        blocks.append(CHECK_PROGRESS_CALL)
    return "\n\n".join(blocks)
