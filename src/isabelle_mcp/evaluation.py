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
from dataclasses import dataclass
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
    DECORATION_GRACE,
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
    acquire_within,
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

# Per-document close budget of the unified close: each close is shielded from a
# re-delivered cancel so a started didClose completes (not orphaned), but bounded
# so a stalled stdin.drain() cannot hang the tool call whose entry runs the sweep.
_CLOSE_TIMEOUT: float = 5.0

# How long the unified close waits for the breakpoint registry's lock before
# skipping the whole round. Near zero, but NEVER 0: asyncio.wait_for with a
# timeout <= 0 cancels the acquire before it runs, so even a free lock is never
# taken and the sweep would silently spin forever without closing anything.
SWEEP_LOCK_WAIT: float = 0.05

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
# The first line says whether the file snapshots below hold errors; {remain} is
# failed_remain_sentence's product. Approved verbatim.
IDLE_CLEAN_SENTENCE = (
    "No evaluation in progress. Nothing is running and no errors remain."
)
IDLE_FAILED_SENTENCE = (
    "No evaluation in progress. Nothing is running, but {remain}"
)

# The one wording for the failures that still stand — a state, not an event:
# the count is every failure currently on the books (open files and bad
# dependencies alike, session-wide) and belongs to no particular run. The
# positive side of IDLE_CLEAN_SENTENCE's "no errors remain". Approved verbatim.
FAILED_REMAIN_ONE = "1 failed command remains."
FAILED_REMAIN = "{n} failed commands remain."

# The summary line for theories with unprocessed commands and nothing failed or
# running (loading imports, nodes retracted by a cancel, open files never
# evaluated): they are counted here instead of listed per file. Approved
# verbatim; a state wording, true of all three kinds of member.
UNPROCESSED_THEORIES_ONE = "1 theory is not yet processed."
UNPROCESSED_THEORIES = "{n} theories are not yet processed."


def failed_remain_sentence(n: int) -> str:
    return FAILED_REMAIN_ONE if n == 1 else FAILED_REMAIN.format(n=n)


def unprocessed_theories_sentence(n: int) -> str:
    return UNPROCESSED_THEORIES_ONE if n == 1 else UNPROCESSED_THEORIES.format(n=n)

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
# The query tools' guard when the position has not been evaluated and nothing
# stands in the way of evaluating it (approved verbatim). Produced only by the
# last branch of check_evaluation_guard's dispatch, for the six query tools:
# a never-evaluated file, a theory the prover does not hold, a position beyond
# the evaluated frontier, or beyond the running evaluation's target. Queries
# never evaluate; isabelle_evaluate_to is the one call that changes this.
NOT_EVALUATED_MESSAGE = (
    "{file}:{line} has not been evaluated. Evaluate up to that line with "
    "isabelle_evaluate_to, then ask again."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_line(value: int, total_lines: int) -> MCPLine:
    if value < 0:
        return MCPLine(max(1, total_lines + 1 + value))
    return MCPLine(value)


def _parse_theory_status(raw: dict) -> TheoryStatus:
    """The one entry of prover paths into the Python side: ``node_name`` leaves
    here canonical (:func:`_canon`), so every map keyed by it and every
    comparison against an ``open_documents`` key agrees with the client's own
    keying — a symlinked node_name never splits one file into two.

    An empty node (a theory with no file) stays empty: ``os.path.realpath("")``
    is the current directory, and the truth tests on ``node_name`` rely on the
    empty string.
    """
    node = raw.get("node_name", "")
    return TheoryStatus(
        node_name=_canon(node) if node else "",
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
    path = _canon(file_path)
    return next((t.theory_name for t in theories if t.node_name == path), None)


def theory_settled(t: TheoryStatus) -> bool:
    """A theory_status row that has nothing failed, nothing unprocessed and
    nothing running: ``ok ∧ unprocessed == 0 ∧ running == 0``.

    Two consumers derive from it: a settled theory is mentioned nowhere in a
    report (neither listed per file — that needs ``failed > 0`` or
    ``running > 0`` — nor counted in the summary line — that needs
    ``unprocessed > 0``), and the unified close may close a settled file
    (with the other conditions of :func:`close_settled_documents`).
    :func:`_dependency_done` is NOT derived from it — see there.
    """
    return t.ok and t.unprocessed == 0 and t.running == 0


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


def evaluation_theory_set(target: str, theories: list[TheoryStatus]) -> set[str]:
    """The current evaluation's theory set (Phase D, approved 2026-08-19
    after two adversarial verification rounds): the files a hit can belong
    to for THIS run. The union of (a) the target, (b) the import closure's
    node_names (deps absent from the snapshot drop out — heap-precompiled
    code cannot hit), and (c) external entries whose theory_name is EMPTY —
    exactly the ML_file-loaded blobs ("all external" was refuted: the flag
    is never cleared, so it converges on everything not currently open).
    A hit in any other open file keeps its notice identity. All canonical
    (node_names are canonical from :func:`_parse_theory_status`)."""
    out = {_canon(target)}
    theory_map = {t.theory_name: t for t in theories}
    target_name = _find_theory_name(target, theories)
    if target_name is not None:
        for dep in _get_recursive_dependencies(target_name, theories):
            t = theory_map.get(dep)
            if t is not None and t.node_name:
                out.add(t.node_name)
    for t in theories:
        if t.external and not t.theory_name and t.node_name:
            out.add(t.node_name)
    return out


def _dependency_done(t: TheoryStatus) -> bool:
    """A dependency the completion verdict need not wait for.

    Deliberately NOT :func:`theory_settled`: the two share only the core
    ``running == 0 ∧ unprocessed == 0``. A failed dependency that has gone
    quiet is a settled old debt for the completion verdict — adding an ``ok``
    conjunct here would keep any evaluation with a broken import from ever
    completing (``active`` never released, every other file refused as busy),
    now that a bad dependency staying open and reported is the normal state.
    ``canceled`` and ``consolidated`` are ruled on here, once: both end the wait.
    """
    return (
        t.canceled
        or t.consolidated
        or (t.running == 0 and t.unprocessed == 0)
        or (t.running == 0 and not t.ok)
    )


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


def _still_running_sentence(n: int) -> str:
    """``1 command is still running.`` / ``2 commands are still running.``

    *n* is the open documents' running-command count when there is one, else
    theory_status's total of running commands: a dependency running in a
    closed file is what put the session on the busy path, and the sentence
    must not say ``0 commands`` about it.
    """
    return f"{plural(n, 'command')} {'is' if n == 1 else 'are'} still running."


def _activity_sentences(
    running_commands: list[RunningCommand], n_failed: int,
) -> list[str]:
    """What the prover is doing, in whole sentences: the commands running past
    the reporting threshold, and the failures that still stand (*n_failed*,
    session-wide — see :func:`_failed_count`)."""
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
        sentences.append(failed_remain_sentence(n_failed))
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
    current: Evaluation | None = None

    def start(self, file_path: str, destination_line: MCPLine) -> Evaluation:
        self.active = True
        self.file_path = file_path
        self.destination_line = destination_line
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
        loop's _finish_if_owner. The caller holds ``_evaluation_state_lock``.
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

    def join_only(self, file_path: str, line: MCPLine) -> Evaluation | None:
        """A query's pure wait: ride the run on *file_path* when that run is
        already going to reach *line*, else None.

        Only a rider is added — the target is never advanced and no caret
        moves; a query past the run's target is refused elsewhere instead of
        pulling the run along. The caller holds ``_evaluation_state_lock`` and
        steps off with :meth:`unjoin`.
        """
        if not (self.active and self.file_path == file_path
                and line <= self.destination_line):
            return None
        assert self.current is not None
        self.current.riders += 1
        return self.current

    @staticmethod
    def unjoin(evaluation: Evaluation) -> None:
        """A query rider steps off *evaluation*. The count drops; the run's
        fate is untouched — only isabelle_evaluate_to's own requests end a
        run (:meth:`leave`), a query never does."""
        evaluation.riders -= 1

    @staticmethod
    def leave(evaluation: Evaluation) -> bool:
        """This request gives up on *evaluation*; True when it must also END it.

        Only the last request that had not given up on the run may end it —
        anyone still inside the wait, and anyone that returned in_progress
        meaning to poll again, is left the run. On a run that already ended
        this can still say True; _finish_if_owner turns that into the no-op
        it is.
        """
        evaluation.riders -= 1
        return evaluation.riders == 0

    def owns(self, evaluation: Evaluation) -> bool:
        """Whether *evaluation* is still the run this state describes.

        ``current`` is deliberately never reset: a run that ended with no
        successor must still recognise itself as the owner and stamp its own
        outcome.
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
) -> tuple[list[TheoryStatus], list[RunningCommand]]:
    """Pull theory_status, auto-open every failed theory, collect running commands.

    Auto-opening a not-ok theory (load-bearing side effect) gives it a decoration
    tracker so the snapshot can report its problems with line numbers. The
    coverage is the whole document model — a broken library import is opened
    and reported like any other file, until it is fixed — and the only guard
    is "not open yet": there is no bookkeeping of what was auto-opened, since
    the unified close is the one closer and closes such a file the moment it
    is settled. No diagnostics are read — the snapshot is built from
    decoration + theory_status (see :func:`_build_file_snapshot`).
    """
    raw_theories = await client.request_theory_status()
    theories = [_parse_theory_status(t) for t in raw_theories]

    for t in theories:
        if not t.ok and t.node_name and t.node_name not in client.open_documents:
            try:
                await client.open_document(t.node_name)
            except OSError:
                # open failed before didOpen (e.g. unreadable path): nothing to
                # report with line numbers, the count fallback still lists it.
                logger.debug("auto-open of %s failed", t.node_name, exc_info=True)

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

    Built fully synchronously (no await between getter reads) so every getter reads
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
        sorry = tracker.get_sorry_ranges()
        oerr = tracker.get_overview_error_ranges()
        owarn = tracker.get_overview_warning_ranges()
        running = tracker.get_running_ranges()
        unproc = tracker.get_unprocessed_ranges()
        # bad and owarn stay in this test on purpose: neither is rendered, but
        # either proves the decoration is fresh content. sorry is named here in
        # its own right — the server also publishes a sorry as bad today (the
        # distribution rendering is untouched), but nothing may rely on that.
        deco_has_content = bool(bad or sorry or oerr or owarn or running or unproc)
        # theory_status reports a problem/activity the decoration should reflect.
        ts_active_or_problem = ts is not None and (
            ts.unprocessed > 0 or ts.running > 0 or ts.failed
        )
        # Trust decoration when it carries content, or when theory_status agrees
        # there is nothing to show. Only fall back when theory_status reports a
        # problem/activity that the (stale) decoration does NOT reflect — e.g. a
        # dependency re-invalidated by an edit, whose decoration lags.
        if deco_has_content or not ts_active_or_problem:
            # errors = text_overview_error and nothing else: a sorry gets its own
            # row and no count, and the prover's other "bad" commands (a benign
            # `back`, say) render nothing.
            errors = _merge_spans(_line_spans(oerr, n_lines))
            sorry_spans = _merge_spans(_line_spans(sorry, n_lines))
            running_spans = _line_spans(running, n_lines)
            pending_spans = (
                _pending_spans(unproc, int(dest_line.to_lsp()), n_lines)
                if dest_line is not None else []
            )
            if errors:
                state = "problems"
            elif running_spans or pending_spans:
                state = "in_progress"
            else:
                state = "clean"
            return FileSnapshot(
                file_path=file_path, lined=True, state=state,
                errors=errors, sorry=sorry_spans, running=running_spans,
                pending=pending_spans,
                error_count=len(errors), running_count=len(running_spans),
                pending_count=len(pending_spans),
            )

    # theory_status fallback (counts only, no line numbers)
    if ts is None:
        return FileSnapshot(file_path=file_path, lined=False, state="in_progress")
    if ts.unprocessed > 0 or ts.running > 0 or not ts.consolidated:
        state = "in_progress"
    elif ts.failed:
        state = "problems"
    else:
        state = "clean"
    return FileSnapshot(
        file_path=file_path, lined=False, state=state,
        error_count=ts.failed, running_count=ts.running,
    )


def _listed_by_theory_status(t: TheoryStatus) -> bool:
    """A theory_status row that gets its own file snapshot: something failed or
    something is running there. Rows with only unprocessed commands are
    counted by :func:`_unprocessed_theory_count` instead; a row that is
    neither is :func:`theory_settled` and appears nowhere."""
    return t.failed > 0 or t.running > 0


def _unprocessed_theory_count(
    theories: list[TheoryStatus], rendered: set[str],
) -> int:
    """The summary line's N: theories with unprocessed commands that the
    report does not show per file — neither listed by theory_status (nothing
    failed, nothing running) nor rendered for another reason (*rendered*: this
    run's target, a decoration-scanned open document). A file with its own
    section is never also counted here; note that only the target's section
    states its unprocessed prefix (the ``pending`` row), so for a
    decoration-scanned entrant the fact is dropped, not relocated. Over the
    whole document model, the same set the per-file listing draws from."""
    return sum(
        1 for t in theories
        if t.unprocessed > 0 and not _listed_by_theory_status(t)
        and t.node_name not in rendered
    )


def _summary_count(theories: list[TheoryStatus], files: list[FileSnapshot]) -> int:
    """:func:`_unprocessed_theory_count` for the snapshots a report renders."""
    return _unprocessed_theory_count(theories, {fs.file_path for fs in files})


def _relevant_files(
    client: IsabelleLSPClient, target: str, theories: list[TheoryStatus],
) -> list[str]:
    """The files a report shows, in order — the explicit union of three parts:

    1. this run's target file (empty whenever no run is outstanding — the
       idle report and the busy report on a lingering fork alike: only
       ``start()`` writes ``file_path`` and it is never reset, so an ended
       run's target must not seed the report; and seeding "" would snapshot
       the empty path, since relativize("") renders the project root as a
       file);
    2. every theory_status row with something failed or running, over the WHOLE
       document model, not just the target's import closure (a broken library
       import is reported until it is fixed) — node_names are canonical from
       :func:`_parse_theory_status`, so a symlinked spelling cannot list one
       file twice or miss the ts_map;
    3. every open document whose decoration tracker holds a bad, sorry,
       text_overview_error or running range.

    Part 3 is the only way a file that theory_status sees nothing wrong with
    gets into a report: theory_status is blind to ``sorry``, so a file whose
    only mark is a sorry enters here, or not at all — through the sorry
    getter named explicitly, never through the bad range the server happens
    to publish alongside it.
    """
    files: list[str] = [target] if target else []
    for t in theories:
        if _listed_by_theory_status(t) and t.node_name and t.node_name not in files:
            files.append(t.node_name)
    for path in list(client.open_documents):
        if path in files:
            continue
        tr = client.get_processing_tracker(path)
        if tr is not None and (
            tr.get_bad_ranges() or tr.get_sorry_ranges()
            or tr.get_overview_error_ranges() or tr.get_running_ranges()
        ):
            files.append(path)
    return files


def _has_rows(fs: FileSnapshot) -> bool:
    """Whether a lined snapshot renders any row (see :func:`_format_file_snapshot`)."""
    return bool(fs.errors or fs.sorry or fs.running or fs.pending)


def _snapshot_files(
    client: IsabelleLSPClient,
    target: str,
    theories: list[TheoryStatus],
    dest_line: MCPLine | None = None,
) -> list[FileSnapshot]:
    """One snapshot per relevant file (:func:`_relevant_files`), synchronous so
    every snapshot reads one tracker state.

    A file that only part 3 of the union brought in and that renders no row —
    a benign ``background_bad`` range such as ``back`` — is left out: its
    snapshot would say nothing but ``clean``. The target and the theory_status
    rows keep their snapshots whatever they render.
    """
    ts_map = {t.node_name: t for t in theories}
    listed = {t.node_name for t in theories if _listed_by_theory_status(t)}
    out: list[FileSnapshot] = []
    for f in _relevant_files(client, target, theories):
        # Only the evaluation target gets dest_line — pending is the prefix
        # [0, dest] of the file actually being evaluated; other files have no
        # destination.
        fs = _build_file_snapshot(client, f, ts_map, dest_line if f == target else None)
        if f == target or f in listed or not fs.lined or _has_rows(fs):
            out.append(fs)
    return out


async def _finish_if_owner(
    client: IsabelleLSPClient, evaluation: Evaluation, outcome: Outcome,
    *, judged_dest: MCPLine | None,
) -> bool:
    """End *evaluation* — flag and outcome stamp — if it still owns the state
    and, for a completion, the target is still the one it was judged at.

    Only the run that started this state may stamp it: a run that finishes
    after a later run took the state over (evaluate_to holds no lock across
    the wait) must not stamp the successor. Closing documents is not this
    function's business — the unified close is the one closer.

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

    Synchronous: the tests and the flag flip run with no await between them.
    """
    if not evaluation_state.owns(evaluation):
        return False
    if judged_dest is not None and evaluation_state.destination_line != judged_dest:
        return False
    evaluation_state._finish(outcome)
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

    def __init__(self, client: IsabelleLSPClient, target: str) -> None:
        self._client = client
        self._target = target
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
                theory_set = evaluation_theory_set(self._target, theories)
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
                theory_set = evaluation_theory_set(self._target, theories)
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
    hit_watch = _HitWatch(client, file_path)
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
        theories, running_commands = await _build_status_snapshot(client)
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
        # refusal lives here, in the one function that moves the caret, so
        # the caret never moves on a hit (queries never evaluate and never
        # reach this point).
        hits_refusal = debugger.hits_live_refusal(client)
        if hits_refusal is not None:
            raise IsabelleToolError(hits_refusal)
        if evaluation_state.busy_with_another_file(file_path):
            raise IsabelleToolError(EVALUATE_TO_REFUSAL.format(
                target=relativize(evaluation_state.file_path, client.project_root),
                target_line=int(evaluation_state.destination_line),
                activity=_activity_clause(client.get_all_running_commands()),
            ))

        await client.open_document(file_path, evaluation_target=True)
        heap_warning = client.heap_warning(file_path)
        doc = client.open_documents.get(_canon(file_path))
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
                # given up on it. Only isabelle_evaluate_to's own requests can
                # end a run: a query riding the pure-wait channel only counts
                # itself in and out. So a lone starter ends the run here, while
                # the last evaluate_to request aborting with a query still
                # riding leaves it active (riders reach 0 when the query steps
                # off); that run is honest — an earlier request's caret is on
                # the wire and the prover is working towards it — and
                # evaluation_status's terminal transition or an explicit
                # cancel closes it out.
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
        # wait-loop await. _finish_if_owner resets synchronously — awaiting a
        # coroutine is not itself a checkpoint, so its ownership test and flag
        # flip always run. Mirrors isabelle_launch's cancellation cleanup.
        # leave() decides who may end the shared run: only the last
        # isabelle_evaluate_to request that had not given up on it (a query
        # riding the pure-wait channel never ends a run — see the caret
        # handler above for the state that leaves behind).
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
    files = _snapshot_files(client, file_path, theories, dest_line)
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
        # fabricate one.
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
        unprocessed_theories=_summary_count(theories, files),
        heap_warning=heap_warning,
    )


def _no_evaluation_view() -> EvaluationView:
    return EvaluationView(
        status="no_evaluation",
        message="No evaluation in progress.",
    )


def _no_pending_work(
    client: IsabelleLSPClient, theories: list[TheoryStatus],
) -> bool:
    """Whether there is genuinely nothing left to watch or cancel.

    ``evaluate_to`` clears ``evaluation_state.active`` as soon as the execution
    frontier reaches the target line — but the command there may have forked
    background work (``value``, an asynchronous proof) that keeps running. Such a
    fork is invisible to ``active`` and shows up only in the running-command
    list, so both ``evaluation_status`` and ``cancel_evaluation`` consult that
    list before reporting that nothing is in progress. A dependency running in
    a file that is not open is invisible to that list too, so the theory_status
    ``running`` counts are consulted as well; ``unprocessed`` counts are NOT —
    a file opened but never evaluated has unprocessed commands forever, and
    counting it would keep the session from ever being idle.
    """
    return (
        not evaluation_state.active
        and not client.get_all_running_commands()
        and not any(t.running > 0 for t in theories)
    )


def _idle_view(client: IsabelleLSPClient, theories: list[TheoryStatus]) -> EvaluationView:
    """The status when no run is outstanding and nothing is running: every
    error that still stands, in every file, with line numbers where a
    decoration tracker has them.

    The same data path as the busy report (``_snapshot_files`` over the
    post-debounce theory_status, after the auto-open of every failed theory),
    with no target: a session-level picture, not the last run's. The first
    line's count is taken from the very snapshots rendered below it, so the
    two cannot disagree.
    """
    files = _snapshot_files(client, "", theories)
    n_failed = sum(fs.error_count for fs in files)
    message = (
        IDLE_FAILED_SENTENCE.format(remain=failed_remain_sentence(n_failed))
        if n_failed else IDLE_CLEAN_SENTENCE
    )
    return EvaluationView(
        status="no_evaluation", message=message, files=files,
        unprocessed_theories=_summary_count(theories, files),
    )


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

    # Capture the run handle BEFORE the await: the round trip below is the one
    # window in which another run can take over, and the stamp must go to the
    # run this call judged, never to a successor (D-B12).
    evaluation = evaluation_state.current
    # One theory_status, pulled AFTER the debounce, decides idle-or-busy and
    # feeds the report: the answer describes the post-edit document either way.
    theories, running_commands = await _build_status_snapshot(client)
    if _no_pending_work(client, theories):
        return _idle_view(client, theories)

    # A target exists only while a run is outstanding. ``file_path`` and
    # ``destination_line`` are never reset (the footer still reads them), so
    # on the busy path with no run — a lingering fork, a dependency running in
    # a closed file — the last run's target must not seed the report or clip
    # a pending row: the picture is session-level then, as when idle.
    active = evaluation_state.active
    target = evaluation_state.file_path if active else ""
    dest_line = evaluation_state.destination_line if active else None
    dest = int(dest_line) if dest_line is not None else None

    complete = dest_line is not None and _is_evaluation_complete(
        target, dest_line, client, theories,
    )
    files = _snapshot_files(client, target, theories, dest_line)
    n_unprocessed = _summary_count(theories, files)
    # Only the active evaluation owns the completion; once ``active`` is False
    # we are merely surfacing a lingering fork, which must stay visible (not
    # collapse back to "complete"). _finish_if_owner is the ONE termination
    # implementation, and its verdict gates the sentence: a run replaced during
    # the round trip is not stamped, and the in_progress branches below
    # describe the current run truthfully — the next poll completes it.
    if (complete and dest_line is not None and evaluation is not None
            and await _finish_if_owner(client, evaluation, "complete",
                                       judged_dest=dest_line)):
        return EvaluationView(
            status="complete",
            target_file=target,
            destination_line=dest,
            message=_target_sentence(
                COMPLETED_SENTENCE, target, int(dest_line), client.project_root,
            ),
            files=files,
            running_commands=running_commands,
            unprocessed_theories=n_unprocessed,
        )

    if dest_line is None:
        # No evaluation is outstanding; what is left is a fork still settling, or
        # work the agent did not start (a re-evaluation triggered by a save).
        # There is no target to name, so name only the activity — and without the
        # footer's 10s threshold, which exists to keep an ambient line quiet. This
        # tool's whole job is to report status, so it says something either way.
        # No call to action either: this IS the tool one would be pointed at.
        message = _still_running_sentence(
            len(running_commands) or sum(t.running for t in theories),
        )
    else:
        message = _progress_sentence(client, target, dest_line, theories)
    return EvaluationView(
        status="in_progress",
        target_file=target or None,
        destination_line=dest,
        message=message,
        files=files,
        running_commands=running_commands,
        unprocessed_theories=n_unprocessed,
    )


def _progress_sentence(
    client: IsabelleLSPClient, target: str, dest_line: MCPLine,
    theories: list[TheoryStatus],
) -> str:
    """ARRIVED once the frontier has passed the target, else TOWARDS."""
    template = (
        ARRIVED_SENTENCE if _frontier_reached(target, dest_line, client, theories)
        else TOWARDS_SENTENCE
    )
    return _target_sentence(template, target, int(dest_line), client.project_root)


async def _progress_view(client: IsabelleLSPClient) -> EvaluationView:
    """The running evaluation's progress, as the query guard reports it after
    its bounded wait ran out: the same picture ``evaluation_status`` paints,
    minus that tool's terminal transition — a query reports, it never ends a
    run. The caller holds no lock; the run must be active."""
    theories, running_commands = await _build_status_snapshot(client)
    target = evaluation_state.file_path
    dest_line = evaluation_state.destination_line
    files = _snapshot_files(client, target, theories, dest_line)
    return EvaluationView(
        status="in_progress",
        target_file=target,
        destination_line=int(dest_line),
        message=_progress_sentence(client, target, dest_line, theories),
        files=files,
        running_commands=running_commands,
        unprocessed_theories=_summary_count(theories, files),
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

    The parsed theory_status is stashed on the client (``entry_theories``) for
    the unified close that follows and for the paths that must answer without
    a fresh round trip.
    """
    theories = [_parse_theory_status(t) for t in await client.request_theory_status()]
    client.entry_theories = theories
    dep_nodes = [
        t.node_name for t in theories
        if t.external and t.node_name and t.node_name not in client.open_documents
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


async def close_settled_documents(client: IsabelleLSPClient) -> None:
    """The unified close: the one mechanism that closes documents.

    Closes every open document that is settled (:func:`theory_settled` on the
    entry theory_status), carries no evaluation-target mark, and has no entry
    in the breakpoint registry (armed or pending — closing a file silently
    demotes every breakpoint on it). What stays open is exactly the evaluation
    targets and the files that still have something wrong or in flight.

    Runs after the tool entry's sync backstop, so theory_status describes the
    text the prover has absorbed. Non-blocking under the post-edit grace gate:
    with an edit still settling, nothing is closed this round — the invariant
    is "eventually", not "on every call" (the auto-open's own didOpen raises
    the gate, so this deferral is systematic and must not become a wait).

    Locks: ``_evaluation_state_lock`` — closing changes the document model,
    like the Layer-2 sync at the same entry — and then, bounded by
    SWEEP_LOCK_WAIT, the breakpoint registry's lock, in the production lock
    order (cancel_evaluation → finish_cancel_sweep takes them the same way).
    Holding the registry lock makes a breakpoint tool's arming transaction —
    several awaits between "entry exists" and "site armed" — invisible to the
    sweep; the exemption read itself is atomic anyway. When the registry lock
    is busy the whole round is skipped (a file stays open one round longer),
    and the bound doubles as a fuse: a lock-order violation elsewhere degrades
    to a skipped round instead of a deadlock. It is a fuse, not permission to
    relax the rule that no evaluation runs while the registry lock is held —
    the boundary is evaluation, not any prover round trip (fetch_sites stays
    inside that lock; the reopen of a swept theory stays outside it). One
    structural consequence: the cancel sweep and this entry sweep both take
    the evaluation-state lock first, so the two can never contend for the
    registry lock. The sweep does no bookkeeping, only closes, so skipping is
    lossless.
    """
    from isabelle_mcp import debugger
    async with _evaluation_state_lock:
        if _grace_remaining() > 0:
            return
        async with acquire_within(debugger.registry.lock, SWEEP_LOCK_WAIT) as held:
            if not held:
                logger.debug("unified close: registry.lock busy; skipping this round")
                return
            exempt = {entry.file_path for entry in debugger.registry.entries}
            ts_map = {t.node_name: t for t in client.entry_theories}
            for path, doc in list(client.open_documents.items()):
                if doc.is_evaluation_target or path in exempt:
                    continue
                ts = ts_map.get(path)
                if ts is None or not theory_settled(ts):
                    continue
                # Each close is shielded (a cancel re-delivered mid-didClose
                # would otherwise orphan the document server-side) and bounded
                # by _CLOSE_TIMEOUT: one stalled pipe must not hang the tool
                # call that merely ran the sweep at its entry.
                try:
                    with anyio.move_on_after(_CLOSE_TIMEOUT, shield=True):
                        await client.close_document(path)
                except Exception:
                    logger.warning("unified close: failed to close %s", path, exc_info=True)


async def resync_and_check_freshness(client: IsabelleLSPClient) -> None:
    """Tool-call entry backstop: Layer 2 (open docs) + Layer 3 (dependency)
    freshness, then the unified close.

    Runs at the start of every tool call (see ``_ensure_lsp_started``). Layer 2
    holds ``_evaluation_state_lock`` — it mutates document content/version. Layer 3
    runs **lock-free**: it only issues a read-only ``theory_status`` request and
    maintains its own ``_dep_stat_sigs``, touching no lock-protected state, so it must
    not block (or be blocked by) the event-driven push path. The unified close
    comes last: it judges from Layer 3's theory_status, which describes the
    text the two sync layers have just pushed.
    """
    await resync_changed_open_documents_locked(client)   # Layer 2 (locked)
    wait = await _dependency_freshness_wait(client)        # Layer 3 (lock-free)
    if wait > 0:
        logger.info(
            "Dependency changed <%.2fs ago; waiting %.2fs for the server to notice it",
            wait, wait,
        )
        await asyncio.sleep(wait)
    await close_settled_documents(client)


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
        # The entry theory_status: no request may be issued under this lock.
        if _no_pending_work(client, client.entry_theories):
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
                    # refused.
                    evaluation_state.cancel()
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
    await _wait_out_grace(client, file_path, line)
    async with _evaluation_state_lock:
        return position_state(client, file_path, line)


async def _wait_out_grace(
    client: IsabelleLSPClient, file_path: str, line: MCPLine | None = None,
) -> None:
    """Wait until the post-edit grace window has really closed, whatever
    the position under it says — bounded: an edit landing during the wait
    re-arms the gate and is waited for too, up to one extra window in all
    (DECORATION_GRACE past the first expiry), then the caller reads what it
    can. Not for ``evaluation_status``, whose entry debounce deliberately
    waits for the edits to STOP, with no bound.

    The one implementation behind the "the gate is open, wait it out" step
    of the settled read above, of command_status's batch wait after its
    reopens and of the site tools' wait after theirs. The predicate is the
    GATE, never a position's verdict: ``range_state`` scans the unprocessed
    ranges before it consults the gate, so a position past the frontier
    answers at once and a wait keyed on it would skip the window while every
    other position still reads ``unknown``. With *line* and a tracker in
    hand each pass wakes early once the frontier passes the line —
    ``line_reached`` itself requires a fresh cache, so that is never before
    the gate closes.
    """
    deadline = time.monotonic() + DECORATION_GRACE + 0.1
    while (grace := _grace_remaining()) > 0 and time.monotonic() < deadline:
        tracker = client.get_processing_tracker(file_path)
        if tracker is not None and line is not None:
            await tracker.wait_until_line_reached_bounded(
                line.to_lsp(),
                timeout=grace + 0.1,
                health_check=lambda: client._check_server_health(client.STALL_TIMEOUT),
            )
        else:
            await asyncio.sleep(grace)


def _failed_count(client: IsabelleLSPClient, theories: list[TheoryStatus]) -> int:
    """The failures that still stand, session-wide: the sum of ``error_count``
    over the very file snapshots a report would render from *theories* — one
    computation behind every ``N failed commands remain.`` (open files and bad
    dependencies alike; a dependency whose decoration has not arrived counts
    by its theory_status ``failed``, exactly as its snapshot renders)."""
    return sum(fs.error_count for fs in _snapshot_files(client, "", theories))


FOOTER_HIT_DETAILS_CALL = "Call isabelle_debug_state for the hit details."


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
    from isabelle_mcp import debugger
    paused = debugger.paused_lead(client)
    # D-B15 (revised 2026-08-31): a command stopped at a breakpoint still
    # counts as running, so "has been running for Ns" would mislead — with a
    # live hit the running list is emptied up here (every outlet at once; no
    # call site can forget) and the pause line is appended as its own second
    # line. Failure sentences survive: they are a closed dependency's only
    # notification (D-C3).
    running = [] if paused is not None else client.get_all_running_commands()
    line = await _footer_status_line(client, running)
    if paused is None:
        return line
    pause_line = f"{paused} {FOOTER_HIT_DETAILS_CALL}"
    return f"{line}\n{pause_line}" if line else pause_line


async def _footer_status_line(
    client: IsabelleLSPClient, running: list[RunningCommand],
) -> str:
    """The footer's status line: target sentence plus activity, built from
    the caller's (possibly emptied) running-command list."""
    if not evaluation_state.active:
        # No target to name. The main sentence is dropped rather than paired with
        # a contradicting one: "Nothing is under evaluation." followed by
        # "2 commands have been running…" argues with itself. The call to action
        # stays: the agent is being told work is running, so it needs somewhere
        # to look. The failure count is the session-wide one, from the entry
        # theory_status (no fresh data in hand, and no round trip here).
        return " ".join(
            _footer_activity(running, _failed_count(client, client.entry_theories)),
        )

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
        # No fresh data in hand: the entry theory_status.
        return " ".join([
            towards,
            *_footer_activity(running, _failed_count(client, client.entry_theories)),
        ])

    theories = [
        _parse_theory_status(t) for t in await client.request_theory_status()
    ]
    if _is_evaluation_complete(target, dest, client, theories):
        # The same theory_status the completion verdict was judged on: the
        # suffix's N and the verdict describe one instant.
        n_failed = _failed_count(client, theories)
        # Under the lock, like the other terminal transitions: the stamp and
        # the flag travel together. Two gates on the sentence.
        # The outcome test: the run already ended for another reason during
        # the round trip above (a cancel or a heap abandonment),
        # and a COMPLETED sentence would contradict that reply; a completion
        # stamped by a concurrent observer passes — same verdict, same
        # sentence — re-finishing an ended run it owns re-stamps nothing
        # (write-once) and has no other effect. The
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
                    *_footer_activity([], n_failed),
                ])
    if _frontier_reached(target, dest, client, theories):
        return " ".join([
            _target_sentence(ARRIVED_SENTENCE, target, int(dest), root),
            *_footer_activity(running, _failed_count(client, theories)),
        ])
    return " ".join([towards, *_footer_activity(running, _failed_count(client, theories))])


def _footer_activity(
    running: list[RunningCommand], n_failed: int,
) -> list[str]:
    """The footer's suffix sentences, with the call to action that earns them.

    *n_failed* is the session-wide count of failures that still stand
    (:func:`_failed_count`), on every path — with or without a run behind the
    footer. It is a state, worded as one (``N failed commands remain.``): a
    failure in a dependency that is not open has no other outlet than this
    line and the status tool, so the count is repeated for as long as it is
    true.
    """
    sentences = _activity_sentences(running, n_failed)
    if sentences:
        sentences.append(FOOTER_DETAILS_CALL)
    return sentences


async def reopen_held_theory(client: IsabelleLSPClient, file_path: str) -> bool:
    """Reopen a ``.thy`` the prover holds but this client has closed (the
    unified close tidied it away); True when a didOpen was sent.

    The prover's holding is read from the entry theory_status — no request.
    The reopen carries the evaluation-target mark, so the file stays open for
    the rest of the session; it evaluates nothing and moves no caret. A theory
    the prover does not hold, a file that is open already, and any non-``.thy``
    path (a ``.ML`` blob shows up in theory_status too, and is never a
    document of ours) are left alone.
    """
    path = _canon(file_path)
    if not path.endswith(".thy") or path in client.open_documents:
        return False
    if all(t.node_name != path for t in client.entry_theories):
        return False
    await client.open_document(path, evaluation_target=True)
    return True


def _served_state(state: str, rel: str, line: MCPLine) -> "str | None":
    """The guard's answer for a judged position, or ``""`` when the position
    is not served: None (processed), a note (running / interrupted), or a
    raised refusal (unknown — still inside the grace window after waiting it
    out; the line may have finished long ago, so say only what is true)."""
    if state == PROCESSED:
        return None
    if state == RUNNING:
        return RUNNING_NOTE.format(file=rel, line=int(line))
    if state == CANCELLED:
        return INTERRUPTED_NOTE.format(file=rel, line=int(line))
    if state == UNKNOWN:
        raise IsabelleToolError(
            UNKNOWN_POSITION_MESSAGE.format(file=rel, line=int(line)),
        )
    return ""


async def _wait_for_line(client: IsabelleLSPClient, file_path: str, line: MCPLine) -> None:
    """The pure wait of the guard: at most EVAL_POLL_INTERVAL for the running
    evaluation's frontier to pass *line* (health-checked like every wait)."""
    tracker = client.get_processing_tracker(file_path)
    if tracker is None:
        return
    await tracker.wait_until_line_reached_bounded(
        line.to_lsp(),
        timeout=EVAL_POLL_INTERVAL,
        health_check=lambda: client._check_server_health(client.STALL_TIMEOUT),
    )


async def check_evaluation_guard(
    client: IsabelleLSPClient,
    file_path: str,
    line: MCPLine,
) -> "EvaluationView | str | None":
    """Serve a query about *line* if it has been evaluated; otherwise say so.

    Queries never evaluate. The dispatch, in order of priority:

    1. The position has been evaluated — served (a still-running or
       interrupted command is served with a note; an untrustworthy cache is
       refused with the retry sentence).
    2. An evaluation is running towards this file and its target covers the
       line — a pure wait of at most EVAL_POLL_INTERVAL for the frontier to
       reach it, then judged again; on timeout the evaluation's progress is
       reported. The wait adds a rider and nothing else: no target moves, no
       caret moves, no run is started or ended.
    3. The prover holds the theory but this client closed it — reopened (the
       unified close's counterpart; about two seconds, no proof re-runs) and
       judged again through the settled read, since the reopen's own didOpen
       raises the grace gate.
    4. Still not evaluated while another file is under evaluation — the
       refusal naming that evaluation.
    5. Otherwise — the honest error: evaluate up to the line first.

    The decision is made about the REQUESTED POSITION, not about the global
    evaluation flag: a position that is already processed is served even while an
    evaluation is outstanding elsewhere. Every query tool is position-explicit and
    moves no caret, so serving one competes with the evaluation for nothing.

    Returns:
      - ``None``: line is fully processed, caller can proceed.
      - ``str``: the command there is still executing, or was interrupted; the caller can
        proceed but should set ``result.note`` to this warning string.
      - ``EvaluationView``: the wait of branch 2 ran out; the caller renders it
        (``format_evaluation_result``) and raises it.
    Raises :class:`IsabelleToolError` when the position cannot be served.
    """
    await evaluation_target(client, file_path, redirect=False)
    rel = relativize(file_path, client.project_root)

    async def judged() -> "str | None":
        return _served_state(await _settled_position_state(client, file_path, line), rel, line)

    # 1. already evaluated
    answer = await judged()
    if answer != "":
        return answer

    # 2. the pure wait on the run that will reach this line
    async with _evaluation_state_lock:
        run = evaluation_state.join_only(file_path, line)
    if run is not None:
        try:
            await _wait_for_line(client, file_path, line)
        finally:
            EvaluationState.unjoin(run)
        answer = await judged()
        if answer != "":
            return answer
        if evaluation_state.active and evaluation_state.file_path == file_path:
            return await _progress_view(client)
        # The run ended under the wait (a cancel): fall through — the line is
        # honestly not evaluated now.

    # 3. reopen what the prover still holds, then judge again
    if await reopen_held_theory(client, file_path):
        answer = await judged()
        if answer != "":
            return answer

    # 4. another file has the prover
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

    # 5. not evaluated, and nothing but isabelle_evaluate_to changes that
    raise IsabelleToolError(NOT_EVALUATED_MESSAGE.format(file=rel, line=int(line)))


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

    The unit word is not decoration: without it ``errors: 12`` reads as
    "12 errors" rather than "an error on line 12".
    """
    body = ", ".join(f"{s}" if s == e else f"{s}-{e}" for s, e in spans)
    single = len(spans) == 1 and spans[0][0] == spans[0][1]
    return f"{'line' if single else 'lines'} {body}"


def _snippet(text: str) -> str:
    """First line of a running command's text, truncated — the running row's
    nested lines name the command by it."""
    first = text.split("\n", 1)[0].strip()
    return (first[:60] + "...") if len(first) > 60 else first


def _count_bits(fs: FileSnapshot) -> str:
    parts = []
    if fs.error_count:
        parts.append(f"{fs.error_count} error" + ("s" if fs.error_count != 1 else ""))
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
        if fs.sorry:
            rows.append(f"  sorry: {_fmt_spans(fs.sorry)}")
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
    threshold, or a failure. Nothing else justifies telling the agent to poll —
    a sorry in particular is listed, not chased."""
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
    if view.unprocessed_theories:
        blocks.append(unprocessed_theories_sentence(view.unprocessed_theories))
    if call_to_action and _worth_watching(view):
        blocks.append(CHECK_PROGRESS_CALL)
    return "\n\n".join(blocks)
