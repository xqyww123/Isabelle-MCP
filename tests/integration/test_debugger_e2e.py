"""End-to-end integration tests of the debugger TOOLS (Phase E).

The probes (test_debugger_probes.py) pin the prover assumptions through the
client's thin wrappers; these tests drive the tool bodies themselves —
debugger.py and evaluation.py — through the full taught workflows of
DEBUGGER_DESIGN.md §2.2 and §6, against a real prover. Sentences are
asserted via the module constants, so a wording change stays one conscious
edit.

Same selection as the probes:

    PATH=contrib/Isabelle2025-2/bin:$PATH pytest tests/integration -m integration
"""
import asyncio
import os
import shutil

import pytest

from isabelle_mcp import debugger
from isabelle_mcp import evaluation as ev
from isabelle_mcp.debugger import DebuggerRegistry
from isabelle_mcp.evaluation import cancel_evaluation, evaluate_to
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.utils import IsabelleToolError

pytestmark = pytest.mark.integration

if shutil.which("isabelle") is None:
    pytest.skip("isabelle not on PATH", allow_module_level=True)

from tests.integration.test_debugger_probes import (  # noqa: E402
    CALLER,
    DEFINER_END,
    RUNAWAY,
    VAL_TOTAL,
    VAL_XS,
    _edit_on_disk,
    _evaluate_through,
    _wait_settled,
    prover,  # re-exported fixture
)

__all__ = ["prover"]


@pytest.fixture
async def dbg(prover):
    """The probes' prover, with a fresh breakpoint registry per test (the
    registry is module-global; hit numbering must start at h1 here)."""
    debugger.registry = DebuggerRegistry()
    yield prover


async def _hit_led_evaluate(client, path, line):
    """evaluate_to expected to exit hit-led. The destination must be the
    CALLER's own line: the parked command keeps that line unprocessed, so
    the frontier decision cannot fire first (with a farther destination
    PIDE can mark the destination line reached while the parked command
    sits earlier in the prefix — then the wait ends "arrived, not quiet"
    before the hit lands, and the hit surfaces per §6.2 instead: notice +
    paused section). Generous poll bound so the exit is the hit, never the
    poll timer."""
    ev.EVAL_POLL_INTERVAL = 60.0
    view = await evaluate_to(client, path, line)
    assert view.status == "in_progress", view.status
    assert view.message.startswith("Breakpoint hit: "), view.message
    return view


@pytest.mark.asyncio
async def test_full_workflow_set_hit_locals_eval_continue(dbg):
    """§2.2 motion 1 through the tools: set → run → hit report → implicit
    locals → locals/eval tools → continue → the run completes."""
    client, path = dbg
    assert await _evaluate_through(client, path, DEFINER_END)

    out = await debugger.set_breakpoint(client, path, VAL_TOTAL, None)
    assert out.startswith(
        "Breakpoint set and armed: DebugProbe.thy:13 before ‹val total"), out

    view = await _hit_led_evaluate(client, path, CALLER)
    msg = view.message
    assert msg.startswith(
        "Breakpoint hit: DebugProbe.thy:13 before ‹val total"), msg
    assert "Hit id: h1" in msg
    assert debugger.CALL_STACK_HEADER in msg
    assert debugger.LOCALS_HEADER in msg
    assert "xs =" in msg and "shift =" in msg   # implicit frame-0 locals
    assert debugger.HIT_REPORT_TAIL in msg

    # The paused section leads evaluation_status while the hit lives.
    paused = debugger.paused_section(client)
    assert paused is not None
    assert paused.startswith(debugger.PAUSED_LEAD_ONE)
    assert paused.rstrip().endswith(debugger.PAUSED_TAIL)

    # A second evaluate_to is refused, leading with the hit.
    with pytest.raises(IsabelleToolError) as exc:
        await evaluate_to(client, path, CALLER)
    assert str(exc.value).startswith(
        "Evaluation is paused at a breakpoint — hit id h1 at "
        "DebugProbe.thy:13 before ‹val total"), str(exc.value)

    out = await debugger.locals_at_breakpoint(client, None, 0, 60.0)
    assert "xs =" in out and "shift =" in out, out
    # Frame 1 is the calling ML block: nothing is bound there at hit time,
    # and the empty listing is answered by the dedicated sentence.
    out = await debugger.locals_at_breakpoint(client, None, 1, 60.0)
    assert out == debugger.LOCALS_NONE.format(frame=1), out
    out = await debugger.eval_at_breakpoint(
        client, "List.length xs", None, 0, 60.0)
    assert "val it = 3" in out, out

    out = await debugger.continue_breakpoint(client, "h1")
    assert out.startswith("Resumed hit id h1 (thread "), out
    assert await _wait_settled(client)


@pytest.mark.asyncio
async def test_step_modes_end_to_end(dbg):
    """§4.12 through the tool: step stops again inside the block; the other
    modes end in one of the two normal outcomes."""
    client, path = dbg
    assert await _evaluate_through(client, path, DEFINER_END)
    await debugger.set_breakpoint(client, path, VAL_XS, None)
    await _hit_led_evaluate(client, path, CALLER)

    out = await debugger.step_at_breakpoint(client, "step", None)
    assert out.startswith("Hit id h1 stopped again.\n"), out
    assert "Hit id: h1" in out   # the refreshed hit block follows

    for mode in ("step_over", "step_out"):
        out = await debugger.step_at_breakpoint(client, mode, None)
        assert out.startswith("Hit id h1 stopped again.") \
            or out.startswith("The thread resumed and did not stop again"), \
            (mode, out)
        if not debugger.registry.hits:
            break

    if debugger.registry.hits:
        await debugger.continue_breakpoint(client, None)
    assert await _wait_settled(client)


@pytest.mark.asyncio
async def test_enable_disable_all_idempotence(dbg):
    """§4.6/4.7: both tools are safely repeatable; the listing tells the
    truth after every step."""
    client, path = dbg
    assert await _evaluate_through(client, path, DEFINER_END)
    await debugger.set_breakpoint(client, path, VAL_XS, None)

    expected_off = debugger.DISABLED_RESULT.format(
        armed=1, breakpoints="breakpoint", pending=0, entries="entries")
    assert await debugger.disable_all_breakpoints(client, None) == expected_off
    assert await debugger.disable_all_breakpoints(client, None) == expected_off
    listing = debugger.list_breakpoints(client, None)
    assert ", disabled, armed" in listing, listing

    out = await debugger.enable_all_breakpoints(client, None)
    assert out.startswith("Armed 1:"), out
    out = await debugger.enable_all_breakpoints(client, None)
    assert out.startswith("Armed 1:"), out
    listing = debugger.list_breakpoints(client, None)
    assert ", enabled, armed" in listing, listing
    assert len(debugger.registry.entries) == 1   # never a twin


@pytest.mark.asyncio
async def test_motion3_fence_then_rearm_after_upstream_edit(dbg):
    """§2.2 motion 3 with the §5 fence: an edit in the defining block kills
    the serial; the next run warns (result line and notice); re-evaluating
    and enable_all re-arms; the run after that hits."""
    client, path = dbg
    assert await _evaluate_through(client, path, DEFINER_END)
    await debugger.set_breakpoint(client, path, VAL_TOTAL, None)

    await _edit_on_disk(client, path, "val shift = n + 1;",
                        "val shift  = n + 1;")
    # The didChange marked the file dirty (the production wiring in
    # sync_dirty_files); in production the next tool call's middleware runs
    # the reconciliation pass — invoke it directly here, as the middleware
    # would. The listing-verified demotion is what arms the fence: PIDE
    # re-processes the small block in the background within seconds, so
    # bullet 2 (position no longer processed) cannot be relied on.
    await debugger.reconcile_dirty(client)
    [entry] = debugger.registry.entries
    assert entry.state == "pending", entry
    assert entry.reason == debugger.TAG_NOT_EVALUATED, entry
    notices = debugger.registry.drain_notices() or ""
    assert "no longer works (not evaluated yet)" in notices, notices

    view = await evaluate_to(client, path, DEFINER_END)
    assert debugger.FENCE_WARNING_ONE in view.message, view.message
    notices = debugger.registry.drain_notices() or ""
    assert debugger.FENCE_WARNING_ONE in notices, notices
    assert await _wait_settled(client)

    out = await debugger.enable_all_breakpoints(client, None)
    assert out.startswith("Armed 1:"), out

    view = await _hit_led_evaluate(client, path, CALLER)
    assert "DebugProbe.thy:13" in view.message, view.message
    await debugger.continue_breakpoint(client, None)
    assert await _wait_settled(client)


@pytest.mark.asyncio
async def test_cancel_while_stopped_sweeps_and_demotes(dbg):
    """§6.4: cancellation with a live hit — the swept line in the result,
    the silent attributed retirement, the wire-free demote-all."""
    client, path = dbg
    assert await _evaluate_through(client, path, DEFINER_END)
    await debugger.set_breakpoint(client, path, VAL_TOTAL, None)
    await _hit_led_evaluate(client, path, CALLER)
    debugger.registry.drain_notices()   # isolate the sweep's notices

    view = await cancel_evaluation(client)
    assert view.status == "cancelled"
    assert view.message == (
        ev.CANCELLED_MESSAGE + "\n" + debugger.HITS_SWEPT_ONE), view.message

    with pytest.raises(IsabelleToolError) as exc:
        debugger.registry.resolve_hit("h1")
    assert debugger.ENDED_CANCELLED in str(exc.value)

    listing = debugger.list_breakpoints(client, None)
    assert "pending (not evaluated yet)" in listing, listing
    notices = debugger.registry.drain_notices() or ""
    assert "no longer works (not evaluated yet)" in notices, notices
    assert "ended" not in notices   # the retirement was silent (attributed)


@pytest.mark.asyncio
async def test_eval_timeout_busy_and_abort(dbg):
    """§4.9's backstop timeout and busy fence, then §4.13's abort — through
    the tools, on one hit."""
    client, path = dbg
    assert await _evaluate_through(client, path, DEFINER_END)
    await debugger.set_breakpoint(client, path, VAL_XS, None)
    await _hit_led_evaluate(client, path, CALLER)

    with pytest.raises(IsabelleToolError) as exc:
        await debugger.abort_eval_at_breakpoint(client, None)
    assert str(exc.value) == debugger.ABORT_NOTHING

    # A shielded 45 s sleep defeats the 3 s prover deadline; the Scala
    # backstop answers `timeout` at 3+30 s (probe R2's manufacture).
    with pytest.raises(IsabelleToolError) as exc:
        await debugger.eval_at_breakpoint(
            client,
            "Thread_Attributes.with_attributes Thread_Attributes.no_interrupts "
            "(fn _ => OS.Process.sleep (Time.fromSeconds 45))",
            None, 0, 3.0)
    assert str(exc.value) == debugger.EVAL_BACKSTOP_TIMEOUT.format(seconds=3)

    # While the debt is owed, the hit takes no new evaluations.
    with pytest.raises(IsabelleToolError) as exc:
        await debugger.eval_at_breakpoint(client, "1", None, 0, 5.0)
    assert str(exc.value) == debugger.EVAL_BUSY

    # The debt clears when the sleep ends; the hit answers again.
    answered = None
    for _ in range(30):
        try:
            answered = await debugger.eval_at_breakpoint(
                client, "2 + 2", None, 0, 10.0)
            break
        except IsabelleToolError as e:
            assert str(e) == debugger.EVAL_BUSY, e
            await asyncio.sleep(5)
    assert answered is not None and "val it = 4" in answered, answered

    # Abort an interruptible runaway: accepted, confirmed by outcome, and
    # the thread stays at the breakpoint, still debuggable.
    runaway = asyncio.create_task(debugger.eval_at_breakpoint(
        client, RUNAWAY, None, 0, 120.0))
    await asyncio.sleep(2)   # let it reach the prover
    out = await debugger.abort_eval_at_breakpoint(client, None)
    assert out == debugger.ABORT_OK
    try:
        await runaway   # the aborted evaluation settles: its reply may be
    except IsabelleToolError:   # an error or a rendered interrupt message
        pass
    answered = await debugger.eval_at_breakpoint(client, "2 + 2", None, 0, 30.0)
    assert "val it = 4" in answered, answered

    await debugger.continue_breakpoint(client, None)
    assert await _wait_settled(client)


# ── Two hits at once ───────────────────────────────────────────────────

FORK_THEORY = r'''theory DebugFork
imports Main
begin

ML \<open>
fun fork_target (n: int) =
  let
    val xs = map (fn i => i + n) (1 upto 3);
    val total = List.foldl (fn (i, acc) => acc + i) 0 xs;
  in total end;
\<close>

ML \<open>
val f1 = Future.fork (fn () => fork_target 1);
val f2 = Future.fork (fn () => fork_target 2);
val _ = (Future.join f1, Future.join f2);
\<close>

end
'''
FORK_TOTAL = 9
FORK_DEFINER_END = 11
FORK_JOIN = 16


@pytest.fixture
async def fork_prover(tmp_path):
    ev.EVAL_POLL_INTERVAL = 4.0
    path = os.path.join(str(tmp_path), "DebugFork.thy")
    with open(path, "w") as f:
        f.write(FORK_THEORY)
    ev.evaluation_state = ev.EvaluationState()
    debugger.registry = DebuggerRegistry()
    client = IsabelleLSPClient(
        logic="HOL",
        project_root=str(tmp_path),
        debug=True,
        extra_args=["-o", "editor_tracing_messages=0"],
    )
    await client.start()
    try:
        yield client, path
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_two_hits_at_once(fork_prover):
    """Parallel evaluation stops two threads at one breakpoint: both are
    first-class hits; single-hit addressing is refused with the row list;
    continue-all resumes both."""
    client, path = fork_prover
    assert await _evaluate_through(client, path, FORK_DEFINER_END)
    await debugger.set_breakpoint(client, path, FORK_TOTAL, None)

    ev.EVAL_POLL_INTERVAL = 60.0
    view = await evaluate_to(client, path, FORK_JOIN)
    assert view.status == "in_progress"
    assert view.message.startswith("Breakpoint hit: "), view.message

    # Both forks park (the report may have caught only the first).
    ok = await client.wait_debugger_event(
        lambda c: sum(1 for s in c.debugger_threads.values() if s) >= 2,
        timeout=90.0)
    assert ok, f"second fork never parked: {client.debugger_threads}"

    state = debugger.debug_state(client)
    assert state.startswith("2 hits are live."), state
    assert state.count("Hit id:") == 2, state

    with pytest.raises(IsabelleToolError) as exc:
        debugger.registry.resolve_hit(None)
    msg = str(exc.value)
    assert msg.startswith(debugger.SEVERAL_HITS)
    assert "hit id h1 — thread " in msg and "hit id h2 — thread " in msg

    out = await debugger.continue_breakpoint(client, None)
    assert out.startswith("Resumed 2 hits:"), out
    assert await _wait_settled(client)
