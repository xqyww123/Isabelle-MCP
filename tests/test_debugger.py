"""Unit tests for the debugger registry, anchors, hits and sentences.

Pure Python, no prover: the wire is a FakeDebugClient scripted per test. The
sentence catalogue is pinned VERBATIM (the query.py convention) — a wording
change must be a conscious edit here, never an accident.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from isabelle_mcp import debugger, processing
from isabelle_mcp.debugger import (
    ARMED,
    PENDING,
    Breakpoint,
    DebuggerRegistry,
    Site,
    anchor_snippet,
    resolve_site,
    sites_from_listing,
)
from isabelle_mcp.utils.core import IsabelleToolError

THY = "/fake/DebugProbe.thy"
ML = "/fake/tools.ML"

# One ML block: an indented statement (site char 4), a column-1 statement
# (site char 0), a second indented one — the probe theory's shape.
CONTENT = "\n".join([
    "theory DebugProbe",            # line 1
    "imports Main",                 # line 2
    "begin",                        # line 3
    "ML \\<open>",                  # line 4
    "fun probe (n: int) =",         # line 5
    "  let",                        # line 6
    "    val xs = map (fn i => i + n) (1 upto 3);",   # line 7
    "val shift = n + 1;",           # line 8
    "    val total = n + shift;",   # line 9
    "  in total end;",              # line 10
    "\\<close>",                    # line 11
    "end",                          # line 12
])
VAL_XS, VAL_SHIFT, VAL_TOTAL = 7, 8, 9


def _bp(serial: int, line1: int, char: int, state: bool | str = False) -> dict:
    """A listing row whose range END is the corrected position (design 3.3:
    the markup is shifted one symbol left; the client anchors at the end)."""
    return {
        "serial": serial, "state": state,
        "range": {"start": {"line": line1 - 1, "character": max(0, char - 1)},
                  "end": {"line": line1 - 1, "character": char}},
    }


def _listing(*bps: dict, status: str = "ok", open_: bool = True) -> dict:
    return {"status": status, "open": open_, "breakpoints": list(bps)}


DEFAULT_SITES = (_bp(11, VAL_XS, 4), _bp(12, VAL_SHIFT, 0), _bp(13, VAL_TOTAL, 4))


class FakeTracker:
    def __init__(self, state: str = processing.PROCESSED,
                 unprocessed: list[tuple[int, int, int, int]] | None = None):
        self.state = state
        self.unprocessed = unprocessed or []

    def position_state(self, line0: int) -> str:
        return self.state

    def get_unprocessed_ranges(self):
        return list(self.unprocessed)


class FakeDebugClient:
    """Scripted wire: each debugger method pops its reply from a list (the
    last reply repeats), and records the calls it saw."""

    def __init__(self):
        self.debug = True
        self.project_root = None
        self.open_documents = {THY: SimpleNamespace(content=CONTENT)}
        self.debugger_state_history: list[dict] = []
        self.debugger_threads: dict[str, list[dict]] = {}
        self.tracker: FakeTracker | None = FakeTracker()
        self.listing_replies: list[dict] = [_listing(*DEFAULT_SITES)]
        self.toggle_replies: list[dict] = [{"status": "ok", "was": False}]
        self.eval_replies: list[dict] = [{"status": "ok", "messages": []}]
        self.abort_replies: list[dict] = [{"status": "no_evaluation"}]
        self.calls: list[tuple] = []
        # Optional hook run when a continue/step verb is delivered.
        self.on_input = None

    def get_processing_tracker(self, file_path):
        return self.tracker

    def _pop(self, replies: list[dict]) -> dict:
        return replies.pop(0) if len(replies) > 1 else replies[0]

    async def debugger_breakpoints(self, file_path, *, timeout, request_timeout):
        self.calls.append(("breakpoints", file_path, timeout, request_timeout))
        return self._pop(self.listing_replies)

    async def debugger_toggle_breakpoint(self, file_path, serial, state, *,
                                         timeout, request_timeout):
        self.calls.append(("toggle", file_path, serial, state))
        return self._pop(self.toggle_replies)

    async def debugger_eval(self, thread, expr, *, frame, timeout,
                            request_timeout):
        self.calls.append(("eval", thread, expr, frame, timeout,
                           request_timeout))
        return self._pop(self.eval_replies)

    async def debugger_print_vals(self, thread, *, frame, timeout,
                                  request_timeout):
        self.calls.append(("print_vals", thread, frame, timeout,
                           request_timeout))
        return self._pop(self.eval_replies)

    async def debugger_abort(self, thread, *, request_timeout):
        self.calls.append(("abort", thread))
        return self._pop(self.abort_replies)

    async def debugger_input(self, thread, verbs, *, request_timeout):
        self.calls.append(("input", thread, verbs))
        # Wire fidelity: on any resume verb the prover's debugger loop
        # exits, emitting a full map WITHOUT the thread, before any
        # re-stop state arrives.
        self.push_state({t: s for t, s in self.debugger_threads.items()
                         if t != thread})
        if self.on_input is not None:
            self.on_input(thread, verbs)
        return {"ok": True}

    async def wait_debugger_event(self, predicate, timeout=60.0):
        return predicate(self)

    # test helpers ------------------------------------------------------

    def push_state(self, threads: dict[str, list[dict]]) -> None:
        """Simulate one PIDE/debugger_state notification (full map)."""
        self.debugger_threads = {t: s for t, s in threads.items() if s}
        self.debugger_state_history.append({"threads": [
            {"thread": t, "stack": s} for t, s in threads.items() if s
        ]})


STACK = [{"function": "probe(1)xs-(1)", "pos": {"offset": "10"}}]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(debugger, "registry", DebuggerRegistry())
    return FakeDebugClient()


def _hit(client: FakeDebugClient, thread: str = "worker-3") -> debugger.Hit:
    client.push_state({thread: STACK})
    debugger.registry.sync_hits(client)
    return next(iter(debugger.registry.hits.values()))


# ── The sentence catalogue, verbatim ───────────────────────────────────


class TestSentenceCatalogue:
    def test_debug_off(self):
        assert debugger.DEBUG_OFF == (
            "Debugging is not enabled in this session. Call "
            "isabelle_terminate, then isabelle_launch with debug=true."
        )

    def test_set_breakpoint_refusals(self):
        assert debugger.NO_SITE_NOT_EVALUATED == (
            "There is no breakable site at {where} — that line has not been "
            "evaluated yet. Breakpoints can only be set on code the prover "
            "has already compiled, so evaluate the file first."
        )
        assert debugger.NO_SITE_NOT_EVALUATED_ML == (
            "There is no breakable site at {where} — that line has not been "
            "evaluated yet. Breakpoints can only be set on code the prover "
            "has already compiled, and a .ML file is compiled by the "
            "ML_file command that loads it, so evaluate that ML_file "
            "command first."
        )
        assert debugger.NO_SITE_STILL_RUNNING == (
            "The command at {where} is still evaluating; a breakpoint can "
            "be set only after it finishes. Retry in a few seconds."
        )
        assert debugger.NO_SITE_ON_LINE == (
            "The command at {where} has been evaluated, but the compiler "
            "placed no breakable site on that line. Breakable sites exist "
            "only inside ML code, at statement boundaries the compiler "
            "chooses."
        )
        assert debugger.NO_SITES_IN_FILE == (
            "This file has no breakable sites at all — it contains no ML "
            "code that was compiled in this prover."
        )
        assert debugger.AT_TEXT_NOT_ON_LINE == \
            "{at_text} does not occur on {where}."
        assert debugger.NO_SITE_AT_OR_BEFORE == (
            "There is no breakable site at or before {at_text} on {where}."
        )
        assert debugger.LINE_SITES_TAIL == (
            "Pass one of these as at_text, or omit at_text to use the first "
            "site on the line."
        )

    def test_reason_tags(self):
        assert debugger.TAG_NOT_EVALUATED == "not evaluated yet"
        assert debugger.TAG_STILL_EVALUATING == "still evaluating"
        assert debugger.TAG_CODE_NOT_FOUND == "code not found"
        assert debugger.TAG_WIRE_FAILURE == "state unknown, internal failure"

    def test_hit_sentences(self):
        assert debugger.NO_THREAD_STOPPED == "No thread is stopped."
        assert debugger.NO_HIT_LIVE == "No thread is stopped at a breakpoint."
        assert debugger.RETIRED_HIT == (
            "Hit {hit_id} has ended: {ending} Call isabelle_debug_state for "
            "the live hits."
        )
        assert debugger.ENDED_TERMINATED == "the prover was terminated."
        assert debugger.ENDED_CONTINUED == \
            "it was resumed by isabelle_continue_breakpoint."

    def test_eval_sentences(self):
        assert debugger.EMPTY_EXPR == (
            "expr is empty. Pass a single ML expression; a temporary "
            "binding is written let val x = … in … end."
        )
        assert debugger.EVAL_OUTSTANDING == (
            "The previous evaluation on this hit has not returned. Wait "
            "for it to finish and retry."
        )
        assert debugger.EVAL_NO_OUTPUT == \
            "The evaluation completed with no output."

    def test_abort_sentences(self):
        assert debugger.ABORT_OK == (
            "The evaluation has ended. The thread stays at the breakpoint, "
            "still debuggable."
        )
        assert debugger.ABORT_NOTHING == (
            "No evaluation is running on this hit — there is nothing to "
            "abort."
        )
        assert debugger.ABORT_UNCONFIRMED == (
            "The evaluation did not end within {seconds}s. Some ML code "
            "cannot be interrupted at all; isabelle_cancel_evaluation is "
            "the way out."
        )

    def test_merge_notices(self):
        assert debugger.NOTICE_MERGED == (
            "breakpoint {where_a} before {anchor_a} resolves to the same "
            "site as {where_b} before {anchor_b}; merged into one "
            "breakpoint")
        assert debugger.NOTICE_MERGED_DUPLICATE == (
            "duplicate breakpoint at {where} before {anchor} merged into "
            "one")
        assert not hasattr(debugger, "NOTICE_STRAY_HALT")

    def test_step_did_not_stop(self):
        assert debugger.STEP_DID_NOT_STOP == (
            "The thread resumed and did not stop again within {seconds}s — "
            "execution left the instrumented region (stepping only stops in "
            "ML compiled with debugging in this session). The hit has ended."
        )


# ── Anchor snippets (section 3.2) ──────────────────────────────────────


class TestAnchorSnippet:
    def test_carries_three_word_tokens(self):
        # "val" alone would already be unique here, but a bare `val` reads
        # the same on every line of a let block, so the snippet grows to
        # three word tokens (symbols ride along, uncounted).
        line = "    val xs = map (fn i => i + n) (1 upto 3);"
        assert anchor_snippet(line, 4) == "val xs = map"

    def test_extends_until_unique(self):
        line = "val a = g x; val b = g x"
        assert anchor_snippet(line, 0) == "val a = g"
        assert anchor_snippet(line, 13) == "val b = g"

    def test_column_one_statement(self):
        assert anchor_snippet("val shift = n + 1;", 0) == "val shift = n"

    def test_short_tail_takes_what_there_is_and_never_crosses_the_line(self):
        # Fewer than three word tokens left: take the rest of the line.
        assert anchor_snippet("f ();", 0) == "f ();"

    def test_keeps_original_spacing(self):
        line = "val  a = 1; val  b = 2"
        # Two spaces survive in the snippet: it is a source slice.
        assert anchor_snippet(line, 0) == "val  a = 1"

    def test_empty_tail(self):
        assert anchor_snippet("val x = 1;   ", 13) == ""

    def test_non_ascii_falls_back_to_whole_tail(self):
        line = "val α = f α; val b = 2"
        assert anchor_snippet(line, 0) == "val α = f α; val b = 2"

    def test_snippet_resolves_back_to_its_site(self):
        # The uniqueness rule guarantees the round trip: passing the snippet
        # back as at_text picks the same site.
        line = "val a = g x; val b = g x"
        sites = [Site(1, 1, 0, False, anchor_snippet(line, 0)),
                 Site(2, 1, 13, False, anchor_snippet(line, 13))]
        for site in sites:
            assert resolve_site(sites, line, site.anchor, "f:1") is site


# ── Listing parsing (corrected positions, section 3.3) ────────────────


class TestSitesFromListing:
    def test_corrects_at_range_end_and_sorts(self):
        lines = CONTENT.split("\n")
        # Deliberately out of order; a column-1 site crosses from the
        # previous line (start on line N-1, end at char 0 of N).
        reply = _listing(
            _bp(13, VAL_TOTAL, 4),
            {"serial": 12, "state": True,
             "range": {"start": {"line": VAL_SHIFT - 2, "character": 44},
                       "end": {"line": VAL_SHIFT - 1, "character": 0}}},
            _bp(11, VAL_XS, 4),
        )
        sites = sites_from_listing(reply, lines)
        assert [(s.serial, s.line, s.char) for s in sites] == [
            (11, VAL_XS, 4), (12, VAL_SHIFT, 0), (13, VAL_TOTAL, 4)]
        assert sites[0].anchor == "val xs = map"
        assert sites[1].state is True

    def test_malformed_rows_are_skipped(self):
        reply = _listing({"serial": 9, "range": {}, "state": False})
        assert sites_from_listing(reply, []) == []


# ── at_text resolution (section 3.1) ───────────────────────────────────


class TestResolveSite:
    LINE = "    val xs = f xs; val ys = f xs"
    SITES = [Site(1, 9, 4, False, "val xs"), Site(2, 9, 19, False, "val ys")]

    def test_omitted_takes_first_site(self):
        assert resolve_site(self.SITES, self.LINE, None, "f:9").serial == 1

    def test_nearest_before(self):
        assert resolve_site(self.SITES, self.LINE, "ys", "f:9").serial == 2

    def test_repetition_on_same_site_is_served(self):
        # "f xs" occurs twice but both occurrences are at/after the LAST
        # site's char only for the second; construct the harmless case:
        line = "    val c = f x + f x"
        sites = [Site(1, 9, 4, False, "val c")]
        assert resolve_site(sites, line, "f x", "f:9").serial == 1

    def test_ambiguous_occurrences_refused(self):
        with pytest.raises(IsabelleToolError) as exc:
            resolve_site(self.SITES, self.LINE, "f xs", "f:9")
        msg = str(exc.value)
        assert msg.startswith(
            "There is no breakable site at or before ‹f xs› on f:9.")
        assert "before ‹val xs›" in msg and "before ‹val ys›" in msg

    def test_at_text_not_on_line(self):
        with pytest.raises(IsabelleToolError) as exc:
            resolve_site(self.SITES, self.LINE, "zs", "f:9")
        assert str(exc.value).startswith("‹zs› does not occur on f:9.")

    def test_no_site_at_or_before(self):
        sites = [Site(2, 9, 19, False, "val ys")]
        with pytest.raises(IsabelleToolError) as exc:
            resolve_site(sites, self.LINE, "xs", "f:9")
        assert str(exc.value).startswith(
            "There is no breakable site at or before ‹xs› on f:9.")


# ── Hit bookkeeping ────────────────────────────────────────────────────


class TestHitSync:
    def test_new_hit_gets_id_and_notice(self, client):
        hit = _hit(client)
        assert hit.hit_id == "h1"
        notices = debugger.registry.drain_notices()
        assert notices == (
            "Debugger notices:\n"
            "- thread worker-3 stopped at a breakpoint — hit h1; inspect "
            "with isabelle_debug_state"
        )
        assert debugger.registry.drain_notices() is None  # delivered once

    def test_resume_retires_with_notice(self, client):
        hit = _hit(client)
        debugger.registry.drain_notices()
        client.push_state({})
        debugger.registry.sync_hits(client)
        assert debugger.registry.hits == {}
        assert debugger.registry.retired[hit.hit_id] == debugger.ENDED_RESUMED
        assert "hit h1 ended: its thread resumed and is no longer stopped." \
            in (debugger.registry.drain_notices() or "")

    def test_resume_and_restop_is_a_new_hit(self, client):
        _hit(client)
        client.push_state({})
        client.push_state({"worker-3": STACK})
        debugger.registry.sync_hits(client)
        assert list(debugger.registry.hits) == ["h2"]

    def test_stepping_suppresses_transient_retirement(self, client):
        hit = _hit(client)
        hit.stepping = True
        client.push_state({})                    # transient absence
        client.push_state({"worker-3": STACK})   # restopped
        debugger.registry.sync_hits(client)
        assert list(debugger.registry.hits) == [hit.hit_id]
        assert hit.stepping is False             # cleared by the restop

    def test_attributed_ending_wins_and_emits_no_generic_notice(self, client):
        hit = _hit(client)
        debugger.registry.drain_notices()
        hit.pending_ending = debugger.ENDED_CONTINUED
        client.push_state({})
        debugger.registry.sync_hits(client)
        assert debugger.registry.retired[hit.hit_id] == debugger.ENDED_CONTINUED
        assert debugger.registry.drain_notices() is None

    def test_teardown_retires_hits_and_demotes_entries(self, client):
        hit = _hit(client)
        debugger.registry.entries.append(Breakpoint(
            file_path=THY, line=VAL_XS, anchor="val", state=ARMED, serial=11))
        debugger.registry.drain_notices()
        debugger.registry.on_prover_teardown()
        assert debugger.registry.hits == {}
        assert debugger.registry.retired[hit.hit_id] == \
            debugger.ENDED_TERMINATED
        entry = debugger.registry.entries[0]
        assert entry.state == PENDING and entry.serial is None
        assert entry.reason == debugger.TAG_NOT_EVALUATED
        notices = debugger.registry.drain_notices() or ""
        assert "hit h1 ended: the prover was terminated." in notices
        assert "no longer works (not evaluated yet)" in notices
        assert debugger.registry._consumed == 0


class TestResolveHit:
    def test_unknown_id(self, client):
        with pytest.raises(IsabelleToolError) as exc:
            debugger.registry.resolve_hit("h9")
        assert str(exc.value) == (
            "There is no hit h9. Call isabelle_debug_state for the live "
            "hits.")

    def test_retired_id_names_the_ending(self, client):
        hit = _hit(client)
        client.push_state({})
        debugger.registry.sync_hits(client)
        with pytest.raises(IsabelleToolError) as exc:
            debugger.registry.resolve_hit(hit.hit_id)
        assert str(exc.value) == (
            "Hit h1 has ended: its thread resumed and is no longer stopped. "
            "Call isabelle_debug_state for the live hits.")

    def test_omitted_with_one_live(self, client):
        hit = _hit(client)
        assert debugger.registry.resolve_hit(None) is hit

    def test_omitted_with_none(self, client):
        with pytest.raises(IsabelleToolError) as exc:
            debugger.registry.resolve_hit(None)
        assert str(exc.value) == debugger.NO_HIT_LIVE

    def test_omitted_with_several_lists_them(self, client):
        client.push_state({"worker-3": STACK, "worker-7": STACK})
        debugger.registry.sync_hits(client)
        with pytest.raises(IsabelleToolError) as exc:
            debugger.registry.resolve_hit(None)
        msg = str(exc.value)
        assert msg.startswith("Several hits are live; pass hit_id:")
        assert "thread worker-3" in msg and "thread worker-7" in msg


# ── set_breakpoint (section 4.2) ───────────────────────────────────────


class TestSetBreakpoint:
    @pytest.mark.asyncio
    async def test_happy_path_arms_and_records(self, client):
        out = await debugger.set_breakpoint(client, THY, VAL_XS, None)
        assert out == (
            f"Breakpoint set and armed: {THY}:{VAL_XS} before "
            f"‹val xs = map›.")
        [entry] = debugger.registry.entries
        assert entry.state == ARMED and entry.serial == 11
        assert entry.enabled is True and entry.anchor == "val xs = map"
        assert ("toggle", THY, 11, True) in client.calls

    @pytest.mark.asyncio
    async def test_debug_off_fails_fast(self, client):
        client.debug = False
        with pytest.raises(IsabelleToolError) as exc:
            await debugger.set_breakpoint(client, THY, VAL_XS, None)
        assert str(exc.value) == debugger.DEBUG_OFF
        assert client.calls == []

    @pytest.mark.asyncio
    async def test_not_evaluated_line(self, client):
        client.tracker = FakeTracker(processing.NOT_EVALUATED)
        with pytest.raises(IsabelleToolError) as exc:
            await debugger.set_breakpoint(client, THY, 12, None)
        assert str(exc.value) == debugger.NO_SITE_NOT_EVALUATED.format(
            where=f"{THY}:12")

    @pytest.mark.asyncio
    async def test_running_line_reports_still_evaluating_even_with_a_hit(
            self, client):
        # No paused-at-a-hit variant (user decision 2026-08-18): a live hit
        # must not change the answer — parallel workers make the "stuck"
        # guess wrong, and "retry" is never misleading.
        client.tracker = FakeTracker(processing.RUNNING)
        _hit(client)
        with pytest.raises(IsabelleToolError) as exc:
            await debugger.set_breakpoint(client, THY, 12, None)
        assert str(exc.value) == \
            debugger.NO_SITE_STILL_RUNNING.format(where=f"{THY}:12")

    @pytest.mark.asyncio
    async def test_evaluated_line_without_site_lists_nearest(self, client):
        with pytest.raises(IsabelleToolError) as exc:
            await debugger.set_breakpoint(client, THY, VAL_SHIFT + 3, None)
        msg = str(exc.value)
        assert msg.startswith(
            f"The command at {THY}:{VAL_SHIFT + 3} has been evaluated, but "
            f"the compiler placed no breakable site on that line.")
        assert f"line {VAL_XS} before ‹val xs = map›" in msg
        assert "Pass one of these as line + at_text." in msg

    @pytest.mark.asyncio
    async def test_no_sites_in_whole_file(self, client):
        client.listing_replies = [_listing()]
        with pytest.raises(IsabelleToolError) as exc:
            await debugger.set_breakpoint(client, THY, 12, None)
        assert str(exc.value).endswith(debugger.NO_SITES_IN_FILE)

    @pytest.mark.asyncio
    async def test_unfinished_site_reports_still_running(self, client):
        client.listing_replies = [_listing(_bp(11, VAL_XS, 4, "unfinished"))]
        with pytest.raises(IsabelleToolError) as exc:
            await debugger.set_breakpoint(client, THY, VAL_XS, None)
        assert str(exc.value) == debugger.NO_SITE_STILL_RUNNING.format(
            where=f"{THY}:{VAL_XS}")
        assert not any(c[0] == "toggle" for c in client.calls)

    @pytest.mark.asyncio
    async def test_toggle_timeout_records_nothing(self, client):
        client.toggle_replies = [{"status": "timeout"}]
        with pytest.raises(IsabelleToolError) as exc:
            await debugger.set_breakpoint(client, THY, VAL_XS, None)
        assert str(exc.value) == debugger.ARMING_TIMED_OUT.format(seconds=30)
        assert debugger.registry.entries == []

    @pytest.mark.asyncio
    async def test_file_not_open(self, client):
        client.listing_replies = [_listing(open_=False)]
        with pytest.raises(IsabelleToolError) as exc:
            await debugger.set_breakpoint(client, THY, VAL_XS, None)
        assert str(exc.value) == debugger.FILE_NOT_OPEN_IN_PROVER.format(
            file=THY)

    @pytest.mark.asyncio
    async def test_outdated_listing_is_retried(self, client):
        client.listing_replies = [
            _listing(status="outdated"), _listing(*DEFAULT_SITES)]
        out = await debugger.set_breakpoint(client, THY, VAL_XS, None)
        assert out.startswith("Breakpoint set and armed:")

    @pytest.mark.asyncio
    async def test_same_site_twice_stays_one_entry(self, client):
        await debugger.set_breakpoint(client, THY, VAL_XS, None)
        await debugger.set_breakpoint(client, THY, VAL_XS, None)
        assert len(debugger.registry.entries) == 1

    @pytest.mark.asyncio
    async def test_field_identical_twins_merge_to_the_surviving_object(
            self, client):
        # F1: with value equality, list.remove would delete the twin the
        # code means to KEEP and mutate a detached object instead.
        for _ in range(2):
            debugger.registry.entries.append(
                Breakpoint(file_path=THY, line=VAL_XS, anchor="val xs = map",
                           state=PENDING,
                           reason=debugger.TAG_NOT_EVALUATED))
        await debugger.set_breakpoint(client, THY, VAL_XS, None)
        assert len(debugger.registry.entries) == 1
        entry = debugger.registry.entries[0]
        assert entry.state == ARMED
        assert entry.serial == 11
        assert (
            "duplicate breakpoint at /fake/DebugProbe.thy:7 before "
            "\u2039val xs = map\u203a merged into one"
        ) in (debugger.registry.drain_notices() or "")
        client.calls.clear()
        await debugger.disable_all_breakpoints(client, None)
        assert ("toggle", THY, 11, False) in client.calls

    @pytest.mark.asyncio
    async def test_recompiled_site_rebinds_the_entry_not_a_twin(self, client):
        # F2: an ARMED entry whose serial died must be rebound, not
        # twinned; its abandoned serial is switched off first.
        await debugger.set_breakpoint(client, THY, VAL_XS, None)
        assert debugger.registry.entries[0].serial == 11
        client.listing_replies = [_listing(
            _bp(21, VAL_XS, 4), _bp(22, VAL_SHIFT, 0), _bp(23, VAL_TOTAL, 4))]
        await debugger.set_breakpoint(client, THY, VAL_XS, None)
        assert len(debugger.registry.entries) == 1
        entry = debugger.registry.entries[0]
        assert entry.state == ARMED
        assert entry.serial == 21
        assert ("toggle", THY, 11, False) in client.calls

    @pytest.mark.asyncio
    async def test_empty_anchor_is_never_a_merge_key(self, client):
        # F2: a site past the held content carries the degenerate empty
        # anchor; an empty-anchored entry must not merge with it.
        client.listing_replies = [_listing(_bp(31, 20, 0))]
        debugger.registry.entries.append(
            Breakpoint(file_path=THY, line=20, anchor="",
                       state=PENDING, reason=debugger.TAG_NOT_EVALUATED))
        await debugger.set_breakpoint(client, THY, 20, None)
        assert len(debugger.registry.entries) == 2

    @pytest.mark.asyncio
    async def test_other_sites_on_the_line_are_named(self, client):
        client.listing_replies = [_listing(
            _bp(21, VAL_XS, 4), _bp(22, VAL_XS, 13))]
        out = await debugger.set_breakpoint(client, THY, VAL_XS, None)
        assert "Other breakable sites on this line:" in out


# ── del_breakpoints (section 4.3) ──────────────────────────────────────


class TestDelBreakpoints:
    @pytest.mark.asyncio
    async def test_delete_disarms_and_reports(self, client):
        await debugger.set_breakpoint(client, THY, VAL_XS, None)
        out = await debugger.del_breakpoints(
            client, [(THY, VAL_XS, None)])
        assert out == "deleted 1 breakpoint"
        assert debugger.registry.entries == []
        assert ("toggle", THY, 11, False) in client.calls

    @pytest.mark.asyncio
    async def test_no_match_is_reported(self, client):
        out = await debugger.del_breakpoints(client, [(THY, 99, None)])
        assert out == (
            f"deleted 0 breakpoints\n"
            f"breakpoint not found — call isabelle_list_breakpoints to see "
            f"the current breakpoints:\n"
            f"  {THY}:99")

    @pytest.mark.asyncio
    async def test_ambiguous_is_skipped_never_guessed(self, client):
        debugger.registry.entries += [
            Breakpoint(file_path=THY, line=VAL_XS, anchor="val a"),
            Breakpoint(file_path=THY, line=VAL_XS, anchor="val b"),
        ]
        out = await debugger.del_breakpoints(client, [(THY, VAL_XS, None)])
        assert (f"matches several breakpoints, not deleted — pass at_text "
                f"to say which one:\n  {THY}:{VAL_XS}") in out
        assert len(debugger.registry.entries) == 2

    @pytest.mark.asyncio
    async def test_anchor_disambiguates(self, client):
        debugger.registry.entries += [
            Breakpoint(file_path=THY, line=VAL_XS, anchor="val a"),
            Breakpoint(file_path=THY, line=VAL_XS, anchor="val b"),
        ]
        out = await debugger.del_breakpoints(client, [(THY, VAL_XS, "val b")])
        assert out == "deleted 1 breakpoint"
        assert [e.anchor for e in debugger.registry.entries] == ["val a"]


# ── list_breakpoints (section 4.4) ─────────────────────────────────────


class TestListBreakpoints:
    def test_empty(self, client):
        assert debugger.list_breakpoints(client, None) == \
            "No breakpoints are registered."

    def test_rows(self, client):
        debugger.registry.entries += [
            Breakpoint(file_path=THY, line=VAL_XS, anchor="val",
                       state=ARMED, serial=11),
            Breakpoint(file_path=THY, line=VAL_SHIFT, anchor="val shift",
                       enabled=False, state=PENDING,
                       reason=debugger.TAG_NOT_EVALUATED),
        ]
        assert debugger.list_breakpoints(client, None) == (
            "breakpoints:\n"
            f"  - {THY}:{VAL_XS} before ‹val›, enabled, armed\n"
            f"  - {THY}:{VAL_SHIFT} before ‹val shift›, disabled, "
            "pending (not evaluated yet)"
        )


# ── list_breakable_sites (section 4.5) ─────────────────────────────────


class TestListBreakableSites:
    @pytest.mark.asyncio
    async def test_overlay_three_values(self, client):
        debugger.registry.entries += [
            Breakpoint(file_path=THY, line=VAL_XS, anchor="val",
                       state=ARMED, serial=11),
            Breakpoint(file_path=THY, line=VAL_SHIFT, anchor="val",
                       enabled=False, state=ARMED, serial=12),
        ]
        out = await debugger.list_breakable_sites(client, THY, None, None)
        assert out == (
            "sites:\n"
            f"  - line {VAL_XS} before ‹val xs = map›, already enabled\n"
            f"  - line {VAL_SHIFT} before ‹val shift = n›, already set but "
            f"disabled\n"
            f"  - line {VAL_TOTAL} before ‹val total = n›, breakable"
        )

    @pytest.mark.asyncio
    async def test_not_evaluated_ranges_are_named(self, client):
        client.tracker = FakeTracker(
            unprocessed=[(9, 0, 11, 5)])  # 0-indexed: lines 10-12
        out = await debugger.list_breakable_sites(client, THY, None, None)
        assert ("not_evaluated: lines 10-12 — evaluate up to those lines to "
                "see their sites") in out

    @pytest.mark.asyncio
    async def test_empty_range_when_evaluated(self, client):
        out = await debugger.list_breakable_sites(client, THY, 1, 3)
        assert out == "No breakable sites in lines 1-3."

    @pytest.mark.asyncio
    async def test_truncation_names_the_resume_line(self, client):
        many = [_bp(100 + i, VAL_XS, 4 + i) for i in range(45)]
        client.open_documents[THY] = SimpleNamespace(
            content="\n".join(CONTENT.split("\n")[:VAL_XS - 1]
                              + ["x" * 60] + CONTENT.split("\n")[VAL_XS:]))
        client.listing_replies = [_listing(*many)]
        out = await debugger.list_breakable_sites(client, THY, None, None)
        assert (f"truncated: showing 40 of 45 sites, line {VAL_XS}"
                f" — narrow the range (e.g. start_line {VAL_XS + 1}) to see "
                f"the rest") in out


# ── enable/disable all (sections 4.6/4.7) ──────────────────────────────


class TestEnableDisableAll:
    @pytest.mark.asyncio
    async def test_enable_arms_pending_by_anchor_and_updates_line(self, client):
        debugger.registry.entries.append(Breakpoint(
            file_path=THY, line=VAL_XS - 1, anchor="val xs",
            state=PENDING, reason=debugger.TAG_NOT_EVALUATED))
        out = await debugger.enable_all_breakpoints(client, None)
        [entry] = debugger.registry.entries
        assert entry.state == ARMED and entry.serial == 11
        assert entry.line == VAL_XS      # nearest site re-anchors the line
        assert "Armed 1:" in out
        assert ("toggle", THY, 11, True) in client.calls

    @pytest.mark.asyncio
    async def test_enable_reports_pending_reasons(self, client):
        debugger.registry.entries.append(Breakpoint(
            file_path=THY, line=99, anchor="no such code",
            state=PENDING, reason=debugger.TAG_NOT_EVALUATED))
        out = await debugger.enable_all_breakpoints(client, None)
        assert out.startswith("No breakpoint could be armed.")
        assert "Still pending: 1 (code not found" in out

    @pytest.mark.asyncio
    async def test_listing_failure_leaves_the_state_unknown(self, client):
        # A prover failure word tells us nothing about the sites, so the
        # honest tag is the unknown-state one, not "not evaluated yet".
        debugger.registry.entries.append(Breakpoint(
            file_path=THY, line=VAL_XS, anchor="val xs = map",
            state=ARMED, serial=11))
        client.listing_replies = [_listing(status="crashed")]
        out = await debugger.enable_all_breakpoints(client, None)
        assert debugger.registry.entries[0].reason == \
            debugger.TAG_WIRE_FAILURE
        assert "Still pending: 1 (state unknown, internal failure)" in out

    @pytest.mark.asyncio
    async def test_file_not_open_is_reported_as_not_evaluated(self, client):
        # The one listing failure whose cause IS known.
        debugger.registry.entries.append(Breakpoint(
            file_path=THY, line=VAL_XS, anchor="val xs = map",
            state=ARMED, serial=11))
        client.listing_replies = [_listing(open_=False)]
        out = await debugger.enable_all_breakpoints(client, None)
        assert debugger.registry.entries[0].reason == \
            debugger.TAG_NOT_EVALUATED
        assert "Still pending: 1 (not evaluated yet)" in out

    @pytest.mark.asyncio
    async def test_enable_empty_registry(self, client):
        assert await debugger.enable_all_breakpoints(client, None) == \
            "No breakpoints are registered."

    @pytest.mark.asyncio
    async def test_disable_switches_off_and_keeps_entries(self, client):
        await debugger.set_breakpoint(client, THY, VAL_XS, None)
        out = await debugger.disable_all_breakpoints(client, None)
        assert out == (
            "Switched off 1 armed breakpoint; 0 pending entries also marked "
            "disabled. Re-enable with isabelle_enable_all_breakpoints.")
        [entry] = debugger.registry.entries
        assert entry.state == ARMED and entry.enabled is False
        assert ("toggle", THY, 11, False) in client.calls

    @pytest.mark.asyncio
    async def test_merge_notice_when_two_entries_hit_one_site(self, client):
        debugger.registry.entries += [
            Breakpoint(file_path=THY, line=VAL_XS, anchor="val",
                       state=PENDING, reason=debugger.TAG_NOT_EVALUATED),
            Breakpoint(file_path=THY, line=VAL_XS, anchor="val xs",
                       state=PENDING, reason=debugger.TAG_NOT_EVALUATED),
        ]
        await debugger.enable_all_breakpoints(client, None)
        assert len(debugger.registry.entries) == 1
        assert (
            "breakpoint /fake/DebugProbe.thy:7 before \u2039val xs\u203a "
            "resolves to the same site as /fake/DebugProbe.thy:7 before "
            "\u2039val\u203a; merged into one breakpoint"
        ) in (debugger.registry.drain_notices() or "")

    @pytest.mark.asyncio
    async def test_duplicate_position_merge_says_duplicate(self, client):
        for _ in range(2):
            debugger.registry.entries.append(
                Breakpoint(file_path=THY, line=VAL_XS, anchor="val xs = map",
                           state=PENDING,
                           reason=debugger.TAG_NOT_EVALUATED))
        await debugger.enable_all_breakpoints(client, None)
        assert len(debugger.registry.entries) == 1
        assert (
            "duplicate breakpoint at /fake/DebugProbe.thy:7 before "
            "\u2039val xs = map\u203a merged into one"
        ) in (debugger.registry.drain_notices() or "")


# ── debug_state, eval, locals (sections 4.8-4.10) ──────────────────────


class TestDebugState:
    def test_no_hits(self, client):
        assert debugger.debug_state(client) == "No thread is stopped."

    def test_one_hit_block(self, client):
        _hit(client)
        out = debugger.debug_state(client)
        assert out == (
            "1 hit is live.\n\n"
            "Hit h1: thread worker-3.\n"
            "Call stack (innermost first; the number is the frame "
            "parameter):\n"
            "  frame 0  probe(1)xs-(1)"
        )

    def test_frame_position_shown_when_present(self, client):
        client.push_state({"worker-3": [
            {"function": "lookup", "file": "~~/src/Pure/thm.ML",
             "line": 14, "pos": {}}]})
        debugger.registry.sync_hits(client)
        assert "  frame 0  lookup  ~~/src/Pure/thm.ML:14" in \
            debugger.debug_state(client)


class TestEvalAtBreakpoint:
    @pytest.mark.asyncio
    async def test_wire_params_and_output(self, client):
        hit = _hit(client)
        client.eval_replies = [{"status": "ok", "messages": [
            {"kind": "writeln", "text": "val it = 2: int"},
            {"kind": "warning", "text": "careful"},
        ]}]
        out = await debugger.eval_at_breakpoint(
            client, "1 + 1", hit.hit_id, 0, 180.0)
        assert out == "val it = 2: int\n[warning] careful"
        assert ("eval", "worker-3", "1 + 1", 0, 180.0, 240.0) in client.calls

    @pytest.mark.asyncio
    async def test_empty_expr_refused_before_the_wire(self, client):
        _hit(client)
        with pytest.raises(IsabelleToolError) as exc:
            await debugger.eval_at_breakpoint(client, "   ", None, 0, 180.0)
        assert str(exc.value) == debugger.EMPTY_EXPR
        assert not any(c[0] == "eval" for c in client.calls)

    @pytest.mark.asyncio
    async def test_no_output_sentence(self, client):
        hit = _hit(client)
        out = await debugger.eval_at_breakpoint(
            client, "()", hit.hit_id, 0, 180.0)
        assert out == debugger.EVAL_NO_OUTPUT

    @pytest.mark.asyncio
    async def test_second_eval_refused_while_first_outstanding(self, client):
        hit = _hit(client)
        hit.eval_task = asyncio.get_running_loop().create_task(
            asyncio.sleep(30))
        try:
            with pytest.raises(IsabelleToolError) as exc:
                await debugger.eval_at_breakpoint(
                    client, "1", hit.hit_id, 0, 180.0)
            assert str(exc.value) == debugger.EVAL_OUTSTANDING
        finally:
            hit.eval_task.cancel()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status,expected", [
        ("timeout", debugger.EVAL_BACKSTOP_TIMEOUT.format(seconds=180)),
        ("busy", debugger.EVAL_BUSY),
        ("resumed", debugger.EVAL_RESUMED),
        ("crashed", debugger.EVAL_CRASHED),
    ])
    async def test_failure_statuses_become_their_sentences(
            self, client, status, expected):
        hit = _hit(client)
        client.eval_replies = [{"status": status}]
        with pytest.raises(IsabelleToolError) as exc:
            await debugger.eval_at_breakpoint(
                client, "1", hit.hit_id, 0, 180.0)
        assert str(exc.value) == expected

    @pytest.mark.asyncio
    async def test_locals_wire_and_render(self, client):
        hit = _hit(client)
        client.eval_replies = [{"status": "ok", "messages": [
            {"kind": "writeln", "text": 'val n = 4: int'}]}]
        out = await debugger.locals_at_breakpoint(client, hit.hit_id, 0, 60.0)
        assert out == "val n = 4: int"
        assert ("print_vals", "worker-3", 0, 60.0, 120.0) in client.calls


# ── continue / step / abort (sections 4.11-4.13) ───────────────────────


class TestContinueBreakpoint:
    @pytest.mark.asyncio
    async def test_resume_one(self, client):
        hit = _hit(client)
        out = await debugger.continue_breakpoint(client, hit.hit_id)
        assert out == "Resumed hit h1 (thread worker-3)."
        assert debugger.registry.hits == {}
        assert debugger.registry.retired["h1"] == debugger.ENDED_CONTINUED

    @pytest.mark.asyncio
    async def test_omitted_resumes_all(self, client):
        client.push_state({"worker-3": STACK, "worker-7": STACK})
        debugger.registry.sync_hits(client)
        out = await debugger.continue_breakpoint(client, None)
        assert out.startswith("Resumed 2 hits:")
        assert debugger.registry.hits == {}

    @pytest.mark.asyncio
    async def test_none_live(self, client):
        assert await debugger.continue_breakpoint(client, None) == \
            debugger.NO_HIT_LIVE


class TestStepAtBreakpoint:
    @pytest.mark.asyncio
    async def test_stopped_again_keeps_the_hit(self, client):
        hit = _hit(client)
        debugger.registry.drain_notices()
        new_stack = [{"function": "probe(1)total-(1)", "pos": {}}]

        def on_input(thread, verbs):
            client.push_state({thread: new_stack})
        client.on_input = on_input
        out = await debugger.step_at_breakpoint(client, "step", hit.hit_id)
        assert out.startswith("Hit h1 stopped again.")
        assert "probe(1)total-(1)" in out       # the post-step stack
        assert "probe(1)xs-(1)" not in out      # not the pre-step one
        assert list(debugger.registry.hits) == ["h1"]
        assert "h1" not in debugger.registry.retired
        # The transient absence between the two states is the step itself:
        # no hit-ended or new-hit notice may leak from it.
        assert debugger.registry.drain_notices() is None
        assert ("input", "worker-3", ["step"]) in client.calls

    @pytest.mark.asyncio
    async def test_did_not_stop_retires_the_hit(self, client):
        hit = _hit(client)
        out = await debugger.step_at_breakpoint(client, "step_out", hit.hit_id)
        assert out == debugger.STEP_DID_NOT_STOP.format(seconds=30)
        assert debugger.registry.retired["h1"] == debugger.ENDED_STEP_LEFT

    @pytest.mark.asyncio
    async def test_bad_mode(self, client):
        _hit(client)
        with pytest.raises(IsabelleToolError):
            await debugger.step_at_breakpoint(client, "leap", None)

    @pytest.mark.asyncio
    async def test_failed_verb_request_clears_the_stepping_flag(self, client):
        hit = _hit(client)

        async def boom(thread, verbs, *, request_timeout):
            raise IsabelleToolError("wire died")
        client.debugger_input = boom
        with pytest.raises(IsabelleToolError):
            await debugger.step_at_breakpoint(client, "step", hit.hit_id)
        assert hit.stepping is False


class TestAbortEval:
    @pytest.mark.asyncio
    async def test_nothing_outstanding_is_an_error(self, client):
        hit = _hit(client)
        with pytest.raises(IsabelleToolError) as exc:
            await debugger.abort_eval_at_breakpoint(client, hit.hit_id)
        assert str(exc.value) == debugger.ABORT_NOTHING

    @pytest.mark.asyncio
    async def test_target_settles_confirms(self, client, monkeypatch):
        # The outstanding evaluation's own reply arrives DURING the abort's
        # wait — the settlement signal that stops the re-sending.
        monkeypatch.setattr(debugger, "ABORT_PERIOD", 0.2)
        hit = _hit(client)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        loop.call_later(
            0.05, future.set_result, {"status": "ok", "messages": []})
        hit.eval_task = asyncio.ensure_future(future)
        client.abort_replies = [{"status": "aborting"}]
        out = await debugger.abort_eval_at_breakpoint(client, hit.hit_id)
        assert out == debugger.ABORT_OK
        assert sum(1 for c in client.calls if c[0] == "abort") == 1

    @pytest.mark.asyncio
    async def test_debt_case_resends_until_no_evaluation(
            self, client, monkeypatch):
        monkeypatch.setattr(debugger, "ABORT_PERIOD", 0.01)
        hit = _hit(client)
        client.abort_replies = [
            {"status": "aborting"}, {"status": "aborting"},
            {"status": "no_evaluation"}]
        out = await debugger.abort_eval_at_breakpoint(client, hit.hit_id)
        assert out == debugger.ABORT_OK
        assert sum(1 for c in client.calls if c[0] == "abort") == 3

    @pytest.mark.asyncio
    async def test_unconfirmed_after_the_bound(self, client, monkeypatch):
        monkeypatch.setattr(debugger, "ABORT_PERIOD", 0.01)
        monkeypatch.setattr(debugger, "ABORT_TOTAL", 0.03)
        hit = _hit(client)
        client.abort_replies = [{"status": "aborting"}]
        with pytest.raises(IsabelleToolError) as exc:
            await debugger.abort_eval_at_breakpoint(client, hit.hit_id)
        assert str(exc.value) == debugger.ABORT_UNCONFIRMED.format(seconds=0)


# ── Notices through the middleware (section 6.3) ───────────────────────


class TestNotices:
    def test_drain_format_and_once_only(self, client):
        debugger.registry.add_notice("first thing")
        debugger.registry.add_notice("second thing")
        assert debugger.registry.drain_notices() == (
            "Debugger notices:\n- first thing\n- second thing")
        assert debugger.registry.drain_notices() is None
