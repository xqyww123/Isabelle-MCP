"""Z series (ISABELLE_MCP_CANCELLATION_REDESIGN_PLAN.md section 6): the cancellation
redesign against a live prover.

Iron rule of the series: every step ends with an evaluate_to of the same file, so
a focus trap cannot fake a result; the positive control (a witness that grows when
a command really runs) must move, or the whole run is void.

The device: a theory whose "slow" command is a time-bounded ALLOCATING loop -- it
is interruptible (Poly/ML delivers interrupts at allocation) and, left alone, it
finishes on its own, so a re-run after a cancel can complete. Witnesses are
appended to a file by the commands themselves (the Z0/Z1 method): a command that
ran leaves its letter.

Run with:  pytest tests/integration/test_cancel_z.py -m integration -s
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time

import pytest

from isabelle_mcp import evaluation as ev
from isabelle_mcp.evaluation import cancel_evaluation, evaluate_to, evaluation_status
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.processing import NOT_EVALUATED, PROCESSED, RUNNING
from isabelle_mcp.tools.goal import goal
from isabelle_mcp.utils import IsabelleCatastrophe, MCPLine

pytestmark = pytest.mark.integration

SLOW_SECONDS = 20.0

# Line numbers of the device (1-based).
L_A, L_SLOW, L_C, L_LEMMA, L_END = 5, 6, 7, 8, 9


def _theory(name: str, witness: str, slow_seconds: float = SLOW_SECONDS,
            imports: str = "Main") -> str:
    """The device. Every marked command appends its letter to *witness*."""
    w = witness.replace("\\", "\\\\")
    return (
        f"theory {name}\n"
        f"imports {imports}\n"
        "begin\n"
        f'ML ‹fun z_mark s = File.append (Path.explode "{w}") s›\n'
        'ML ‹z_mark "A"›\n'
        "ML ‹let val t0 = Time.now () "
        f"in while Time.< (Time.- (Time.now (), t0), seconds {slow_seconds}) do "
        'ignore (List.tabulate (1000, fn i => i)) end; z_mark "S"›\n'
        'ML ‹z_mark "C"›\n'
        'lemma z_lemma: "1 + 1 = (2::nat)" by simp\n'
        "end\n"
    )


def _read(witness: str) -> str:
    try:
        with open(witness) as f:
            return f.read()
    except FileNotFoundError:
        return ""


@pytest.fixture
async def prover(tmp_path):
    ev.EVAL_POLL_INTERVAL = 2.0
    ev.evaluation_state = ev.EvaluationState()
    client = IsabelleLSPClient(
        logic="HOL",
        project_root=str(tmp_path),
        extra_args=["-o", "editor_tracing_messages=0"],
    )
    await client.start()
    try:
        yield client, str(tmp_path)
    finally:
        if client.process is not None:
            await client.teardown("test over")


async def _wait_state(client, path, line, wanted, timeout=30.0):
    """Poll position_state until it reads *wanted* (the decoration cache has a
    freshness gate; a just-sent edit makes it read UNKNOWN for a moment)."""
    deadline = time.monotonic() + timeout
    state = None
    while time.monotonic() < deadline:
        await evaluation_status(client)          # refreshes the trackers
        state = ev.position_state(client, path, MCPLine(line))
        if state == wanted:
            return state
        await asyncio.sleep(1)
    return state


async def _run_to_completion(client, path, line, timeout=120.0):
    view = await evaluate_to(client, path, line)
    deadline = time.monotonic() + timeout
    while view.status not in ("complete", "no_evaluation") and time.monotonic() < deadline:
        await asyncio.sleep(2)
        view = await evaluation_status(client)
    return view


def _descendants(pid: int) -> list[tuple[int, str]]:
    out = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="], capture_output=True, text=True).stdout
    rows = [line.split(None, 2) for line in out.splitlines() if line.strip()]
    children: dict[int, list[tuple[int, str]]] = {}
    for p, pp, comm in rows:
        children.setdefault(int(pp), []).append((int(p), comm))
    result, stack = [], [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            result.append(child)
            stack.append(child[0])
    return result


def _poly_pids(client) -> list[int]:
    return [p for p, comm in _descendants(client.process.pid) if comm.startswith("poly")]


# ── Z2 / Z4 / Z6 / Z22 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_z2_cancel_target_file_then_resume(prover):
    """Z2: the interrupted command goes back to unevaluated; the finished prefix
    keeps its result (witness A written once); Z4: nothing re-runs while idle;
    Z6: a second cancel is the early-exit reply, and the server answers
    nothing_running when asked directly; then the re-evaluation completes and
    the interrupted command runs to the end (S appears), C runs once; Z22: a
    query tool still works afterwards."""
    client, root = prover
    witness = os.path.join(root, "witness.txt")
    path = os.path.join(root, "Z2.thy")
    with open(path, "w") as f:
        f.write(_theory("Z2", witness))

    view = await evaluate_to(client, path, L_END)
    assert view.status == "in_progress", view.status
    assert await _wait_state(client, path, L_SLOW, RUNNING) == RUNNING
    assert _read(witness) == "A"

    t0 = time.monotonic()
    view = await cancel_evaluation(client)
    elapsed = time.monotonic() - t0
    print(f"\nZ2 cancel took {elapsed:.2f}s; message:\n{view.message}")
    assert view.status == "cancelled"
    lines = view.message.split("\n")
    assert lines[0] == ev.CANCEL_MESSAGES[ev.CANCEL_OUTCOME_RETIRED], view.message
    assert any(l.startswith("Reset to unevaluated: ") and f":{L_SLOW} (ML)" in l
               for l in lines), view.message
    assert elapsed < 30, elapsed

    # the prefix is untouched, the interrupted command is back to unevaluated
    assert await _wait_state(client, path, L_SLOW, NOT_EVALUATED) == NOT_EVALUATED
    assert await _wait_state(client, path, L_A, PROCESSED) == PROCESSED
    assert _read(witness) == "A"

    # Z4: idle for longer than the slow command takes -- nothing re-runs
    await asyncio.sleep(SLOW_SECONDS + 5)
    assert _read(witness) == "A"
    assert ev.position_state(client, path, MCPLine(L_SLOW)) == NOT_EVALUATED

    # Z6: the Python guard sees nothing to cancel; the server, asked directly,
    # reports nothing running
    assert (await cancel_evaluation(client)).status == "no_evaluation"
    payload = await client.force_interrupt()
    assert payload["outcome"] == ev.CANCEL_OUTCOME_NOTHING_RUNNING, payload

    # the positive control: re-evaluating runs the interrupted command to the end
    view = await _run_to_completion(client, path, L_END)
    assert view.status == "complete", view
    assert _read(witness) == "ASC", _read(witness)
    assert ev.position_state(client, path, MCPLine(L_SLOW)) == PROCESSED

    # Z22: a query after the cancel/resume still works
    state = await goal(client, path, MCPLine(L_LEMMA), after_text="by simp")
    assert state is not None


# ── Z11: two forked proofs in one node, retired in one batch ─────────────


@pytest.mark.asyncio
async def test_z11_two_running_commands_same_node(prover):
    """Z11: two slow commands alive at once in one theory (forked proofs);
    both retired in one batch, in ascending order, both re-run afterwards."""
    client, root = prover
    witness = os.path.join(root, "w11.txt")
    w = witness.replace("\\", "\\\\")
    path = os.path.join(root, "Z11.thy")
    slow_tac = (
        "by (tactic ‹fn st => (let val t0 = Time.now () in while "
        f"Time.< (Time.- (Time.now (), t0), seconds {SLOW_SECONDS}) do "
        "ignore (List.tabulate (1000, fn i => i)) end; "
        'File.append (Path.explode "%s") "%s"; Seq.single st)›)'
    )
    with open(path, "w") as f:
        f.write(
            "theory Z11\nimports Main\nbegin\n"
            f'lemma p1: "True" {slow_tac % (w, "P")}\n'          # line 4
            f'lemma p2: "True" {slow_tac % (w, "Q")}\n'          # line 5
            'lemma z_lemma: "1 + 1 = (2::nat)" by simp\n'          # line 6
            "end\n"
        )
    view = await evaluate_to(client, path, 7)
    assert view.status == "in_progress"
    # both proofs are forked: wait until both show running
    assert await _wait_state(client, path, 4, RUNNING) == RUNNING
    assert await _wait_state(client, path, 5, RUNNING, timeout=10) == RUNNING

    view = await cancel_evaluation(client)
    print(f"\nZ11 message:\n{view.message}")
    assert view.message.split("\n")[0] == ev.CANCEL_MESSAGES[ev.CANCEL_OUTCOME_RETIRED]
    reset = [l for l in view.message.split("\n") if l.startswith("Reset to unevaluated")]
    # the running command is the proof command ("by"), not the "lemma" header
    assert reset and ":4 (by)" in reset[0] and ":5 (by)" in reset[0], view.message
    assert reset[0].index(":4 (by)") < reset[0].index(":5 (by)")   # ascending
    assert _read(witness) == ""

    view = await _run_to_completion(client, path, 7)
    assert view.status == "complete", view
    # a proof method may be applied more than once per run, so count sets, not
    # letters: both proofs ran after the cancel, none had run before it
    assert set(_read(witness)) == {"P", "Q"}, _read(witness)


# ── Z20 / Z21: report ordering under a markup flood; concurrent tool calls ──


@pytest.mark.asyncio
async def test_z20_z21_flood_and_concurrent_calls(prover):
    """Z20: a command that floods the channel with output, then the slow one;
    the cancel report still arrives and the outcome is retired. Z21: a status
    call issued while the cancel runs queues behind it and completes after."""
    client, root = prover
    witness = os.path.join(root, "w20.txt")
    path = os.path.join(root, "Z20.thy")
    thy = _theory("Z20", witness).replace(
        'ML ‹z_mark "A"›',
        'ML ‹List.app (fn i => writeln (string_of_int i)) (List.tabulate (20000, fn i => i)); z_mark "A"›')
    with open(path, "w") as f:
        f.write(thy)

    view = await evaluate_to(client, path, L_END)
    assert view.status == "in_progress"
    assert await _wait_state(client, path, L_SLOW, RUNNING, timeout=90) == RUNNING

    order: list[str] = []

    async def status_during_cancel():
        await asyncio.sleep(0.5)
        await evaluation_status(client)
        order.append("status")

    async def cancel():
        view = await cancel_evaluation(client)
        order.append("cancel")
        return view

    view, _ = await asyncio.gather(cancel(), status_during_cancel())
    print(f"\nZ20 message:\n{view.message}\norder={order}")
    assert view.message.split("\n")[0] == ev.CANCEL_MESSAGES[ev.CANCEL_OUTCOME_RETIRED]
    assert order == ["cancel", "status"], order       # Z21: queued behind the cancel

    view = await _run_to_completion(client, path, L_END)
    assert view.status == "complete", view
    assert _read(witness) == "ASC"


# ── Z19: the catastrophe -- a prover that does not answer ─────────────────


@pytest.mark.asyncio
async def test_z19_stopped_prover_is_a_catastrophe(prover):
    """Z19 branch (a): SIGSTOP the ML process; the cancel cannot get its report,
    the server aborts within its 120 s budget, the Python side raises
    IsabelleCatastrophe; the teardown (what the tool boundary runs) leaves no
    poly process behind within 3 s; a fresh launch then works."""
    client, root = prover
    witness = os.path.join(root, "w19.txt")
    path = os.path.join(root, "Z19.thy")
    with open(path, "w") as f:
        f.write(_theory("Z19", witness, slow_seconds=600.0))

    view = await evaluate_to(client, path, L_END)
    assert view.status == "in_progress"
    assert await _wait_state(client, path, L_SLOW, RUNNING) == RUNNING
    polys = _poly_pids(client)
    assert polys, "no poly process found under the server"
    for p in polys:
        os.kill(p, 19)   # SIGSTOP

    t0 = time.monotonic()
    with pytest.raises(IsabelleCatastrophe) as exc:
        await cancel_evaluation(client)
    elapsed = time.monotonic() - t0
    print(f"\nZ19 catastrophe after {elapsed:.1f}s: {exc.value}")
    assert 100 < elapsed < ev.CANCEL_TOTAL_BUDGET + 5, elapsed
    assert not ev.evaluation_state.active

    # what CatastropheMiddleware does
    t1 = time.monotonic()
    await client.teardown("test: catastrophe")
    assert client.process is None
    for _ in range(30):
        if not any(p for p in polys if os.path.exists(f"/proc/{p}")):
            break
        await asyncio.sleep(0.1)
    alive = [p for p in polys if os.path.exists(f"/proc/{p}")]
    print(f"Z19 teardown took {time.monotonic() - t1:.1f}s; poly alive after: {alive}")
    for p in alive:
        os.kill(p, 9)   # do not leave a stopped orphan behind whatever the verdict
    assert not alive, alive

    # a fresh launch works
    await client.start()
    view = await _run_to_completion(client, os.path.join(root, "Z19.thy"), L_A)
    assert view.status == "complete", view


# ── Z17: the file is edited while the cancel runs ─────────────────────────


@pytest.mark.asyncio
async def test_z17_edit_during_cancel(prover):
    """Z17: an external edit lands while the cancellation is in flight (the
    File_Watcher path, outside every lock). The request must either retire the
    command or find it re-minted by the edit, never corrupt the session: a
    later evaluation must still complete (the session still produces versions)."""
    client, root = prover
    witness = os.path.join(root, "w17.txt")
    path = os.path.join(root, "Z17.thy")
    with open(path, "w") as f:
        f.write(_theory("Z17", witness))
    view = await evaluate_to(client, path, L_END)
    assert view.status == "in_progress"
    assert await _wait_state(client, path, L_SLOW, RUNNING) == RUNNING

    async def edit_soon():
        await asyncio.sleep(0.05)
        # replace the slow command by a fast one, and push the change ourselves
        # (the watcher is event-driven; the direct push is the same code path)
        text = _theory("Z17", witness).replace(
            "ML ‹let val t0 = Time.now ()", "ML ‹let val t0 = Time.now () (*edited*)")
        with open(path, "w") as f:
            f.write(text)
        await ev.sync_file_locked(client, path)

    view, _ = await asyncio.gather(cancel_evaluation(client), edit_soon())
    print(f"\nZ17 message:\n{view.message}")
    assert view.status == "cancelled"
    assert view.message.split("\n")[0] == ev.CANCEL_MESSAGES[ev.CANCEL_OUTCOME_RETIRED]

    # the session is alive and still produces versions: a full run completes
    view = await _run_to_completion(client, path, L_END)
    assert view.status == "complete", view
    assert _read(witness) == "ASC", _read(witness)


# ── Z14: probe cost with a large probe set ────────────────────────────────


@pytest.mark.asyncio
async def test_z14_probe_cost_large_document(prover):
    """Z14(a): N large (thousands of evaluated commands), A = 1. Measures the
    whole cancel round trip; the server log carries the probe call count."""
    client, root = prover
    witness = os.path.join(root, "w14.txt")
    path = os.path.join(root, "Z14.thy")
    n = 2000
    body = "".join(f'lemma l{i}: "True" by simp\n' for i in range(n))
    thy = _theory("Z14", witness).replace("begin\n", "begin\n" + body, 1)
    with open(path, "w") as f:
        f.write(thy)
    slow_line = L_SLOW + n
    view = await evaluate_to(client, path, L_END + n)
    assert view.status == "in_progress"
    assert await _wait_state(client, path, slow_line, RUNNING, timeout=300) == RUNNING

    t0 = time.monotonic()
    view = await cancel_evaluation(client)
    elapsed = time.monotonic() - t0
    print(f"\nZ14 N≈{n + 4} commands, A=1: cancel took {elapsed:.2f}s\n{view.message}")
    assert view.message.split("\n")[0] == ev.CANCEL_MESSAGES[ev.CANCEL_OUTCOME_RETIRED]
    assert elapsed < 30, elapsed
