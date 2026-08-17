"""Debugger probes, against a REAL prover — the permanent form of Phase A's probes.

These drive the ``PIDE/debugger_*`` requests through the client's thin
wrappers with no MCP tool in between, the way test_query_tools_e2e.py drives
``PIDE/find_theorems_at_position``.  They pin the assumptions
DEBUGGER_DESIGN.md builds on; an Isabelle upgrade that
silently breaks one of them fails here first.  Numbering follows
DEBUGGER_IMPLEMENTATION_PLAN.md's probe list; gates 1–3 gate the whole design.

Probe policy (project rule: measure, do not infer from source): every probe uses
an observable side effect plus a positive control, and waits generously (>= 60s)
before concluding a negative.

Marked ``integration`` (deselected by default) and skipped unless ``isabelle``
is on PATH:

    PATH=contrib/Isabelle2025-2/bin:$PATH pytest tests/integration -m integration
"""
import asyncio
import os
import shutil
import time

import pytest

from isabelle_mcp import evaluation as ev
from isabelle_mcp.evaluation import evaluate_to, evaluation_status
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.tools.goal import goal
from isabelle_mcp.utils import MCPLine

pytestmark = pytest.mark.integration

if shutil.which("isabelle") is None:
    pytest.skip("isabelle not on PATH", allow_module_level=True)


# One theory, three arenas: a finished lemma the query tools can be asked about
# while a thread is parked (probe 8); probe_target with an indented statement, a
# column-1 statement and top-level declarations (gates 1-2, probes 4/11/11bis/13);
# slow_target whose `sluggish` value carries a deliberately slow printer (probe
# 11bis's per-value bound and outer-deadline interplay).  The slow printer is
# installed through the prelude's re-exposed Isabelle_MCP_PolyML — the raw
# addPrettyPrinter, since ML_system_pp is a no-op stub in user theory ML.
# (A 2e6-node raw term was measured to print in well under 5s — depth pruning
# is effective on raw terms — so slowness must be manufactured, not found.)
# 1-indexed line names below.
THEORY = r'''theory DebugProbe
imports Main
begin

lemma probe_lemma: "(x::nat) + 0 = x"
  by simp

ML \<open>
fun probe_target (n: int) =
  let
    val xs = map (fn i => i + n) (1 upto 3);
val shift = n + 1;
    val total = List.foldl (fn (i, acc) => acc + i + shift) 0 xs;
  in total end;
val top_level_val = 42;
\<close>

ML \<open>
datatype mcp_slow = MCP_Slow;
val _ = Isabelle_MCP_PolyML.addPrettyPrinter (fn _ => fn _ => fn (_: mcp_slow) =>
  (OS.Process.sleep (Time.fromSeconds 30); PolyML.PrettyString "MCP_Slow"));
fun slow_target (n: int) =
  let
    val sluggish = MCP_Slow;
    val small = n + 1;
    val answer = small + (case sluggish of MCP_Slow => 0);
  in answer end;
\<close>

ML \<open>val probe_result = probe_target 4\<close>

ML \<open>val slow_result = slow_target 1\<close>

end
'''
LEMMA = 5
VAL_XS = 11        # indented statement (4 spaces)
VAL_SHIFT = 12     # column-1 statement
VAL_TOTAL = 13
TOP_LEVEL_VAL = 15
DEFINER_END = 16   # end of probe_target's ML block
VAL_ANSWER = 26    # slow_target's site with sluggish and small in scope
SLOW_DEFINER_END = 28
CALLER = 30        # ML block calling probe_target
SLOW_CALLER = 32   # ML block calling slow_target


RUNAWAY = "let fun f xs = f (1 :: xs) in f [] end"  # allocating: has safe points


@pytest.fixture
async def prover(tmp_path):
    ev.EVAL_POLL_INTERVAL = 4.0
    path = os.path.join(str(tmp_path), "DebugProbe.thy")
    with open(path, "w") as f:
        f.write(THEORY)
    # The evaluation bookkeeping is module-global; a failed earlier test in the
    # same process must not leak an "active" evaluation into this one.
    ev.evaluation_state = ev.EvaluationState()
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


_SETTLED = ("complete", "no_evaluation")


async def _evaluate_through(client, path, line, tries=60):
    view = await evaluate_to(client, path, line)
    for _ in range(tries):
        if view.status in _SETTLED:
            return True
        await asyncio.sleep(2)
        view = await evaluation_status(client)
    return False


async def _wait_settled(client, tries=60):
    """Wait for the ALREADY-RUNNING evaluation (the one a hit stalled) to end;
    evaluate_to would refuse while it is still active."""
    for _ in range(tries):
        view = await evaluation_status(client)
        if view.status in _SETTLED:
            return True
        await asyncio.sleep(2)
    return False


async def _breakpoints(client, path, timeout=30.0):
    """Listing with prover-truth states: each call resolves the enabled-states
    from the real breakpoint refs in one batched round trip.

    `outdated` (pending edits not yet incorporated) is the one status where
    retrying is the uniquely correct response, so this helper absorbs it with
    a bounded loop; every other non-ok status stays a hard failure."""
    reply: dict = {}
    for _ in range(30):
        reply = await client.debugger_breakpoints(
            path, timeout=timeout, request_timeout=timeout + 30.0)
        if reply.get("status") != "outdated":
            break
        await asyncio.sleep(1.0)
    assert reply.get("status") == "ok", f"listing failed: {reply}"
    assert reply.get("open") is True, f"file not open in the prover: {reply}"
    return reply["breakpoints"]


def _corrected(bp):
    """Anchor at the markup range's END (the one-symbol shift, design 3.3),
    as (0-based line, 0-based character)."""
    end = bp["range"]["end"]
    return (end["line"], end["character"])


async def _toggle(client, path, serial, state, *, timeout=30.0):
    """Acknowledged toggle: absolute semantics, prover-truth write; the reply
    carries the previous value in `was` when status is ok."""
    return await client.debugger_toggle_breakpoint(
        path, serial, state, timeout=timeout, request_timeout=timeout + 30.0)


async def _enable_site_at(client, path, line_1indexed, character_0indexed):
    bps = await _breakpoints(client, path)
    matches = [
        bp for bp in bps
        if _corrected(bp) == (line_1indexed - 1, character_0indexed)
    ]
    assert matches, (
        f"no breakable site corrected to line {line_1indexed} "
        f"char {character_0indexed}; sites: {[_corrected(b) for b in bps]}"
    )
    serial = matches[0]["serial"]
    reply = await _toggle(client, path, serial, True)
    assert reply.get("status") == "ok", f"toggle failed: {reply}"
    return serial


async def _wait_for_hit(client, timeout=90.0):
    ok = await client.wait_debugger_event(
        lambda c: any(stack for stack in c.debugger_threads.values()),
        timeout=timeout,
    )
    assert ok, "no debugger_state with a non-empty stack arrived"
    return next(t for t, s in client.debugger_threads.items() if s)


async def _wait_all_resumed(client, timeout=60.0):
    return await client.wait_debugger_event(
        lambda c: not any(stack for stack in c.debugger_threads.values()),
        timeout=timeout,
    )


async def _eval_at(client, thread, expr, timeout_s=30.0, *,
                   request_timeout=None):
    """The bare expression: the Scala side composes the wrapper text and carries
    the expression as one ML string literal (design section 4.9)."""
    return await client.debugger_eval(
        thread, expr, timeout=float(timeout_s),
        request_timeout=request_timeout or (timeout_s + 60.0),
    )


def _texts(result):
    return [m["text"] for m in result.get("messages", [])]


# ── Gate 1: sites are visible and where we think they are ──────────────────

@pytest.mark.asyncio
async def test_gate1_sites_are_where_we_think_they_are(prover):
    client, path = prover
    assert await _evaluate_through(client, path, -1), "the theory never evaluated"

    bps = await _breakpoints(client, path)
    assert len(bps) >= 3, f"positive control failed: only {len(bps)} sites: {bps}"

    # Every markup range is a single symbol: one character on one line, or the
    # newline ending the previous line (start on line N, end at col 0 of N+1).
    for bp in bps:
        start, end = bp["range"]["start"], bp["range"]["end"]
        same_line = (start["line"] == end["line"]
                     and end["character"] - start["character"] == 1)
        newline = (end["line"] == start["line"] + 1 and end["character"] == 0)
        assert same_line or newline, f"not a single-symbol range: {bp}"

    corrected = [_corrected(bp) for bp in bps]

    # The indented statement: shifted range covers the last indentation space,
    # so the corrected position is the statement's first character (col 4).
    assert (VAL_XS - 1, 4) in corrected, f"no site before `val xs`: {corrected}"
    xs_bp = next(bp for bp in bps if _corrected(bp) == (VAL_XS - 1, 4))
    assert xs_bp["range"]["start"] == {"line": VAL_XS - 1, "character": 3}, (
        f"indented site does not cover the last indentation space: {xs_bp}\n"
        "If it covers the statement's first letter instead, the shift "
        "correction must be REMOVED (gate 1's alternative outcome)."
    )

    # The column-1 statement: shifted range covers the previous line's newline.
    assert (VAL_SHIFT - 1, 0) in corrected, f"no site before `val shift`: {corrected}"
    shift_bp = next(bp for bp in bps if _corrected(bp) == (VAL_SHIFT - 1, 0))
    assert shift_bp["range"]["start"]["line"] == VAL_SHIFT - 2, (
        f"column-1 site does not cross from the previous line: {shift_bp}"
    )

    assert (VAL_TOTAL - 1, 4) in corrected, f"no site before `val total`: {corrected}"

    # Top-level val/fun declarations yield no sites.
    on_top_level = [c for c in corrected if c[0] == TOP_LEVEL_VAL - 1]
    assert not on_top_level, f"unexpected site on a top-level val: {on_top_level}"

    # All enabled states start false.
    assert all(bp["state"] is False for bp in bps)


# ── Gates 2 and 3, probes 4, 8, 10, 11, 13 — the arc at a live hit ─────────

@pytest.mark.asyncio
async def test_gate2_gate3_and_the_probes_at_a_live_hit(prover):
    client, path = prover

    # Motion 1: evaluate to the end of the defining ML block, set, evaluate on.
    assert await _evaluate_through(client, path, DEFINER_END)
    await _enable_site_at(client, path, VAL_XS, 4)

    # Gate 2: a breakpoint stops a thread.
    await evaluate_to(client, path, -1)
    thread = await _wait_for_hit(client)
    stack = client.debugger_threads[thread]
    assert stack, "hit with an empty stack"

    # Probe 13 (frame position resolution): record what the frames carry.
    frame0 = stack[0]
    assert frame0.get("function"), f"frame 0 has no function name: {frame0}"
    print(f"\nPROBE 13 — frame positions at the hit: {stack}")

    # Probe 8: query tools answer about processed lines while a thread is parked.
    state = await goal(client, path, MCPLine(LEMMA))
    assert state.subgoals == ["x + 0 = x"]

    # Probe 10: how a stopped command reports its status.
    theories = await client.request_theory_status()
    ours = [t for t in theories if "DebugProbe" in str(t)]
    print(f"\nPROBE 10 — theory_status during a hit: {ours}")

    # Probe 4: exactly one debugger_state per input, always after the output.
    states_before = len(client.debugger_state_history)
    result = await _eval_at(client, thread, "()", timeout_s=30)
    assert result["status"] == "ok", f"empty eval did not complete: {result}"
    assert len(client.debugger_state_history) == states_before + 1, (
        "an eval round trip must produce exactly one debugger_state"
    )
    result = await _eval_at(client, thread, "1 + 1", timeout_s=30)
    assert result["status"] == "ok"
    assert any("val it = 2: int" in t for t in _texts(result)), _texts(result)
    result = await _eval_at(client, thread, 'raise Fail "probe-raise"', timeout_s=30)
    assert result["status"] == "ok"
    assert any("probe-raise" in t for t in _texts(result)), _texts(result)
    assert thread in client.debugger_threads, "the thread left the hit"

    # Gate 3: an allocating runaway under a 5s envelope ends as a TIMEOUT error
    # and the thread is still parked.  Twice — the asynch-once re-arm.
    for attempt in (1, 2):
        result = await _eval_at(client, thread, RUNAWAY, timeout_s=5,
                                request_timeout=120.0)
        assert result["status"] == "ok", (
            f"attempt {attempt}: prover-side timeout did not fire "
            f"(status {result['status']}) — if this reproduces, the eval/abort "
            f"design of DEBUGGER_DESIGN.md 7.3-7.4 must be reconsidered"
        )
        assert any("Isabelle_MCP.debug_eval: TIMEOUT" in t for t in _texts(result)), (
            f"attempt {attempt}: no TIMEOUT error message: {_texts(result)}"
        )
        assert thread in client.debugger_threads, (
            f"attempt {attempt}: the timeout killed the parked thread"
        )
        # follow-up eval still answers
        follow = await _eval_at(client, thread, "2 + 2", timeout_s=30)
        assert follow["status"] == "ok"
        assert any("val it = 4: int" in t for t in _texts(follow))

    # Probe 11: the abort flag.  Nothing evaluating -> refused.
    reply = await client.debugger_abort(thread, request_timeout=30.0)
    assert reply == {"status": "no_evaluation"}, reply

    # A slow eval ends early on abort; the thread stays parked.
    eval_task = asyncio.create_task(
        _eval_at(client, thread, RUNAWAY, timeout_s=600, request_timeout=700.0))
    await asyncio.sleep(3.0)
    reply = await client.debugger_abort(thread, request_timeout=30.0)
    assert reply == {"status": "aborting"}, reply
    result = await asyncio.wait_for(eval_task, timeout=60)
    assert result["status"] == "ok"
    assert any("Isabelle_MCP.debug_eval: ABORTED" in t for t in _texts(result)), (
        _texts(result))
    assert thread in client.debugger_threads
    # A stale abort never reaches the next evaluation (flag died with its entry).
    follow = await _eval_at(client, thread, "3 + 3", timeout_s=30)
    assert follow["status"] == "ok"
    assert any("val it = 6: int" in t for t in _texts(follow))

    # Resume; the theory runs to its end.
    await client.debugger_input(thread, ["continue"], request_timeout=30.0)
    assert await _wait_all_resumed(client), "the thread never resumed"
    assert await _wait_settled(client)


# ── R3: the self-compiling wrapper — envelope silence and containment ──────

@pytest.mark.asyncio
async def test_r3_wrapper_envelope_and_containment(prover):
    client, path = prover
    assert await _evaluate_through(client, path, DEFINER_END)
    await _enable_site_at(client, path, VAL_TOTAL, 4)
    await evaluate_to(client, path, -1)
    thread = await _wait_for_hit(client)

    # Envelope silence: exactly ONE message — the inner result echo.  The outer
    # `val _ = ...;` binding flushes an EMPTY writeln under verbose = true, and
    # only Debugger.writeln_message's empty-message drop keeps it off the wire;
    # if an Isabelle upgrade breaks that drop, a second message shows up here.
    result = await _eval_at(client, thread, "1 + 1", timeout_s=30)
    assert result["status"] == "ok", result
    assert _texts(result) == ["val it = 2: int"], _texts(result)

    # An empty expression is wire-legal: it compiles to `val it = ( );`.
    # (The MCP tool layer refuses it before it gets this far — Phase C.)
    result = await _eval_at(client, thread, "", timeout_s=30)
    assert result["status"] == "ok", result
    assert _texts(result) == ["val it = (): unit"], _texts(result)

    # Frame scope: the literal is compiled in the hit frame's scope (n = 4 at
    # probe_target's `val total` site).
    result = await _eval_at(client, thread, "n * 100", timeout_s=30)
    assert result["status"] == "ok", result
    assert any("val it = 400: int" in t for t in _texts(result)), _texts(result)

    # Literal round trip, quotes and backslashes: the \ddd encoder must not
    # corrupt them on the way in; the result echo re-escapes them on the way
    # out.
    result = await _eval_at(client, thread, r'"x\"y\\z"', timeout_s=30)
    assert result["status"] == "ok", result
    joined = "\n".join(_texts(result))
    assert 'x\\"y\\\\z' in joined, joined

    # Literal round trip, unicode: the Scala side Symbol.encodes the expression
    # before embedding, exactly what typing α inside a .thy ML block yields —
    # so the compiled string holds the 8-byte "\<alpha>", not 2-byte UTF-8.
    result = await _eval_at(client, thread, 'size "α"', timeout_s=30)
    assert result["status"] == "ok", result
    assert any("val it = 8: int" in t for t in _texts(result)), _texts(result)

    # Containment: the classic paren-escape now runs INSIDE the protection — a
    # smuggled runaway declaration still ends as the wrapper's TIMEOUT and the
    # thread survives.  (Composed client-side, this text escaped debug_eval.)
    result = await _eval_at(client, thread, f"1); val boom = ({RUNAWAY}",
                            timeout_s=5, request_timeout=120.0)
    assert result["status"] == "ok", result
    assert any("Isabelle_MCP.debug_eval: TIMEOUT" in t for t in _texts(result)), (
        _texts(result))
    assert thread in client.debugger_threads

    # A bare declaration fails to parse against the shell's own tokens; the
    # compile error classifies inside the wrapper and the thread answers again.
    result = await _eval_at(client, thread, "fun f x = x", timeout_s=30)
    assert result["status"] == "ok", result
    assert thread in client.debugger_threads
    follow = await _eval_at(client, thread, "2 + 2", timeout_s=30)
    assert follow["status"] == "ok", follow
    assert any("val it = 4: int" in t for t in _texts(follow)), _texts(follow)

    await client.debugger_input(thread, ["continue"], request_timeout=30.0)
    assert await _wait_all_resumed(client), "the thread never resumed"
    assert await _wait_settled(client)


# ── R4: the acknowledged toggle ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_r4_acknowledged_toggle(prover):
    client, path = prover
    assert await _evaluate_through(client, path, DEFINER_END)

    bps = await _breakpoints(client, path)
    serial = next(bp["serial"] for bp in bps
                  if _corrected(bp) == (VAL_XS - 1, 4))

    # First arming acknowledges with the previous value False.
    reply = await _toggle(client, path, serial, True)
    assert reply == {"status": "ok", "was": False}, reply
    # Absolute semantics: re-arming is idempotent and reports was=True.
    reply = await _toggle(client, path, serial, True)
    assert reply == {"status": "ok", "was": True}, reply
    # Disarming reports was=True; disarming again, was=False.
    reply = await _toggle(client, path, serial, False)
    assert reply == {"status": "ok", "was": True}, reply
    reply = await _toggle(client, path, serial, False)
    assert reply == {"status": "ok", "was": False}, reply

    # A serial the file's markup does not know is refused before the prover.
    reply = await _toggle(client, path, 999999999, True)
    assert reply == {"status": "unknown_breakpoint"}, reply

    # The acknowledged write is the real breakpoint ref: arm and it hits.
    reply = await _toggle(client, path, serial, True)
    assert reply == {"status": "ok", "was": False}, reply
    await evaluate_to(client, path, -1)
    thread = await _wait_for_hit(client)
    await client.debugger_input(thread, ["continue"], request_timeout=30.0)
    assert await _wait_all_resumed(client), "the thread never resumed"
    assert await _wait_settled(client)


# ── R5/R11: a refused toggle on a RUNNING command lists as unfinished ──────

# One command holding all three pieces: the instrumented definition (whose
# breakpoint markup is reported when its compile unit is COMPILED, before the
# next unit runs), a gate polling for a sentinel file (holds the command
# unfinished, no timing dependence), and the call.  The fixture interpolates
# the sentinel path and opens the gate unconditionally at teardown.
GATED_THEORY = r'''theory GatedProbe
imports Main
begin

ML \<open>
fun gated_target (n: int) =
  let
    val g = n + 1;
  in g end;
val _ =
  let
    fun wait 0 = ()
      | wait k =
          if OS.FileSys.access ("SENTINEL_PATH_PLACEHOLDER", []) then ()
          else (OS.Process.sleep (Time.fromReal 0.25); wait (k - 1));
  in wait 240 end;
val gated_result = gated_target 3;
\<close>

end
'''
GATED_VAL_G = 8   # 1-indexed: `    val g = n + 1;` (site corrected to char 4)


@pytest.fixture
async def gated_prover(tmp_path):
    ev.EVAL_POLL_INTERVAL = 4.0
    sentinel = os.path.join(str(tmp_path), "gate-open")
    path = os.path.join(str(tmp_path), "GatedProbe.thy")
    with open(path, "w") as f:
        f.write(GATED_THEORY.replace("SENTINEL_PATH_PLACEHOLDER", sentinel))
    ev.evaluation_state = ev.EvaluationState()
    client = IsabelleLSPClient(
        logic="HOL",
        project_root=str(tmp_path),
        debug=True,
        extra_args=["-o", "editor_tracing_messages=0"],
    )
    await client.start()
    try:
        yield client, path, sentinel
    finally:
        # Open the gate on EVERY path so a failed probe never wedges shutdown.
        with open(sentinel, "w") as f:
            f.write("open")
        await client.shutdown()


@pytest.mark.asyncio
async def test_r5_refused_toggle_on_running_command_lists_unfinished(gated_prover):
    client, path, sentinel = gated_prover
    await evaluate_to(client, path, -1)

    # Poll the listing until the definition's site appears WITH state
    # "unfinished".  Everything else is "not yet": the markup arrives
    # asynchronously; a just-appeared site can transiently resolve
    # "undefined" (assignment window); a listing during the pending-edit
    # window answers top-level "outdated".  The gate holds the command
    # unfinished, so poll-until-unfinished is monotone-safe.
    deadline = time.monotonic() + 45.0
    serial = None
    while time.monotonic() < deadline:
        reply = await client.debugger_breakpoints(
            path, timeout=30.0, request_timeout=60.0)
        if reply.get("status") == "ok" and reply.get("open") is True:
            match = [
                bp for bp in reply["breakpoints"]
                if _corrected(bp) == (GATED_VAL_G - 1, 4)
                and bp["state"] == "unfinished"
            ]
            if match:
                serial = match[0]["serial"]
                break
        await asyncio.sleep(1.5)
    assert serial is not None, (
        "HARD FAIL by design, not an environment flake (the sentinel gate "
        "removes all timing dependence): the gated command's site never "
        "listed as 'unfinished' while the gate held the command running.  "
        "The running-command listing semantics of DEBUGGER_DESIGN.md 7.1 and "
        "R11's discriminating leg do not hold on this build — re-examine the "
        "design; do not skip or retry this probe."
    )

    # The discriminating leg: the toggle on the running command is REFUSED
    # with "unfinished" — and the listing keeps showing the real ref's state,
    # not an echo of the attempted arming.
    reply = await _toggle(client, path, serial, True)
    assert reply == {"status": "unfinished"}, reply
    bps = await _breakpoints(client, path)
    entry = next(bp for bp in bps if bp["serial"] == serial)
    assert entry["state"] == "unfinished", entry

    # Open the gate; the command finishes; the real ref reads FALSE (the
    # refused toggle wrote nothing), and the positive control now succeeds.
    with open(sentinel, "w") as f:
        f.write("open")
    assert await _wait_settled(client, tries=60), "the gated command never finished"
    bps = await _breakpoints(client, path)
    entry = next(bp for bp in bps if _corrected(bp) == (GATED_VAL_G - 1, 4))
    assert entry["state"] is False, entry
    reply = await _toggle(client, path, entry["serial"], True)
    assert reply == {"status": "ok", "was": False}, reply


# ── R5: listing states are prover truth ────────────────────────────────────

@pytest.mark.asyncio
async def test_r5_listing_states_are_prover_truth(prover):
    client, path = prover
    assert await _evaluate_through(client, path, DEFINER_END)

    bps = await _breakpoints(client, path)
    assert all(bp["state"] is False for bp in bps), bps
    serial = next(bp["serial"] for bp in bps
                  if _corrected(bp) == (VAL_XS - 1, 4))

    # Arm through the acknowledged toggle; the next listing reads the real ref.
    reply = await _toggle(client, path, serial, True)
    assert reply == {"status": "ok", "was": False}, reply
    states = {bp["serial"]: bp["state"] for bp in await _breakpoints(client, path)}
    assert states[serial] is True, states
    assert all(v is False for s, v in states.items() if s != serial), states

    # Disarm; the listing follows.
    reply = await _toggle(client, path, serial, False)
    assert reply == {"status": "ok", "was": True}, reply
    bps = await _breakpoints(client, path)
    assert all(bp["state"] is False for bp in bps), bps


# ── R6: eval/print_vals refused on a thread that is not stopped ────────────

@pytest.mark.asyncio
async def test_r6_eval_refused_on_not_stopped_thread(prover):
    client, path = prover
    assert await _evaluate_through(client, path, DEFINER_END)

    # A name that never stopped: refused before anything is sent -- sending
    # would create the thread's input queue and poison its next stop.
    result = await _eval_at(client, "no-such-thread", "1 + 1", timeout_s=10)
    assert result["status"] == "not_stopped", result

    # A real thread after it resumed: same refusal, and the guarantee is the
    # same -- the expression was never sent, so nothing ran.
    await _enable_site_at(client, path, VAL_XS, 4)
    await evaluate_to(client, path, -1)
    thread = await _wait_for_hit(client)
    await client.debugger_input(thread, ["continue"], request_timeout=30.0)
    assert await _wait_all_resumed(client)
    result = await _eval_at(client, thread, "1 + 1", timeout_s=10)
    assert result["status"] == "not_stopped", result
    assert await _wait_settled(client)

    # print_vals shares start_eval's fence.
    reply = await client.debugger_print_vals(
        thread, timeout=10.0, request_timeout=60.0)
    assert reply["status"] == "not_stopped", reply


# ── R2: abort on an indebted thread ────────────────────────────────────────

@pytest.mark.asyncio
async def test_r2_abort_on_indebted_thread_replies_aborting(prover):
    client, path = prover
    assert await _evaluate_through(client, path, DEFINER_END)
    await _enable_site_at(client, path, VAL_XS, 4)
    await evaluate_to(client, path, -1)
    thread = await _wait_for_hit(client)

    # Manufacture debt: the expression defers interrupt delivery around a 60s
    # sleep, so the 3s prover-side deadline cannot cut it (measured: a bare
    # self-recursive loop IS cut — function entry is a safe point — hence the
    # explicit no_interrupts shield), the Scala backstop answers `timeout` at
    # 3+30s, and the thread then OWES a debugger_state until the sleep ends.
    result = await _eval_at(
        client, thread,
        "Thread_Attributes.with_attributes Thread_Attributes.no_interrupts "
        "(fn _ => OS.Process.sleep (Time.fromSeconds 60))",
        timeout_s=3, request_timeout=120.0)
    assert result["status"] == "timeout", result

    # The busy fence holds while the debt is owed...
    follow = await _eval_at(client, thread, "1", timeout_s=5,
                            request_timeout=60.0)
    assert follow["status"] == "busy", follow

    # ...and abort on the indebted thread is ACCEPTED: the abandoned runaway
    # is exactly what abort exists for.  (Before the R2 fix this answered
    # no_evaluation, because the check consulted only the pending table.)
    reply = await client.debugger_abort(thread, request_timeout=30.0)
    assert reply == {"status": "aborting"}, reply

    # The late completion arrives when the sleep ends, pays the debt, and the
    # fence lifts — the settled thread answers evals again and abort now says
    # no_evaluation, which is the Python retry loop's stop signal.
    lifted = None
    for _ in range(30):
        r = await _eval_at(client, thread, "2 + 2", timeout_s=10,
                           request_timeout=60.0)
        if r["status"] == "ok":
            lifted = r
            break
        assert r["status"] == "busy", r
        await asyncio.sleep(5.0)
    assert lifted is not None, "the debt never cleared"
    assert any("val it = 4: int" in t for t in _texts(lifted)), _texts(lifted)
    reply = await client.debugger_abort(thread, request_timeout=30.0)
    assert reply == {"status": "no_evaluation"}, reply

    await client.debugger_input(thread, ["continue"], request_timeout=30.0)
    assert await _wait_all_resumed(client)
    assert await _wait_settled(client)


# ── Probe 11bis: locals through the eval verb (gates the locals design) ────

@pytest.mark.asyncio
async def test_probe11bis_locals_through_the_eval_verb(prover):
    client, path = prover

    # First the re-exposure itself: if compiling the prelude's
    # Isabelle_MCP_PolyML failed, the prover died at startup and no test in
    # this file gets this far — reaching a hit IS the re-exposure probe.
    assert await _evaluate_through(client, path, DEFINER_END)
    await _enable_site_at(client, path, VAL_TOTAL, 4)
    await evaluate_to(client, path, -1)
    thread = await _wait_for_hit(client)

    # Ours: through the eval verb under debug_eval.
    ours = await client.debugger_print_vals(
        thread, timeout=60.0, request_timeout=120.0)
    assert ours["status"] == "ok", ours
    our_texts = _texts(ours)
    assert not any(t == "val it = (): unit" for t in our_texts), (
        f"the `val _` envelope leaked a result echo: {our_texts}")
    listing = "\n".join(our_texts)
    for name in ("n", "xs", "shift"):
        assert f"val {name} =" in listing, f"local {name} missing:\n{listing}"

    # Stock print_vals on the same frame, byte for byte.
    outputs_before = len(client.debugger_output_history)
    states_before = len(client.debugger_state_history)
    await client.debugger_input(
        thread, ["print_vals", "0", "false", ""], request_timeout=30.0)
    ok = await client.wait_debugger_event(
        lambda c: len(c.debugger_state_history) > states_before, timeout=90.0)
    assert ok, "the stock print_vals round trip never completed"
    stock_msgs = [
        m
        for params in client.debugger_output_history[outputs_before:]
        for m in params.get("messages", [])
    ]
    stock_texts = [m["text"] for m in stock_msgs]
    assert our_texts == stock_texts, (
        "locals through the eval verb differ from stock print_vals:\n"
        f"ours:  {our_texts!r}\nstock: {stock_texts!r}"
    )

    await client.debugger_input(thread, ["continue"], request_timeout=30.0)
    assert await _wait_all_resumed(client)
    assert await _wait_settled(client)


@pytest.mark.asyncio
async def test_probe11bis_per_value_bound_and_outer_deadline(prover):
    client, path = prover
    assert await _evaluate_through(client, path, SLOW_DEFINER_END)
    await _enable_site_at(client, path, VAL_ANSWER, 4)
    await evaluate_to(client, path, -1)
    thread = await _wait_for_hit(client)
    assert any("slow_target" in (f.get("function") or "")
               for f in client.debugger_threads[thread]), (
        client.debugger_threads[thread])

    # The slow value placeholders out at 5s; the rest still print.
    ours = await client.debugger_print_vals(
        thread, timeout=60.0, request_timeout=150.0)
    assert ours["status"] == "ok", ours
    listing = "\n".join(_texts(ours))
    assert "val sluggish = <printing timed out>" in listing, (
        f"the per-value bound did not fire:\n{listing[:2000]}")
    assert "val small =" in listing, f"the rest did not print:\n{listing[:2000]}"

    # The outer deadline still fires DURING a slow value (3s < the 5s per-value
    # bound): the whole listing ends as a TIMEOUT error, not a placeholder.
    result = await client.debugger_print_vals(
        thread, timeout=3.0, request_timeout=120.0)
    assert result["status"] == "ok", result
    texts = _texts(result)
    assert any("Isabelle_MCP.debug_eval: TIMEOUT" in t for t in texts), (
        f"the outer deadline was masked by the per-value check: {texts}")
    assert thread in client.debugger_threads

    await client.debugger_input(thread, ["continue"], request_timeout=30.0)
    assert await _wait_all_resumed(client)


# ── Probe 9: stepping ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probe9_stepping(prover):
    client, path = prover
    assert await _evaluate_through(client, path, DEFINER_END)
    await _enable_site_at(client, path, VAL_XS, 4)
    await evaluate_to(client, path, -1)
    thread = await _wait_for_hit(client)

    # A step from `val xs` stops again inside instrumented code.
    states_before = len(client.debugger_state_history)
    await client.debugger_input(thread, ["step"], request_timeout=30.0)
    ok = await client.wait_debugger_event(
        lambda c: len(c.debugger_state_history) > states_before
        and bool(c.debugger_threads.get(thread)),
        timeout=60.0,
    )
    print(f"\nPROBE 9 — step from `val xs`: stopped_again={ok}, "
          f"stack={client.debugger_threads.get(thread)}")

    # Step until execution leaves the instrumented region; both outcomes of the
    # design are normal, but it must END (no wedge), and the thread must
    # eventually be absent from the state (= resumed, hit retired).
    for _ in range(40):
        if not client.debugger_threads.get(thread):
            break
        states_before = len(client.debugger_state_history)
        await client.debugger_input(thread, ["step"], request_timeout=30.0)
        assert await client.wait_debugger_event(
            lambda c: len(c.debugger_state_history) > states_before,
            timeout=60.0,
        ), "a step round trip never completed"
    assert not client.debugger_threads.get(thread), (
        "stepping never left probe_target")

    # Stray-halt check (measured 2026-08-14: the anomaly is REAL).  The
    # stepping flag is thread-local, set by a bare assignment and cleared only
    # by a continue at a later break — so after stepping off the end, the
    # worker carries it into the next instrumented code it runs (slow_target,
    # no armed breakpoint anywhere near it) and stops there.  The design's
    # recovery is exact: sending continue clears the flag.
    stray = await client.wait_debugger_event(
        lambda c: any(c.debugger_threads.values()), timeout=60.0)
    print(f"\nPROBE 9 — stray halt after stepping off the end: {stray}, "
          f"threads={ {t: [f.get('function') for f in s] for t, s in client.debugger_threads.items()} }")
    if stray:
        parked = next(iter(client.debugger_threads))
        await client.debugger_input(parked, ["continue"], request_timeout=30.0)
        assert await _wait_all_resumed(client)
    assert await _wait_settled(client)
    assert not any(client.debugger_threads.values())


# ── Probes 6 and 14: recompilation, re-arming, edits while parked ──────────

async def _edit_on_disk(client, path, old, new):
    """Edit the file and push it the way production does: the probes bypass the
    MCP tool layer, so the tool-entry stat backstop (Layer 2) must be invoked
    explicitly — without it nothing ever reaches the prover (measured: the
    first draft of this probe 'passed' motion 2 on a hit that was really the
    caller's FIRST run, with every disk edit silently unsynced)."""
    with open(path) as f:
        text = f.read()
    assert old in text, f"edit target not found: {old!r}"
    with open(path, "w") as f:
        f.write(text.replace(old, new))
    await client.resync_changed_open_documents()


async def _continue_all(client):
    for parked in [t for t, s in client.debugger_threads.items() if s]:
        await client.debugger_input(parked, ["continue"], request_timeout=30.0)
    assert await _wait_all_resumed(client)


@pytest.mark.asyncio
async def test_probe6_upstream_edit_invalidates_serials_and_rearming_works(prover):
    client, path = prover

    # Baseline: motion 1, and the caller's FIRST run hits.
    assert await _evaluate_through(client, path, DEFINER_END)
    old_serial = await _enable_site_at(client, path, VAL_XS, 4)
    await evaluate_to(client, path, -1)
    await _wait_for_hit(client)
    await _continue_all(client)
    assert await _wait_settled(client)

    # A disk edit AFTER the definer reaches the prover as a minimal RANGED
    # didChange (since 2026-08-14; a range-less didChange was remove-all +
    # insert-all server-side, re-created every command, and killed every
    # serial on ANY edit).  The definer's serial therefore survives ARMED and
    # the re-run caller hits with NO re-arm — the design's motion 2.  The
    # timing probes (prefix reuse, multi-hunk sync) live in
    # test_ranged_sync_probes.py.
    await _edit_on_disk(
        client, path,
        "ML \\<open>val probe_result = probe_target 4\\<close>",
        "ML \\<open>val probe_result = probe_target 5\\<close>",
    )
    await evaluate_to(client, path, -1)
    hit_after_edit = await client.wait_debugger_event(
        lambda c: bool(c.debugger_threads), timeout=60.0)
    assert hit_after_edit, (
        "motion 2 regressed: the armed breakpoint did not survive a caller edit")
    await _continue_all(client)
    assert await _wait_settled(client)
    bps = await _breakpoints(client, path)
    assert any(bp["serial"] == old_serial and bp["state"] is True for bp in bps), (
        "the armed serial must survive a downstream edit, still armed")

    # An edit strictly BEFORE the definer still invalidates it — chained
    # execs; the ranged win is prefix-only.  The definer recompiles, fresh
    # sites come back unarmed, the stale serial is refused, and the re-run
    # caller does NOT stop.
    await _edit_on_disk(
        client, path,
        'lemma probe_lemma: "(x::nat) + 0 = x"',
        'lemma probe_lemma: "(x::nat) + 0 = x" (* touched *)',
    )
    await evaluate_to(client, path, -1)
    assert await _wait_settled(client)
    bps = await _breakpoints(client, path)
    serials = [bp["serial"] for bp in bps]
    print(f"\nPROBE 6 — sites after the upstream edit: "
          f"{[(_corrected(b), b['serial'], b['state']) for b in bps]}; "
          f"old_serial={old_serial}")
    assert serials, "the re-evaluated file must have sites again"
    assert old_serial not in serials, (
        "an upstream edit was expected to mint fresh serials")
    reply = await _toggle(client, path, old_serial, True)
    assert reply == {"status": "unknown_breakpoint"}, (
        f"toggling a stale serial must be refused: {reply}")

    # Motion 1 is the recovery on the edited file: edit, evaluate to the
    # definer, arm the NEW serial, evaluate onward — the caller hits.
    await _edit_on_disk(
        client, path,
        "ML \\<open>val probe_result = probe_target 5\\<close>",
        "ML \\<open>val probe_result = probe_target 6\\<close>",
    )
    assert await _evaluate_through(client, path, DEFINER_END)
    new_serial = await _enable_site_at(client, path, VAL_XS, 4)
    assert new_serial != old_serial, "recompilation must mint new serials"
    await evaluate_to(client, path, -1)
    thread = await _wait_for_hit(client)

    # Probe 14: a file-save resync edits the document while the thread is
    # parked (append blank lines at the very end — after everything).
    with open(path) as f:
        text = f.read()
    with open(path, "w") as f:
        f.write(text + "\n\n")
    await client.resync_changed_open_documents()
    await asyncio.sleep(5.0)
    parked = thread in client.debugger_threads and bool(
        client.debugger_threads[thread])
    print(f"\nPROBE 14 — after an edit while parked: thread still parked={parked}, "
          f"threads={list(client.debugger_threads)}")

    if parked:
        await client.debugger_input(thread, ["continue"], request_timeout=30.0)
        assert await _wait_all_resumed(client)


# ── Probe 7: cancellation's synthetic edit ─────────────────────────────────

@pytest.mark.asyncio
async def test_probe7_cancellation_sweeps_the_hit(prover):
    client, path = prover
    assert await _evaluate_through(client, path, DEFINER_END)
    old_serial = await _enable_site_at(client, path, VAL_XS, 4)
    await evaluate_to(client, path, -1)
    await _wait_for_hit(client)

    # isabelle_cancel_evaluation's path: force_interrupt (synthetic edit).
    await client.force_interrupt(path)

    # The parked thread must leave the hit table.
    resumed = await _wait_all_resumed(client, timeout=90.0)
    print(f"\nPROBE 7 — after cancel: all threads gone={resumed}, "
          f"threads={list(client.debugger_threads)}")
    assert resumed, "cancellation left a thread parked at the breakpoint"

    # Did the synthetic edit kill the serials?  (Decides the demote-after-cancel
    # trigger of design section 5.)  Measure, either outcome is information.
    reply = await _toggle(client, path, old_serial, False)
    print(f"\nPROBE 7 — toggling the pre-cancel serial afterwards: {reply}")
