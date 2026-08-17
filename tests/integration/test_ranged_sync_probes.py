"""Ranged-didChange probes, against a REAL prover — the permanent form of the
2026-08-14 three-arm experiment (plan R9).

What they pin: a disk edit now reaches the prover as MINIMAL ranged
contentChanges through the production sync path, so the evaluated prefix is
reused (design motion 2 works: an armed breakpoint strictly before the edit
survives, still armed, and hits with NO re-arm), while an upstream edit still
invalidates everything after it — chained execs; the win is prefix-only.

Sentinels make re-execution measurable: a 20s sleep before the definer (the
prefix) and a 10s sleep between the two edit targets (the middle).  Probe
policy as in test_debugger_probes: measure, positive controls, generous waits.

    PATH=contrib/Isabelle2025-2/bin:$PATH pytest tests/integration -m integration
"""
import logging
import os
import shutil
import time

import pytest

from isabelle_mcp import evaluation as ev
from isabelle_mcp.evaluation import evaluate_to
from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.utils import LSPCharacter, LSPLine, parse_command_output_html

from .test_debugger_probes import (
    _breakpoints,
    _continue_all,
    _edit_on_disk,
    _enable_site_at,
    _eval_at,
    _texts,
    _wait_for_hit,
    _wait_settled,
)

pytestmark = pytest.mark.integration

if shutil.which("isabelle") is None:
    pytest.skip("isabelle not on PATH", allow_module_level=True)


THEORY = r'''theory RangedSync
imports Main
begin

ML \<open>OS.Process.sleep (Time.fromSeconds 20)\<close>

ML \<open>
fun ranged_target (n: int) =
  let
    val total = n + 1;
  in total end;
\<close>

ML \<open>val ranged_result = ranged_target 4\<close>

ML \<open>OS.Process.sleep (Time.fromSeconds 10)\<close>

ML \<open>val ranged_other = 7\<close>

end
'''
VAL_TOTAL = 10        # site inside ranged_target
PREFIX_SLEEP = 20.0   # line 5, strictly before the definer
MIDDLE_SLEEP = 10.0   # line 16, between the two edit targets


@pytest.fixture
async def prover(tmp_path):
    ev.EVAL_POLL_INTERVAL = 4.0
    path = os.path.join(str(tmp_path), "RangedSync.thy")
    with open(path, "w") as f:
        f.write(THEORY)
    ev.evaluation_state = ev.EvaluationState()
    client = IsabelleLSPClient(
        logic="HOL",
        project_root=str(tmp_path),
        extra_args=["-o", "ML_debugger=true", "-o", "editor_tracing_messages=0"],
    )
    await client.start()
    try:
        yield client, path
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_ranged_sync_prefix_reuse_and_upstream_invalidation(prover):
    client, path = prover

    # Baseline: full evaluation (pays both sleeps), then arm the definer's site.
    await evaluate_to(client, path, -1)
    assert await _wait_settled(client, tries=90), "the theory never evaluated"
    serial = await _enable_site_at(client, path, VAL_TOTAL, 4)

    # Arm B — motion 2 through the production sync path: edit ONLY the caller.
    # The prefix (20s sleep + definer) must be reused: the hit arrives fast,
    # the serial survives, and it is STILL ARMED with no re-arm in between.
    t0 = time.monotonic()
    await _edit_on_disk(
        client, path,
        "ML \\<open>val ranged_result = ranged_target 4\\<close>",
        "ML \\<open>val ranged_result = ranged_target 5\\<close>",
    )
    await evaluate_to(client, path, -1)
    thread = await _wait_for_hit(client, timeout=60.0)
    t_hit = time.monotonic() - t0
    assert thread, "the still-armed breakpoint did not hit after a caller edit"
    assert t_hit < PREFIX_SLEEP, (
        f"prefix reuse failed: the hit took {t_hit:.1f}s, so the {PREFIX_SLEEP}s "
        f"prefix sentinel re-ran — the ranged didChange did not preserve the "
        f"evaluated prefix"
    )
    await _continue_all(client)
    assert await _wait_settled(client, tries=90)
    bps = await _breakpoints(client, path)
    survivor = next((bp for bp in bps if bp["serial"] == serial), None)
    assert survivor is not None, "the armed serial died on a downstream edit"
    assert survivor["state"] is True, "the surviving breakpoint lost its arming"

    # Arm C — the honest limit: an edit strictly BEFORE the prefix sentinel
    # invalidates everything after it (chained execs; the win is prefix-only).
    t1 = time.monotonic()
    await _edit_on_disk(client, path, "begin\n", "begin\n(* upstream touch *)\n")
    await evaluate_to(client, path, -1)
    assert await _wait_settled(client, tries=90)
    t_upstream = time.monotonic() - t1
    bps = await _breakpoints(client, path)
    assert all(bp["serial"] != serial for bp in bps), (
        "an upstream edit must re-create the definer and kill its serials"
    )
    assert t_upstream >= PREFIX_SLEEP, (
        f"expected the prefix sentinel to re-run after an upstream edit, "
        f"but settling took only {t_upstream:.1f}s"
    )


@pytest.mark.asyncio
async def test_two_distant_edits_arrive_as_one_multi_hunk_didchange(prover, caplog):
    client, path = prover
    # A rejected didChange surfaces ONLY as a type=1 log message (the recovery
    # hook then heals silently before settling, so no flag survives to assert
    # on) -- the whole-test log scan at the bottom is the rejection witness.
    caplog.set_level(logging.ERROR, logger="isabelle_mcp.lsp_client")

    await evaluate_to(client, path, -1)
    assert await _wait_settled(client, tries=90), "the theory never evaluated"
    serial = await _enable_site_at(client, path, VAL_TOTAL, 4)

    # Two distant edits in ONE sync — exactly what stat-backstop batching
    # produces.  Record the outgoing didChange to pin the multi-hunk shape
    # (a single contiguous range would rewrite the middle sentinel's command
    # and was rejected for that reason).
    sent: list[dict] = []
    original_notify = client.notify

    async def recording_notify(method, params):
        if method == "textDocument/didChange":
            sent.append(params)
        await original_notify(method, params)

    client.notify = recording_notify
    try:
        with open(path) as f:
            text = f.read()
        text = text.replace("ranged_target 4", "ranged_target 5")
        text = text.replace("val ranged_other = 7", "val ranged_other = 8")
        with open(path, "w") as f:
            f.write(text)
        t0 = time.monotonic()
        await client.resync_changed_open_documents()
    finally:
        client.notify = original_notify

    assert len(sent) == 1, f"expected one didChange, got {len(sent)}"
    changes = sent[0]["contentChanges"]
    assert len(changes) == 2 and all("range" in c for c in changes), changes
    assert changes[0]["range"]["start"]["line"] > changes[1]["range"]["start"]["line"], (
        f"hunks must descend: {changes}"
    )

    # Both edits take effect and the prefix is still reused: the armed site
    # hits fast on the re-run caller.
    await evaluate_to(client, path, -1)
    thread = await _wait_for_hit(client, timeout=60.0)
    t_hit = time.monotonic() - t0
    assert thread
    assert t_hit < PREFIX_SLEEP, (
        f"prefix reuse failed under a multi-hunk sync: hit after {t_hit:.1f}s"
    )
    # Hunk 1's semantic witness, in prover truth: at the hit, the frame's n is
    # the caller's NEW argument (5, not 4) — "hit arrived fast" alone shows
    # only that the caller re-ran, not what text it ran.
    result = await _eval_at(client, thread, "n * 100", timeout_s=30)
    assert result["status"] == "ok", result
    assert any("val it = 500: int" in t for t in _texts(result)), _texts(result)
    await _continue_all(client)
    assert await _wait_settled(client, tries=90)
    t_settled = time.monotonic() - t0
    # The middle sentinel sits between the two hunks; whether it re-runs is a
    # measurement (chained execs say yes), not a promise — record it.
    print(f"\nRANGED SYNC — two-hunk sync: hit at {t_hit:.1f}s, settled at "
          f"{t_settled:.1f}s (middle sentinel {'re-ran' if t_settled >= MIDDLE_SLEEP else 'was reused'})")
    bps = await _breakpoints(client, path)
    survivor = next((bp for bp in bps if bp["serial"] == serial), None)
    assert survivor is not None and survivor["state"] is True, (
        "the armed serial must survive two downstream hunks"
    )

    # Hunk 2's semantic witness, in prover truth: the ranged_other command's
    # rendered output shows the NEW value.  (Position-explicit request; no
    # caret move, no evaluation start.)
    read_back = await client.get_output_at_position(
        path, LSPLine(17), LSPCharacter(4))
    assert read_back is not None, "no command at the ranged_other line"
    _source, _rng, content_html = read_back
    messages = parse_command_output_html(content_html)
    assert any("ranged_other = 8" in m["text"] for m in messages), messages

    # The server's copy evaluates without a single failed command (a hunk
    # misapplied into ML text would break compilation server-side).
    assert ev._failed_count(client) == 0, "failed commands after the two-hunk sync"

    # Client-model hygiene only (witnesses no server state): the model equals
    # the on-disk text after the sync round trips.
    with open(path) as f:
        assert client.open_documents[path].content == f.read()

    # The rejection witness: a didChange the server could not apply logs
    # "Failed to apply document change" (type=1) and nothing else survives to
    # settle time — the recovery hook heals the flag before we could read it.
    rejections = [
        r for r in caplog.records
        if "Failed to apply document change" in r.getMessage()
    ]
    assert not rejections, rejections
