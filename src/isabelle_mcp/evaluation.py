"""Async evaluation lifecycle for Isabelle theories (v0.3.0).

Separates *evaluation* (telling Isabelle what to process) from *querying*
(reading hover/goal/diagnostic results).  Three MCP tools manage
evaluation; query tools call :func:`check_evaluation_guard` to ensure
the target region has been processed.

v0.3.0 leverages PIDE/theory_status for dependency-aware completion and
PIDE/cancel_execution for global cancellation.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field

from isabelle_mcp.lsp_client import IsabelleLSPClient, _stat_sig, _stat_sigs
from isabelle_mcp.models import (
    EvaluationView,
    FileSnapshot,
    RunningCommand,
    TheoryStatus,
)
from isabelle_mcp.processing import _grace_remaining, clip_line_range, note_edit_sent
from isabelle_mcp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPLine,
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


def _complete_message(
    dest: int,
    running_commands: list[RunningCommand],
    files: list[FileSnapshot],
) -> str:
    """Message for a reached destination, honest about leftover running/failed work.

    The destination line is reached, but the command at/after it may still be
    running (e.g. a stuck tactic) and earlier commands may have failed (errors do
    not halt checking). Surface those counts instead of a bare "complete" so the
    agent knows whether to keep watching or cancel.
    """
    n_running = len(running_commands)
    n_failed = sum(fs.error_count for fs in files)
    if n_running == 0 and n_failed == 0:
        return f"Evaluation complete, arrived at line {dest}."
    return (
        f"Evaluation arrived at line {dest} with {n_running} statement(s) still "
        f"running and {n_failed} statement(s) failed."
    )


def _in_progress_message(running_commands: list[RunningCommand]) -> str:
    if running_commands:
        parts = []
        for cmd in running_commands[:5]:
            text_preview = cmd.text[:60] + ("..." if len(cmd.text) > 60 else "")
            parts.append(
                f"  {cmd.file_path}:{cmd.start_line}"
                f" ({cmd.elapsed_seconds:.0f}s) {text_preview}"
            )
        header = f"{len(running_commands)} command(s) running"
        return f"{header}. Call evaluation_status to check progress.\n" + "\n".join(parts)
    return "Evaluation in progress. Call evaluation_status to check progress."


# ---------------------------------------------------------------------------
# EvaluationState
# ---------------------------------------------------------------------------

@dataclass
class EvaluationState:
    active: bool = False
    file_path: str = ""
    destination_line: MCPLine = MCPLine(1)
    auto_opened_files: set[str] = field(default_factory=set)

    def start(self, file_path: str, destination_line: MCPLine) -> None:
        self.active = True
        self.file_path = file_path
        self.destination_line = destination_line
        self.auto_opened_files = set()

    def complete(self) -> None:
        self.active = False

    def cancel(self) -> None:
        self.active = False


evaluation_state = EvaluationState()
# Serializes the short evaluation-state transitions (evaluate_to start /
# cancel / guard) and the document content/version mutations and caret-target
# resolution that must stay atomic with them. Held only for those transitions —
# NOT for the whole evaluation. The event-driven file-sync push and the tool-call
# stat backstop also take it so a concurrent sync cannot interleave with a start/stop.
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
        if not t.ok and t.node_name and t.node_name not in evaluation_state.auto_opened_files:
            if client.open_documents.get(t.node_name) is None:
                try:
                    await client.open_document(t.node_name)
                    evaluation_state.auto_opened_files.add(t.node_name)
                except OSError:
                    pass

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


def _relevant_files(
    client: IsabelleLSPClient, target: str, auto_opened: set[str],
) -> list[str]:
    """Target ∪ auto-opened deps ∪ open docs with any problem/running marker."""
    files: list[str] = [target]
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
    for path in state.auto_opened_files:
        try:
            await client.close_document(path)
        except Exception:
            logger.warning("Failed to close auto-opened file %s", path, exc_info=True)
    state.auto_opened_files.clear()


# ---------------------------------------------------------------------------
# Wait loop
# ---------------------------------------------------------------------------

async def _evaluation_wait_loop(
    client: IsabelleLSPClient,
    file_path: str,
    dest_line: MCPLine,
    state: EvaluationState,
    timeout: float,
) -> tuple[str, list[TheoryStatus], list[RunningCommand]]:
    deadline = time.monotonic() + timeout
    last_restat = time.monotonic()
    while True:
        if not state.active:
            return "cancelled", [], []
        now = time.monotonic()
        if now - last_restat >= _LONG_EVAL_RESTAT_INTERVAL:
            last_restat = now
            # Push any edit that landed mid-evaluation; PIDE re-checks incrementally.
            await resync_changed_open_documents_locked(client)
        theories, running_commands = await _build_status_snapshot(client, state)
        # Decide the instant the frontier reaches dest: prefix quiet → complete;
        # otherwise return in_progress NOW (no grace). Trailing forks are reported
        # (running/pending lines), not waited on — the caller polls to convergence.
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

async def evaluate_to(
    client: IsabelleLSPClient,
    file_path: str,
    line: int,
    after_text: str | None = None,
) -> EvaluationView:
    async with _evaluation_state_lock:
        if evaluation_state.active:
            raise IsabelleToolError(
                "An evaluation is already in progress. "
                "Call cancel_evaluation to cancel, "
                "or evaluation_status to check progress.",
            )

        await client.open_document(file_path)
        heap_warning = client.heap_warning(file_path)
        doc = client.open_documents.get(file_path)
        total_lines = (doc.content.count("\n") + 1) if doc else 1
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

        evaluation_state.start(file_path, dest_line)

    try:
        # No freshness invalidation here: every edit-send path calls
        # note_edit_sent (didOpen/didChange/dep change), and a caret-only move
        # cannot make stale decorations claim "processed" for work that isn't
        # (see note_edit_sent's docstring).
        await client.set_caret(file_path, dest_line.to_lsp(), lsp_char)
        status, theories, running_commands = await _evaluation_wait_loop(
            client, file_path, dest_line, evaluation_state,
            HEAP_POLL_INTERVAL if heap_warning else EVAL_POLL_INTERVAL,
        )
    except Exception:
        await _cleanup_auto_opened(client, evaluation_state)
        evaluation_state.cancel()
        raise

    if heap_warning and status != "complete" and evaluation_state.active:
        # The miss may be only the post-edit grace gate (a concurrent edit
        # re-armed it inside the short heap budget) — an unmodified precompiled
        # file replays instantly once the gate opens. Re-check past the gate
        # before declaring the file divergent and telling the agent not to retry.
        grace = _grace_remaining()
        if grace > 0:
            status, theories, running_commands = await _evaluation_wait_loop(
                client, file_path, dest_line, evaluation_state, grace + 0.2,
            )

    dest = int(dest_line)
    auto_opened = set(evaluation_state.auto_opened_files)
    # Build the snapshot BEFORE cleanup closes the auto-opened deps (which would
    # drop their decoration trackers).
    files = _snapshot_files(client, file_path, theories, auto_opened, dest_line)
    if heap_warning and status != "complete":
        # The file differs from its precompiled copy, so PIDE will never
        # reprocess it — abandon the evaluation instead of leaving it pending.
        await _cleanup_auto_opened(client, evaluation_state)
        evaluation_state.cancel()
        status = "cancelled"
        message = (
            "Evaluation abandoned: the file differs from its precompiled copy "
            "and Isabelle will never reprocess it. Do not retry or poll."
        )
    else:
        message = (
            _complete_message(dest, running_commands, files)
            if status == "complete"
            else _in_progress_message(running_commands)
        )
        if status == "complete":
            await _cleanup_auto_opened(client, evaluation_state)
            evaluation_state.complete()
    return EvaluationView(
        status=status,
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


async def evaluation_status(
    client: IsabelleLSPClient,
) -> EvaluationView:
    if _no_pending_work(client):
        return _no_evaluation_view()

    theories, running_commands = await _build_status_snapshot(
        client, evaluation_state,
    )
    dest = int(evaluation_state.destination_line)
    target = evaluation_state.file_path
    auto_opened = set(evaluation_state.auto_opened_files)

    complete = _is_evaluation_complete(
        target, evaluation_state.destination_line, client, theories,
    )
    files = _snapshot_files(
        client, target, theories, auto_opened, evaluation_state.destination_line,
    )
    # Only the active evaluation owns the complete→cleanup transition; once
    # ``active`` is False the cleanup already ran and we are merely surfacing a
    # lingering fork, which must stay visible (not collapse back to "complete").
    if evaluation_state.active and complete:
        await _cleanup_auto_opened(client, evaluation_state)
        evaluation_state.complete()
        return EvaluationView(
            status="complete",
            destination_line=dest,
            message=_complete_message(dest, running_commands, files),
            files=files,
            running_commands=running_commands,
        )

    return EvaluationView(
        status="in_progress",
        destination_line=dest,
        message=_in_progress_message(running_commands),
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
    async with _evaluation_state_lock:
        if _no_pending_work(client):
            return _no_evaluation_view()

        # When the active evaluation already completed but a fork is still
        # running, ``file_path`` may be the stale (now-closed) target; fall back
        # to whichever file still holds a running command so force_interrupt's
        # doc lookup resolves. (cancel_execution itself is global.)
        running = client.get_all_running_commands()
        fp = evaluation_state.file_path or (running[0].file_path if running else "")
        dest = int(evaluation_state.destination_line)
        await client.force_interrupt(fp)
        await _cleanup_auto_opened(client, evaluation_state)
        evaluation_state.cancel()
        return EvaluationView(
            status="cancelled",
            destination_line=dest,
            message="Evaluation cancelled.",
        )


async def check_evaluation_guard(
    client: IsabelleLSPClient,
    file_path: str,
    line: MCPLine,
) -> "EvaluationView | str | None":
    """Ensure *line* has been evaluated; raise, warn, or auto-start evaluation.

    (Auto-starts *evaluation of unevaluated lines* on an already-running session —
    it does not start the prover; the session must first be launched via
    ``isabelle_launch``.)

    Returns:
      - ``None``: line is fully processed, caller can proceed.
      - ``str``: line is running (forked proof), caller can proceed but
        should set ``result.note`` to this warning string.
      - ``EvaluationView``: auto-evaluation started but didn't complete; the caller
        renders it (``format_evaluation_result``) and raises it.
    Raises :class:`IsabelleToolError` if another evaluation is running.
    """
    async with _evaluation_state_lock:
        if evaluation_state.active:
            raise IsabelleToolError(
                "Evaluation in progress. "
                "Call evaluation_status to check progress.",
            )

        tracker = client.get_processing_tracker(file_path)
        if tracker is not None and tracker.line_reached(line.to_lsp()):
            if tracker.line_running(line.to_lsp()):
                return "This line is still being executed (forked proof). Output may be incomplete."
            return None

    result = await evaluate_to(client, file_path, int(line))
    if result.status == "complete":
        return None
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _relativize(path: str, root: str | None) -> str:
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
    return ", ".join(f"{s}" if s == e else f"{s}-{e}" for s, e in spans)


def _count_bits(fs: FileSnapshot) -> str:
    parts = []
    if fs.error_count:
        parts.append(f"{fs.error_count} error" + ("s" if fs.error_count != 1 else ""))
    if fs.warning_count:
        parts.append(f"{fs.warning_count} warning" + ("s" if fs.warning_count != 1 else ""))
    if fs.running_count:
        parts.append(f"{fs.running_count} running")
    return ", ".join(parts)


def _format_file_snapshot(fs: FileSnapshot, root: str | None) -> str:
    name = _relativize(fs.file_path, root)
    if fs.lined:
        rows = []
        if fs.errors:
            rows.append(f"  errors: {_fmt_spans(fs.errors)}")
        if fs.warnings:
            rows.append(f"  warnings: {_fmt_spans(fs.warnings)}")
        if fs.running:
            rows.append(f"  running: {_fmt_spans(fs.running)}")
        if fs.pending:
            rows.append(f"  pending: {_fmt_spans(fs.pending)}")
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


def format_evaluation_result(view: EvaluationView, root: str | None = None) -> str:
    """Render an EvaluationView as the agent-facing plain-text snapshot."""
    if view.status == "no_evaluation":
        return view.message or "No evaluation in progress."
    buf = io.StringIO()
    if view.heap_warning:
        buf.write("⚠️ " + view.heap_warning + "\n\n")
    buf.write(view.message.rstrip("\n") if view.message else view.status)
    for fs in view.files:
        buf.write("\n\n")
        buf.write(_format_file_snapshot(fs, root))
    return buf.getvalue()
