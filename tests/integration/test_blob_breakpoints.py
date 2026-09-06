r"""Blob (`.ML`) breakpoints, against a REAL prover.

A breakpoint whose site sits inside a `.ML` **blob** -- an auxiliary ML file loaded
by a `.thy` via `ML_file` -- must arm and stop exactly like a breakpoint in a `.thy`.

This pins the fix for a pre-existing bug: the debugger sent the blob file's OWN node
name paired with the breakable command's id, but a blob site's command belongs to the
LOADER theory's node (the `ML_file` command), so `Document.command_exec` raised and
every blob site answered `undefined` -- unarmable, while `.thy` sites always worked
because there the two nodes coincide.  A jar that sends `rendering.model.node_name`
instead of `command.node_name` (debugger.scala) fails HERE: the blob states read a
non-boolean `undefined` and the caller never stops.

    PATH=contrib/Isabelle2025-2/bin:$PATH pytest tests/integration -m integration \
        -k blob_breakpoint
"""
import asyncio
import os
import shutil

import pytest

from isabelle_mcp import evaluation as ev
from isabelle_mcp.evaluation import evaluate_to
from isabelle_mcp.lsp_client import IsabelleLSPClient

from .test_debugger_probes import (
    _breakpoints,
    _eval_at,
    _evaluate_through,
    _texts,
    _toggle,
    _wait_all_resumed,
    _wait_for_hit,
    _wait_settled,
)

pytestmark = pytest.mark.integration

if shutil.which("isabelle") is None:
    pytest.skip("isabelle not on PATH", allow_module_level=True)


# thy_target lives inline in the .thy (a positive control); blob_target lives in the
# blob Foo.ML (the file under test).  Evaluating through the ML_file line (11) compiles
# both without running either caller (13, 15), so a site can be armed before its call.
THEORY = r'''theory Blob imports Main begin

ML \<open>
fun thy_target (n: int) =
  let
    val ys = map (fn i => i * n) (1 upto 3);
    val tot = List.foldl (fn (i, acc) => acc + i) 0 ys;
  in tot end;
\<close>

ML_file "Foo.ML"

ML \<open>val thy_result = thy_target 5\<close>

ML \<open>val blob_result = blob_target 4\<close>

end
'''
BLOB = '''fun blob_target (n: int) =
  let
    val xs = map (fn i => i + n) (1 upto 3);
    val total = List.foldl (fn (i, acc) => acc + i) 0 xs;
  in total end;
'''
ML_FILE_LINE = 11       # ML_file "Foo.ML" -- both functions compiled, no caller run
THY_CALLER_LINE = 13    # thy_target 5
BLOB_CALLER_LINE = 15   # blob_target 4


@pytest.fixture
async def prover(tmp_path):
    ev.EVAL_POLL_INTERVAL = 4.0
    thy = os.path.join(str(tmp_path), "Blob.thy")
    blob = os.path.join(str(tmp_path), "Foo.ML")
    with open(thy, "w") as f:
        f.write(THEORY)
    with open(blob, "w") as f:
        f.write(BLOB)
    # Evaluation bookkeeping is module-global; do not inherit a leaked "active" run.
    ev.evaluation_state = ev.EvaluationState()
    client = IsabelleLSPClient(
        logic="HOL",
        project_root=str(tmp_path),
        debug=True,
        extra_args=["-o", "editor_tracing_messages=0"],
    )
    await client.start()
    try:
        yield client, thy, blob
    finally:
        await client.shutdown()


def _site_at(bps, start_line_0indexed, start_char=3):
    """Select a breakable site by its markup range START -- serials are not stable
    across compilations, ranges are."""
    for bp in bps:
        s = bp["range"]["start"]
        if s["line"] == start_line_0indexed and s["character"] == start_char:
            return bp
    return None


async def _arm_site_at(client, path, start_line_0indexed, *, tries=30):
    """Arm the breakable site at a markup-range start, absorbing the retryable
    `outdated` status (pending edits not yet incorporated) the same way
    `_breakpoints` does for the listing.  Serials are re-read on each attempt --
    they are not stable across re-incorporation; the range is."""
    reply: dict = {}
    for _ in range(tries):
        sites = await _breakpoints(client, path)
        site = _site_at(sites, start_line_0indexed)
        assert site is not None, [bp["range"]["start"] for bp in sites]
        reply = await _toggle(client, path, site["serial"], True)
        if reply.get("status") != "outdated":
            break
        await asyncio.sleep(1.0)
    return reply


@pytest.mark.asyncio
async def test_blob_breakpoint_arms_and_stops_inside_the_blob(prover):
    client, thy, blob = prover

    # Through the ML_file loader only: blob_target and thy_target compile; neither
    # caller has run, so an armed site can still catch the first call.
    assert await _evaluate_through(client, thy, ML_FILE_LINE), "the loader never evaluated"

    # The blob must be an open model for the prover to render its breakable markup.
    await client.open_document(blob)
    await asyncio.sleep(1.0)

    # REGRESSION CORE: every blob site carries a real boolean state, never `undefined`.
    # The wrong-node bug makes each state the literal string "undefined" instead.
    blob_sites = await _breakpoints(client, blob)
    assert blob_sites, "no breakable sites found in the blob"
    assert all(isinstance(bp["state"], bool) for bp in blob_sites), (
        "a blob site read a non-boolean state -- the wrong-node bug is back: "
        f"{[(bp['serial'], bp['state']) for bp in blob_sites]}"
    )

    # Arm the `val xs` site (Foo.ML line 3 -> 0-based range start line 2).
    reply = await _arm_site_at(client, blob, start_line_0indexed=2)
    assert reply.get("status") == "ok", f"blob toggle failed (wrong-node bug?): {reply}"
    assert isinstance(reply.get("was"), bool), reply  # a real prior value, never `undefined`/None

    # Run the blob caller: the armed site must STOP execution inside the blob.
    try:
        await evaluate_to(client, thy, BLOB_CALLER_LINE)
    except Exception:
        pass  # a hit stalls the run; evaluate_to may refuse -- the wait below is the truth
    thread = await _wait_for_hit(client, timeout=90.0)
    frame0 = client.debugger_threads[thread][0]
    assert frame0.get("function") == "blob_target", frame0
    assert str(frame0.get("file", "")).endswith("Foo.ML"), frame0
    # Prover truth: genuinely parked in the blob's own frame -- the parameter n is the
    # call's argument (blob_target 4), proving the stop is inside the blob's ML.
    result = await _eval_at(client, thread, "n")
    assert result.get("status") == "ok", result
    assert any("val it = 4: int" in t for t in _texts(result)), _texts(result)

    await client.debugger_input(thread, ["continue"], request_timeout=30.0)
    await _wait_all_resumed(client, timeout=60.0)
    await _wait_settled(client)


@pytest.mark.asyncio
async def test_thy_breakpoint_still_works_no_regression(prover):
    client, thy, _blob = prover
    assert await _evaluate_through(client, thy, ML_FILE_LINE), "the loader never evaluated"

    reply = await _arm_site_at(client, thy, start_line_0indexed=5)  # val ys, Blob.thy line 6
    assert reply.get("status") == "ok", reply

    try:
        await evaluate_to(client, thy, THY_CALLER_LINE)
    except Exception:
        pass
    thread = await _wait_for_hit(client, timeout=90.0)
    frame0 = client.debugger_threads[thread][0]
    assert frame0.get("function") == "thy_target", frame0
    result = await _eval_at(client, thread, "n")
    assert any("val it = 5: int" in t for t in _texts(result)), _texts(result)

    await client.debugger_input(thread, ["continue"], request_timeout=30.0)
    await _wait_all_resumed(client, timeout=60.0)
    await _wait_settled(client)
