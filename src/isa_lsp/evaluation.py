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
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field

from isa_lsp.lsp_client import IsabelleLSPClient
from isa_lsp.models import (
    DiagnosticMessage,
    EvaluationResult,
    RunningCommand,
    TheoryStatus,
)
from isa_lsp.utils import (
    IsabelleToolError,
    LSPCharacter,
    LSPLine,
    MCPLine,
    lsp_to_mcp_position,
    severity_int_to_string,
)

logger = logging.getLogger(__name__)

EVAL_POLL_INTERVAL: float = float(
    os.environ.get("ISA_LSP_EVAL_POLL_INTERVAL", "10"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_line(value: int, total_lines: int) -> MCPLine:
    if value < 0:
        return MCPLine(max(1, total_lines + 1 + value))
    return MCPLine(value)


def _parse_diagnostic(diag: dict) -> DiagnosticMessage:
    start = diag.get("range", {}).get("start", {})
    end = diag.get("range", {}).get("end", {})
    start_line, start_col = lsp_to_mcp_position(
        LSPLine(start.get("line", 0)), LSPCharacter(start.get("character", 0)),
    )
    end_line, end_col = lsp_to_mcp_position(
        LSPLine(end.get("line", 0)), LSPCharacter(end.get("character", 0)),
    )
    return DiagnosticMessage(
        severity=severity_int_to_string(diag.get("severity", 1)),
        message=diag.get("message", ""),
        line=start_line, column=start_col,
        end_line=end_line, end_column=end_col,
    )


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


def _is_evaluation_complete(
    file_path: str,
    dest_line: MCPLine,
    client: IsabelleLSPClient,
    theories: list[TheoryStatus],
) -> bool:
    tracker = client.get_processing_tracker(file_path)
    if not tracker or not tracker.line_reached(dest_line.to_lsp()):
        return False
    target_name = _find_theory_name(file_path, theories)
    if target_name is None:
        return False
    deps = _get_recursive_dependencies(target_name, theories)
    theory_map = {t.theory_name: t for t in theories}
    return all(_dependency_done(theory_map[d]) for d in deps if d in theory_map)


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
    reported_errors: set[tuple[str, int, str]] = field(default_factory=set)
    auto_opened_files: set[str] = field(default_factory=set)

    def start(self, file_path: str, destination_line: MCPLine) -> None:
        self.active = True
        self.file_path = file_path
        self.destination_line = destination_line
        self.reported_errors = set()
        self.auto_opened_files = set()

    def complete(self) -> None:
        self.active = False

    def cancel(self) -> None:
        self.active = False


evaluation_state = EvaluationState()
_evaluation_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------

async def _build_status_snapshot(
    client: IsabelleLSPClient,
    evaluation_state: EvaluationState,
) -> tuple[list[TheoryStatus], list[RunningCommand], list[DiagnosticMessage]]:
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

    new_errors: list[DiagnosticMessage] = []
    for path in list(client.open_documents):
        for diag in client.get_cached_diagnostics(path):
            sev = diag.get("severity", 4)
            if sev > 2:
                continue
            msg = diag.get("message", "")
            line = diag.get("range", {}).get("start", {}).get("line", 0)
            key = (path, line, msg)
            if key in evaluation_state.reported_errors:
                continue
            evaluation_state.reported_errors.add(key)
            new_errors.append(_parse_diagnostic(diag))

    return theories, running_commands, new_errors


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
) -> tuple[str, list[TheoryStatus], list[RunningCommand], list[DiagnosticMessage]]:
    deadline = time.monotonic() + timeout
    all_errors: list[DiagnosticMessage] = []
    while True:
        if not state.active:
            return "cancelled", [], [], all_errors
        theories, running_commands, new_errors = await _build_status_snapshot(client, state)
        all_errors.extend(new_errors)
        if _is_evaluation_complete(file_path, dest_line, client, theories):
            return "complete", theories, running_commands, all_errors
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "in_progress", theories, running_commands, all_errors
        tracker = client.get_processing_tracker(file_path)
        if tracker:
            await tracker.wait_until_processed_bounded(
                LSPLine(0), dest_line.to_lsp(),
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
) -> EvaluationResult:
    async with _evaluation_lock:
        if evaluation_state.active:
            raise IsabelleToolError(
                "An evaluation is already in progress. "
                "Call cancel_evaluation to cancel, "
                "or evaluation_status to check progress.",
            )

        await client.open_document(file_path)
        doc = client.open_documents.get(file_path)
        total_lines = (doc.content.count("\n") + 1) if doc else 1
        mcp_line = _resolve_line(line, total_lines)
        if mcp_line < 1:
            raise IsabelleToolError(f"line must be >= 1, got {mcp_line}")

        evaluation_state.start(file_path, mcp_line)

    try:
        tracker = client.get_processing_tracker(file_path)
        if tracker is not None:
            tracker.require_fresh_update()
        await client.set_caret(file_path, mcp_line.to_lsp())
        status, theories, running_commands, errors = await _evaluation_wait_loop(
            client, file_path, mcp_line, evaluation_state, EVAL_POLL_INTERVAL,
        )
    except Exception:
        await _cleanup_auto_opened(client, evaluation_state)
        evaluation_state.cancel()
        raise

    dest = int(mcp_line)
    if status == "complete":
        await _cleanup_auto_opened(client, evaluation_state)
        evaluation_state.complete()
        return EvaluationResult(
            status="complete",
            errors=errors,
            theories=theories,
            running_commands=running_commands,
            destination_line=dest,
            message=f"Evaluation complete, arrived at line {dest}.",
        )

    return EvaluationResult(
        status=status,
        errors=errors,
        theories=theories,
        running_commands=running_commands,
        destination_line=dest,
        message=_in_progress_message(running_commands),
    )


async def evaluation_status(
    client: IsabelleLSPClient,
) -> EvaluationResult:
    if not evaluation_state.active:
        return EvaluationResult(
            status="no_evaluation",
            message="No evaluation in progress.",
        )

    theories, running_commands, errors = await _build_status_snapshot(
        client, evaluation_state,
    )
    dest = int(evaluation_state.destination_line)

    if _is_evaluation_complete(
        evaluation_state.file_path,
        evaluation_state.destination_line,
        client, theories,
    ):
        await _cleanup_auto_opened(client, evaluation_state)
        evaluation_state.complete()
        return EvaluationResult(
            status="complete",
            errors=errors,
            theories=theories,
            running_commands=running_commands,
            destination_line=dest,
            message=f"Evaluation complete, arrived at line {dest}.",
        )

    return EvaluationResult(
        status="in_progress",
        errors=errors,
        theories=theories,
        running_commands=running_commands,
        destination_line=dest,
        message=_in_progress_message(running_commands),
    )


async def cancel_evaluation(
    client: IsabelleLSPClient,
) -> EvaluationResult:
    async with _evaluation_lock:
        if not evaluation_state.active:
            return EvaluationResult(
                status="no_evaluation",
                message="No evaluation in progress.",
            )

        fp = evaluation_state.file_path
        dest = int(evaluation_state.destination_line)
        await client.force_interrupt(fp)

        errors: list[DiagnosticMessage] = []
        for diag in client.get_cached_diagnostics(fp):
            sev = diag.get("severity", 4)
            if sev > 2:
                continue
            errors.append(_parse_diagnostic(diag))

        await _cleanup_auto_opened(client, evaluation_state)
        evaluation_state.cancel()
        return EvaluationResult(
            status="cancelled",
            errors=errors,
            destination_line=dest,
            message="Evaluation cancelled.",
        )


async def check_evaluation_guard(
    client: IsabelleLSPClient,
    file_path: str,
    line: MCPLine,
) -> EvaluationResult | str | None:
    """Ensure *line* has been evaluated; raise, warn, or auto-start.

    Returns:
      - ``None``: line is fully processed, caller can proceed.
      - ``str``: line is running (forked proof), caller can proceed but
        should set ``result.note`` to this warning string.
      - ``EvaluationResult``: auto-evaluation started but didn't complete.
    Raises :class:`IsabelleToolError` if another evaluation is running.
    """
    async with _evaluation_lock:
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
