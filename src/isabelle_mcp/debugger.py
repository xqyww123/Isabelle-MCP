"""The breakpoint registry, the hit table and debugger notices.

Design: docs/archive/DEBUGGER_DESIGN.md sections 3 (addressing), 5 (registry
lifecycle), 6 (hits and notices); the tools of section 4 are thin wrappers
in server.py over the functions here (the abort tool is implemented but not
registered — user decision 2026-08-18).

Every agent-facing sentence of the debugger tools lives in this module (or in
the pure text assemblers of utils/formatters.py), unit-tested verbatim, in the
query.py style: wording changes cost no jar rebuild and are pinned character
for character.

Vocabulary (the glossary of the design doc, used consistently):

- breakable site — a stopping location the Poly/ML compiler inserted into
  instrumented ML code, identified on the wire by a *serial*.
- breakpoint — a registry entry: the intent to stop at a source location,
  surviving recompilation (which destroys and recreates sites) by
  re-resolution.
- armed / pending — the entry has a live site / has none right now.
- hit — one occasion of a thread halting in the debugger, identified by a
  *hit_id*; retired when the thread resumes or the prover goes away.
- debugger notice — a buffered one-line message about an asynchronous
  debugger event, appended to the next tool result.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from isabelle_mcp.lsp_client import IsabelleLSPClient
from isabelle_mcp.utils.core import IsabelleToolError, plural
from isabelle_mcp.utils.formatters import (
    cartouche,
    format_call_stack,
    indent_rows,
)
from isabelle_mcp.utils.isabelle_tokens import (
    find_symbol_occurrences,
    tokenize_isabelle_line,
)

logger = logging.getLogger(__name__)

# ── Timing policy ──────────────────────────────────────────────────────
#
# Prover-side deadlines are what the agent chooses (or the defaults below);
# request_timeout adds the hard transport deadline on top. It MUST accompany
# any prover-side timeout, because the client's default wait is
# progress-monitored (120 s stall detector) and would flag a long silent
# evaluation as a stall (Phase B review hand-off note).

LISTING_TIMEOUT = 30.0        # prover-side deadline of a breakpoints listing
TOGGLE_TIMEOUT = 30.0         # prover-side deadline of a toggle
EVAL_DEFAULT_TIMEOUT = 180.0  # section 4.9's default eval/locals deadline
REQUEST_MARGIN = 60.0         # request_timeout = prover timeout + this
OUTDATED_RETRIES = 20         # bounded retry on `outdated` (pending edits)
OUTDATED_RETRY_SLEEP = 0.5
STEP_WAIT = 30.0              # bounded wait for a step to stop again
CONTINUE_WAIT = 30.0          # bounded wait for a resumed thread to leave the map
ABORT_TOTAL = 30.0            # section 4.13's overall retry bound
ABORT_PERIOD = 2.0            # one retry period of the abort loop
SITE_LISTING_CAP = 40         # section 4.5's truncation bound
NEAREST_SITES_SHOWN = 8       # section 4.2 message 3: 4 each side


# ── Registry entry states and the section-4.4 reason tags ──────────────

ARMED = "armed"
PENDING = "pending"

# Short tags, used verbatim in listings AND notices (one concept, one
# wording). Kept short by user decision 2026-08-18 — the design's long
# advice clauses were dropped: the tag alone says enough.
TAG_NOT_EVALUATED = "not evaluated yet"
TAG_STILL_EVALUATING = "still evaluating"
TAG_CODE_NOT_FOUND = "code not found"
# Covers both a toggle request that never came back and a prover answer
# we cannot act on: either way the site's real state is unknown, and only
# a fresh (absolute) toggle settles it.
TAG_WIRE_FAILURE = "state unknown, internal failure"


# ── Sentence catalogue ─────────────────────────────────────────────────
# {where} is file:line (project-root-relative). Templates are .format()-ed.

DEBUG_OFF = (
    "Debugging is not enabled in this session. Call isabelle_terminate, "
    "then isabelle_launch with debug=true."
)

# set_breakpoint refusals (section 4.2, messages 1-5)
NO_SITE_NOT_EVALUATED = (
    "There is no breakable site at {where} — that line has not been "
    "evaluated yet. Breakpoints can only be set on code the prover has "
    "already compiled, so evaluate the file first."
)
NO_SITE_NOT_EVALUATED_ML = (
    "There is no breakable site at {where} — that line has not been "
    "evaluated yet. Breakpoints can only be set on code the prover has "
    "already compiled, and a .ML file is compiled by the ML_file command "
    "that loads it, so evaluate that ML_file command first."
)
NO_SITE_STILL_RUNNING = (
    "The command at {where} is still evaluating; a breakpoint can be set "
    "only after it finishes. Retry in a few seconds."
)
NO_SITE_ON_LINE = (
    "The command at {where} has been evaluated, but the compiler placed no "
    "breakable site on that line. Breakable sites exist only inside ML "
    "code, at statement boundaries the compiler chooses."
)
NEAREST_SITES_LEAD_IN = "The nearest breakable sites in this file are:"
NEAREST_SITES_TAIL = "Pass one of these as line + at_text."
MORE_SITES_POINTER = (
    "({n} more breakable sites in this file — use "
    "isabelle_list_breakable_sites to see them.)"
)
MORE_SITES_POINTER_ONE = (
    "(1 more breakable site in this file — use "
    "isabelle_list_breakable_sites to see it.)"
)
NO_SITES_IN_FILE = (
    "This file has no breakable sites at all — it contains no ML code that "
    "was compiled in this prover."
)
AT_TEXT_NOT_ON_LINE = "{at_text} does not occur on {where}."
NO_SITE_AT_OR_BEFORE = (
    "There is no breakable site at or before {at_text} on {where}."
)
LINE_SITES_LEAD_IN = "The sites on that line are:"
LINE_SITES_TAIL = (
    "Pass one of these as at_text, or omit at_text to use the first site "
    "on the line."
)
ARMING_FAILED = (
    "Arming the breakpoint at {where} failed: the prover answered "
    "{status}. The breakpoint was not recorded; retry in a few seconds."
)
ARMING_TIMED_OUT = (
    "The prover did not acknowledge the toggle within {seconds}s; whether "
    "the site was armed is unknown (the next listing shows the prover "
    "truth). The breakpoint was not recorded; set it again."
)
SET_OK = "Breakpoint set and armed: {where} before {anchor}."
SET_OTHER_SITES = "Other breakable sites on this line: {others}."
EMPTY_EXPR = (
    "expr is empty. Pass a single ML expression; a temporary binding is "
    "written let val x = … in … end."
)

# listing failures shared by every tool that reads the prover
LISTING_FAILED = (
    "Listing the breakable sites of {file} failed: the prover answered "
    "{status}. Retry in a few seconds."
)
FILE_NOT_OPEN_IN_PROVER = (
    "{file} is not open in the prover, so it has no breakable sites. "
    "Evaluate the file first."
)
FILE_NOT_OPEN_IN_PROVER_ML = (
    "{file} is not open in the prover. A .ML file is compiled by "
    "evaluating the theory that loads it (its ML_file command); evaluate "
    "that theory first."
)

# del_breakpoints (section 4.3); the problem refs follow, one per
# indented line
DELETED_COUNT = "deleted {count}"
NO_MATCH_HEADER = (
    "breakpoint not found — call isabelle_list_breakpoints to see the "
    "current breakpoints:"
)
AMBIGUOUS_HEADER = (
    "matches several breakpoints, not deleted — pass at_text to say which "
    "one:"
)

# list_breakpoints (section 4.4)
NO_BREAKPOINTS = "No breakpoints are registered."

# list_breakable_sites (section 4.5)
NOT_EVALUATED_LINE = (
    "not_evaluated: {spans} — evaluate up to those lines to see their sites"
)
NOT_EVALUATED_LINE_ONE = (
    "not_evaluated: {spans} — evaluate up to that line to see its sites"
)
TRUNCATED_LINE = (
    "truncated: showing {shown} of {total} sites, {span} — "
    "narrow the range (e.g. start_line {resume}) to see the rest"
)
NO_SITES_IN_RANGE = "No breakable sites in {span}."

# enable/disable all (sections 4.6/4.7)
ENABLE_NONE_REGISTERED = NO_BREAKPOINTS
ARMED_HEADER = "Armed {n}:"
ARMED_NONE = "No breakpoint could be armed."
STILL_PENDING = "Still pending: {counts}."
DISABLED_RESULT = (
    "Switched off {armed} armed {breakpoints}; {pending} pending "
    "{entries} also marked disabled. Re-enable with "
    "isabelle_enable_all_breakpoints."
)

# debug_state (section 4.8) and hit blocks
NO_THREAD_STOPPED = "No thread is stopped."
HITS_LIVE = "{n} {hits} live."
HIT_HEADER = "Hit {hit_id}: thread {thread}."
CALL_STACK_HEADER = (
    "Call stack (innermost first; the number is the frame parameter):"
)

# hit resolution errors
UNKNOWN_HIT = "There is no hit {hit_id}. Call isabelle_debug_state for the live hits."
RETIRED_HIT = (
    "Hit {hit_id} has ended: {ending} Call isabelle_debug_state for the "
    "live hits."
)
SEVERAL_HITS = "Several hits are live; pass hit_id:"
NO_HIT_LIVE = "No thread is stopped at a breakpoint."

# how a hit ended (the {ending} of RETIRED_HIT; each is a full clause)
ENDED_CONTINUED = "it was resumed by isabelle_continue_breakpoint."
ENDED_STEP_LEFT = (
    "it stepped without stopping again — execution left the instrumented "
    "region."
)
ENDED_RESUMED = "its thread resumed and is no longer stopped."
ENDED_TERMINATED = "the prover was terminated."

# eval / locals (sections 4.9/4.10)
EVAL_OUTSTANDING = (
    "The previous evaluation on this hit has not returned. Wait for it to "
    "finish and retry."
)
EVAL_NO_OUTPUT = "The evaluation completed with no output."
EVAL_BACKSTOP_TIMEOUT = (
    "The prover did not answer within {seconds}s. The evaluation may still "
    "be running, and this hit takes no new evaluations until it ends. "
    "Retry later; isabelle_cancel_evaluation is the way out if it never "
    "ends."
)
EVAL_BUSY = (
    "The prover still owes the reply of a previous evaluation on this hit. "
    "New evaluations are refused until it settles; retry in a few seconds."
)
EVAL_RESUMED = (
    "The thread resumed while the input was in flight; the expression may "
    "or may not have run. The hit is over — call isabelle_debug_state."
)
EVAL_NOT_STOPPED = (
    "The thread of hit {hit_id} is not stopped any more — the expression "
    "was never sent. Call isabelle_debug_state for the live hits."
)
EVAL_CRASHED = (
    "The prover could not answer this evaluation and could not say why."
)

# continue (section 4.11)
RESUMED_ONE = "Resumed hit {hit_id} (thread {thread})."
RESUMED_MANY_HEADER = "Resumed {n} hits:"
NOT_RESUMED = (
    "Hit {hit_id} did not resume within {seconds}s — the thread is still "
    "stopped. Call isabelle_debug_state."
)

# step (section 4.12)
STEP_STOPPED_AGAIN = "Hit {hit_id} stopped again."
STEP_DID_NOT_STOP = (
    "The thread resumed and did not stop again within {seconds}s — "
    "execution left the instrumented region (stepping only stops in ML "
    "compiled with debugging in this session). The hit has ended."
)

# abort (section 4.13)
ABORT_OK = (
    "The evaluation has ended. The thread stays at the breakpoint, still "
    "debuggable."
)
ABORT_NOTHING = "No evaluation is running on this hit — there is nothing to abort."
ABORT_UNCONFIRMED = (
    "The evaluation did not end within {seconds}s. Some ML code cannot be "
    "interrupted at all; isabelle_cancel_evaluation is the way out."
)

# debugger notices (section 6.3)
NOTICES_HEADER = "Debugger notices:"
NOTICE_DEMOTED = "breakpoint {where} before {anchor} no longer works ({tag})"
NOTICE_MERGED = (
    "breakpoint {where_a} before {anchor_a} resolves to the same site as "
    "{where_b} before {anchor_b}; merged into one entry"
)
NOTICE_NEW_HIT = (
    "thread {thread} stopped at a breakpoint — hit {hit_id}; inspect with "
    "isabelle_debug_state"
)
NOTICE_HIT_ENDED = "hit {hit_id} ended: {ending}"
NOTICE_STRAY_HALT = (
    "anomaly: thread {thread} stopped without any breakpoint being hit "
    "(a stepping flag can linger after stepping off the end) — recorded as "
    "hit {hit_id}; isabelle_continue_breakpoint clears it"
)


# ── Anchor snippets (section 3.2) ──────────────────────────────────────


# A snippet carries at least this many word tokens (identifiers/numbers;
# symbols like `=` or `(` ride along but do not count). Uniqueness alone
# would often stop at a bare `val`, which reads the same on every line of a
# let block (user decision 2026-08-18).
ANCHOR_MIN_WORDS = 3


def _is_word_token(text: str) -> bool:
    return text[:1].isalnum() or text[:1] in "_'"


def anchor_snippet(line_text: str, char: int) -> str:
    """The site's anchor snippet: source text from the statement's first
    character, whole tokens, extended until it is unique within its line AND
    carries ANCHOR_MIN_WORDS word tokens (fewer only when the rest of the
    line has fewer).

    Uniqueness is token-run occurrence counting (find_symbol_occurrences),
    the same matching at_text resolution uses — so a snippet passed back as
    at_text always survives the section-3.1 ambiguity rule. A snippet equal
    to the rest of the line cannot occur twice in it, so both conditions are
    always reachable without crossing the line.
    """
    tail = line_text[char:].rstrip()
    if not tail:
        return ""
    if not line_text.isascii():
        # The tokenizer's offsets are ASCII-space; rather than juggling two
        # coordinate systems for a rare case, fall back to the whole rest of
        # the line (trivially unique).
        return tail
    tokens = tokenize_isabelle_line(tail)
    if not tokens:
        return tail
    words = 0
    for tok_text, offset, _sym in tokens:
        if _is_word_token(tok_text):
            words += 1
        if words < ANCHOR_MIN_WORDS:
            continue
        candidate = tail[: offset + len(tok_text)]
        if len(find_symbol_occurrences(line_text, candidate)) == 1:
            return candidate
    return tail


# ── Breakable sites (the client-side view of one listing) ──────────────


class FileNotOpenInProver(IsabelleToolError):
    """The prover does not hold this file — the one listing failure whose
    cause is known (the code was never evaluated), as opposed to a failure
    that leaves the sites' state unknown."""


@dataclass
class Site:
    """One breakable site, in corrected coordinates (section 3.3: anchored at
    the markup range's END — the one-symbol shift — line derived from the
    corrected position)."""

    serial: int
    line: int            # 1-indexed corrected line
    char: int            # 0-indexed character of the statement's first char
    state: bool | str    # prover truth: enabled, or the unresolvable word
    anchor: str          # section-3.2 snippet


def sites_from_listing(reply: dict[str, Any], lines: list[str]) -> list[Site]:
    """Parse a `PIDE/debugger_breakpoints` reply into corrected, anchored
    sites, in source order. *lines* is the file content the prover holds."""
    sites: list[Site] = []
    for bp in reply.get("breakpoints", []):
        end = bp.get("range", {}).get("end", {})
        line0, char = end.get("line"), end.get("character")
        if not isinstance(line0, int) or not isinstance(char, int):
            continue
        line_text = lines[line0] if 0 <= line0 < len(lines) else ""
        sites.append(Site(
            serial=bp["serial"], line=line0 + 1, char=char,
            state=bp.get("state"),
            anchor=anchor_snippet(line_text, char),
        ))
    sites.sort(key=lambda s: (s.line, s.char))
    return sites


def _file_lines(client: IsabelleLSPClient, file_path: str) -> list[str]:
    """The file content the prover holds: the open document's model for .thy
    files; for dependency blobs (.ML), the disk copy — the closest available
    stand-in for "as last synced" (the server's own File_Watcher syncs them,
    so a fresher disk copy can drift until the next sync; accepted)."""
    doc = client.open_documents.get(file_path)
    if doc is not None:
        return doc.content.split("\n")
    try:
        with open(file_path, encoding="utf-8") as f:
            return f.read().split("\n")
    except OSError:
        return []


async def fetch_sites(
    client: IsabelleLSPClient, file_path: str,
) -> tuple[list[Site], list[str]]:
    """One listing round trip with the bounded `outdated` retry (pending
    edits incorporate within moments; every other failure is surfaced).
    Returns (sites, file lines); raises with a final sentence otherwise."""
    reply: dict[str, Any] = {}
    for _ in range(OUTDATED_RETRIES):
        reply = await client.debugger_breakpoints(
            file_path, timeout=LISTING_TIMEOUT,
            request_timeout=LISTING_TIMEOUT + REQUEST_MARGIN,
        )
        if reply.get("status") != "outdated":
            break
        await asyncio.sleep(OUTDATED_RETRY_SLEEP)
    display = _display_path(client, file_path)
    if reply.get("status") != "ok":
        raise IsabelleToolError(LISTING_FAILED.format(
            file=display, status=reply.get("status", "nothing")))
    if reply.get("open") is not True:
        template = (FILE_NOT_OPEN_IN_PROVER_ML if file_path.endswith(".ML")
                    else FILE_NOT_OPEN_IN_PROVER)
        raise FileNotOpenInProver(template.format(file=display))
    lines = _file_lines(client, file_path)
    return sites_from_listing(reply, lines), lines


# ── at_text resolution (section 3.1) ───────────────────────────────────


def _line_site_rows(sites_on_line: list[Site]) -> list[str]:
    return [f"before {cartouche(s.anchor)}" for s in sites_on_line]


def resolve_site(
    sites_on_line: list[Site], line_text: str, at_text: str | None,
    where: str,
) -> Site:
    """Pick the addressed site on a line that HAS sites.

    at_text omitted: the first site. Otherwise the site at or nearest
    before at_text's occurrence — refused when several occurrences resolve
    to different sites (a misplaced breakpoint costs a whole debugging
    round trip), with the line's sites listed.
    """
    if at_text is None:
        return sites_on_line[0]
    occurrences = find_symbol_occurrences(line_text, at_text)
    rows = indent_rows(_line_site_rows(sites_on_line))
    if not occurrences:
        raise IsabelleToolError(
            AT_TEXT_NOT_ON_LINE.format(
                at_text=cartouche(at_text), where=where)
            + " " + LINE_SITES_LEAD_IN + "\n\n" + rows + "\n\n"
            + LINE_SITES_TAIL)
    resolved: set[int | None] = set()
    chosen: Site | None = None
    for occ in occurrences:
        candidates = [s for s in sites_on_line if s.char <= occ]
        site = candidates[-1] if candidates else None
        resolved.add(site.serial if site else None)
        if chosen is None and site is not None:
            chosen = site
    if len(resolved) > 1 or chosen is None:
        # No site at/before the anchor, or ambiguous across occurrences:
        # both get message 5 (the section-3.1 refusal).
        raise IsabelleToolError(
            NO_SITE_AT_OR_BEFORE.format(
                at_text=cartouche(at_text), where=where)
            + " " + LINE_SITES_LEAD_IN + "\n\n" + rows + "\n\n"
            + LINE_SITES_TAIL)
    return chosen


# ── The registry ───────────────────────────────────────────────────────


@dataclass
class Breakpoint:
    """One registry entry (section 5): the intent to stop at a location."""

    file_path: str            # realpath
    line: int                 # recorded 1-indexed line, updated at arming
    anchor: str               # anchor snippet, updated at arming
    enabled: bool = True
    state: str = ARMED        # entries are only ever created armed
    reason: str | None = None  # section-4.4 tag while pending
    serial: int | None = None  # the live site's serial while armed


@dataclass
class Hit:
    """One live hit (glossary): a thread halted in the debugger."""

    hit_id: str
    thread: str
    stack: list[dict[str, Any]] = field(default_factory=list)
    # A step is a controlled resume-and-restop INSIDE the hit: while set,
    # the bookkeeping does not retire the hit on a transient absence.
    stepping: bool = False
    # Ending attributed by the tool that initiated the resume, consumed by
    # the bookkeeping when the thread actually leaves the map.
    pending_ending: str | None = None
    # The outstanding eval/locals round trip, so a second request is
    # refused and the abort tool can wait on its target's own reply.
    eval_task: asyncio.Task[Any] | None = None


class DebuggerRegistry:
    """Registry + hit table + notice buffer, with ONE lock serialising every
    read-check-mutate sequence (design section 5: all site toggles happen
    inside the explicit tools under this lock, which is what makes the
    projection invariant — prover site state rebuildable from the registry —
    actually hold)."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.entries: list[Breakpoint] = []
        self.hits: dict[str, Hit] = {}      # live hits by hit_id
        self.retired: dict[str, str] = {}   # hit_id -> how it ended
        self._hit_seq = 0
        self._notices: list[str] = []
        # Index into client.debugger_state_history already folded into the
        # hit table; the history is replayed in order so a resume-and-restop
        # between two tool calls still yields retire + new hit.
        self._consumed = 0

    # ── notices ────────────────────────────────────────────────────

    def add_notice(self, text: str) -> None:
        self._notices.append(text)

    def drain_notices(self) -> str | None:
        """The buffered notices as one block, clearing the buffer — each
        notice is delivered exactly once."""
        if not self._notices:
            return None
        block = NOTICES_HEADER + "\n" + "\n".join(
            f"- {n}" for n in self._notices)
        self._notices.clear()
        return block

    # ── hit bookkeeping ────────────────────────────────────────────

    def _new_hit(self, thread: str, stack: list[dict[str, Any]]) -> Hit:
        self._hit_seq += 1
        hit = Hit(hit_id=f"h{self._hit_seq}", thread=thread, stack=stack)
        self.hits[hit.hit_id] = hit
        return hit

    def _retire(self, hit: Hit, ending: str) -> None:
        self.hits.pop(hit.hit_id, None)
        self.retired[hit.hit_id] = ending

    def sync_hits(self, client: IsabelleLSPClient) -> None:
        """Fold the state notifications received since the last call into
        the hit table. Synchronous (no awaits), so it is atomic under the
        event loop and safe to call from any tool or the middleware."""
        history = client.debugger_state_history
        if self._consumed > len(history):
            # The history was cleared outside a teardown (defensive only).
            self._consumed = 0
        for params in history[self._consumed:]:
            entries = params.get("threads")
            if not isinstance(entries, list):
                continue
            stopped: dict[str, list[dict[str, Any]]] = {}
            for entry in entries:
                if isinstance(entry, dict) \
                        and isinstance(entry.get("thread"), str):
                    stack = entry.get("stack")
                    if isinstance(stack, list) and stack:
                        stopped[entry["thread"]] = stack
            for hit in list(self.hits.values()):
                if hit.thread in stopped:
                    hit.stack = stopped[hit.thread]
                    hit.stepping = False  # stopped again = same hit, restopped
                elif not hit.stepping:
                    ending = hit.pending_ending or ENDED_RESUMED
                    self._retire(hit, ending)
                    if hit.pending_ending is None:
                        self.add_notice(NOTICE_HIT_ENDED.format(
                            hit_id=hit.hit_id, ending=ending))
            live_threads = {h.thread for h in self.hits.values()}
            for thread, stack in stopped.items():
                if thread not in live_threads:
                    hit = self._new_hit(thread, stack)
                    self.add_notice(NOTICE_NEW_HIT.format(
                        thread=thread, hit_id=hit.hit_id))
        self._consumed = len(history)

    def resolve_hit(self, hit_id: str | None) -> Hit:
        """The addressed hit; sentences per section 4.9's hit_id contract."""
        if hit_id is not None:
            hit = self.hits.get(hit_id)
            if hit is not None:
                return hit
            ending = self.retired.get(hit_id)
            if ending is not None:
                raise IsabelleToolError(RETIRED_HIT.format(
                    hit_id=hit_id, ending=ending))
            raise IsabelleToolError(UNKNOWN_HIT.format(hit_id=hit_id))
        live = list(self.hits.values())
        if not live:
            raise IsabelleToolError(NO_HIT_LIVE)
        if len(live) > 1:
            rows = indent_rows([
                HIT_HEADER.format(hit_id=h.hit_id, thread=h.thread)
                for h in live])
            raise IsabelleToolError(SEVERAL_HITS + "\n" + rows)
        return live[0]

    # ── registry bookkeeping ───────────────────────────────────────

    def demote(
        self, client: IsabelleLSPClient, entry: Breakpoint, tag: str,
    ) -> None:
        """Demote an armed entry to pending (or re-tag a pending one),
        emitting one notice per state change — never toggling any site."""
        changed = entry.state != PENDING or entry.reason != tag
        entry.state = PENDING
        entry.serial = None
        entry.reason = tag
        if changed:
            self.add_notice(NOTICE_DEMOTED.format(
                where=self.where(client, entry),
                anchor=cartouche(entry.anchor), tag=tag))

    def where(self, client: IsabelleLSPClient, entry: Breakpoint) -> str:
        return f"{_display_path(client, entry.file_path)}:{entry.line}"

    def entry_row(self, client: IsabelleLSPClient, entry: Breakpoint) -> str:
        """One section-4.4 listing row."""
        flag = "enabled" if entry.enabled else "disabled"
        state = ARMED if entry.state == ARMED \
            else f"{PENDING} ({entry.reason or TAG_NOT_EVALUATED})"
        return (f"{self.where(client, entry)} before "
                f"{cartouche(entry.anchor)}, {flag}, {state}")

    def scoped_entries(self, file_path: str | None) -> list[Breakpoint]:
        if file_path is None:
            return list(self.entries)
        real = os.path.realpath(file_path)
        return [e for e in self.entries if e.file_path == real]

    # ── prover teardown (design: the hit table is cleared on EVERY
    #    prover teardown path; entries are retained and demoted) ──────

    def on_prover_teardown(self) -> None:
        for hit in list(self.hits.values()):
            self._retire(hit, ENDED_TERMINATED)
            self.add_notice(NOTICE_HIT_ENDED.format(
                hit_id=hit.hit_id, ending=ENDED_TERMINATED))
        for entry in self.entries:
            if entry.state == ARMED:
                # No client for path display here (the prover is going
                # away); the raw stored path is the honest fallback.
                entry.state = PENDING
                entry.serial = None
                entry.reason = TAG_NOT_EVALUATED
                self.add_notice(NOTICE_DEMOTED.format(
                    where=f"{entry.file_path}:{entry.line}",
                    anchor=cartouche(entry.anchor),
                    tag=TAG_NOT_EVALUATED))
        self._consumed = 0


registry = DebuggerRegistry()


def _display_path(client: IsabelleLSPClient, file_path: str) -> str:
    from isabelle_mcp.evaluation import relativize
    return relativize(file_path, client.project_root)


def require_debug(client: IsabelleLSPClient) -> None:
    """Every breakpoint/debugger tool fails fast without instrumentation."""
    if not client.debug:
        raise IsabelleToolError(DEBUG_OFF)


# ── Tool bodies (the section-4 tools; server.py wraps them) ──────────


def _position_state(client: IsabelleLSPClient, file_path: str, line: int) -> str:
    """The decoration tracker's verdict for a 1-indexed line."""
    from isabelle_mcp.processing import ProcessingTracker
    tracker = client.get_processing_tracker(file_path) or ProcessingTracker()
    return tracker.position_state(line - 1)


def _no_site_on_line_error(
    client: IsabelleLSPClient, file_path: str, line: int,
    sites: list[Site], where: str,
) -> IsabelleToolError:
    """Which of section 4.2's messages 1-3 applies when the line has no
    site. Message 3 (evaluated, no site) carries the nearest-sites listing."""
    from isabelle_mcp import processing
    state = _position_state(client, file_path, line)
    is_ml = file_path.endswith(".ML")
    if is_ml:
        # Per-position evaluation status exists only for .thy documents
        # (section 3.4): with sites elsewhere in the file the code was
        # compiled and this line simply has none (message 3); with no sites
        # at all the loading theory has likely not been evaluated.
        if not sites:
            return IsabelleToolError(
                NO_SITE_NOT_EVALUATED_ML.format(where=where))
        return _evaluated_no_site_error(sites, line, where)
    if state == processing.NOT_EVALUATED or state == processing.CANCELLED:
        return IsabelleToolError(NO_SITE_NOT_EVALUATED.format(where=where))
    if state == processing.RUNNING or state == processing.UNKNOWN:
        # Deliberately no paused-at-a-hit variant (user decision 2026-08-18):
        # the client can only guess the cause (parallel workers make the
        # guess wrong), while "retry" is never misleading — a parked run
        # surfaces through the hit notices and isabelle_debug_state anyway.
        return IsabelleToolError(NO_SITE_STILL_RUNNING.format(where=where))
    return _evaluated_no_site_error(sites, line, where)


def _evaluated_no_site_error(
    sites: list[Site], line: int, where: str,
) -> IsabelleToolError:
    lead = NO_SITE_ON_LINE.format(where=where)
    if not sites:
        return IsabelleToolError(lead + " " + NO_SITES_IN_FILE)
    before = [s for s in sites if s.line < line][-NEAREST_SITES_SHOWN // 2:]
    after = [s for s in sites if s.line > line][:NEAREST_SITES_SHOWN // 2]
    shown = before + after
    rows = indent_rows([
        f"line {s.line} before {cartouche(s.anchor)}" for s in shown])
    text = (lead + " " + NEAREST_SITES_LEAD_IN + "\n\n" + rows + "\n\n"
            + NEAREST_SITES_TAIL)
    remaining = len(sites) - len(shown)
    if remaining == 1:
        text += "\n" + MORE_SITES_POINTER_ONE
    elif remaining > 1:
        text += "\n" + MORE_SITES_POINTER.format(n=remaining)
    return IsabelleToolError(text)


async def _toggle_site(
    client: IsabelleLSPClient, file_path: str, serial: int, state: bool,
) -> dict[str, Any]:
    return await client.debugger_toggle_breakpoint(
        file_path, serial, state,
        timeout=TOGGLE_TIMEOUT,
        request_timeout=TOGGLE_TIMEOUT + REQUEST_MARGIN,
    )


async def set_breakpoint(
    client: IsabelleLSPClient, file_path: str, line: int,
    at_text: str | None,
) -> str:
    """section 4.2: register a breakpoint and enable its site. An entry is
    created only on the toggle's positive acknowledgement."""
    require_debug(client)
    registry.sync_hits(client)
    if line < 1:
        raise IsabelleToolError(f"line must be >= 1, got {line}")
    file_path = os.path.realpath(file_path)
    where = f"{_display_path(client, file_path)}:{line}"
    async with registry.lock:
        sites, lines = await fetch_sites(client, file_path)
        on_line = [s for s in sites if s.line == line]
        if not on_line:
            raise _no_site_on_line_error(client, file_path, line, sites, where)
        line_text = lines[line - 1] if line - 1 < len(lines) else ""
        site = resolve_site(on_line, line_text, at_text, where)
        if site.state == "unfinished":
            raise IsabelleToolError(NO_SITE_STILL_RUNNING.format(where=where))
        reply = await _toggle_site(client, file_path, site.serial, True)
        status = reply.get("status")
        if status == "timeout":
            raise IsabelleToolError(ARMING_TIMED_OUT.format(
                seconds=int(TOGGLE_TIMEOUT)))
        if status == "unfinished":
            raise IsabelleToolError(NO_SITE_STILL_RUNNING.format(where=where))
        if status != "ok":
            raise IsabelleToolError(ARMING_FAILED.format(
                where=where, status=status))
        entry = _record_armed(client, file_path, site)
        result = SET_OK.format(
            where=f"{_display_path(client, file_path)}:{entry.line}",
            anchor=cartouche(entry.anchor))
        others = [s for s in on_line if s.serial != site.serial]
        if others:
            result += "\n" + SET_OTHER_SITES.format(
                others=", ".join(
                    f"before {cartouche(s.anchor)}" for s in others))
        return result


def _record_armed(
    client: IsabelleLSPClient, file_path: str, site: Site,
) -> Breakpoint:
    """Record an acknowledged arming: update the entry already on this site
    (or merge duplicates into it), else create one. Caller holds the lock."""
    matching = [
        e for e in registry.entries
        if e.file_path == file_path and (
            e.serial == site.serial
            or (e.state == PENDING and e.line == site.line
                and e.anchor == site.anchor))
    ]
    if matching:
        keep = matching[0]
        for extra in matching[1:]:
            registry.entries.remove(extra)
            registry.add_notice(NOTICE_MERGED.format(
                where_a=registry.where(client, extra),
                anchor_a=cartouche(extra.anchor),
                where_b=registry.where(client, keep),
                anchor_b=cartouche(keep.anchor)))
    else:
        keep = Breakpoint(file_path=file_path, line=site.line,
                          anchor=site.anchor)
        registry.entries.append(keep)
    keep.enabled = True
    keep.state = ARMED
    keep.serial = site.serial
    keep.reason = None
    keep.line = site.line      # re-anchor: arming-time resolution updates
    keep.anchor = site.anchor  # the recorded line and snippet
    return keep


def _normalize_ref_path(client: IsabelleLSPClient, path: str) -> str:
    """Accept both the project-root-relative form listings print and
    absolute paths (section 4.3)."""
    if not os.path.isabs(path) and client.project_root:
        path = os.path.join(client.project_root, path)
    return os.path.realpath(path)


async def del_breakpoints(
    client: IsabelleLSPClient, refs: list[tuple[str, int, str | None]],
) -> str:
    """section 4.3: best-effort delete; disarm armed sites; report what did
    not match. A reference matching several entries is skipped, never
    guessed."""
    require_debug(client)
    registry.sync_hits(client)
    if not refs:
        raise IsabelleToolError("breakpoints must not be empty")
    deleted = 0
    no_match: list[str] = []
    ambiguous: list[str] = []
    async with registry.lock:
        for path, line, at_text in refs:
            real = _normalize_ref_path(client, path)
            display = f"{_display_path(client, real)}:{line}"
            if at_text is not None:
                display += f" before {cartouche(at_text)}"
            matches = [
                e for e in registry.entries
                if e.file_path == real and e.line == line
                and (at_text is None or e.anchor == at_text)
            ]
            if not matches:
                no_match.append(display)
                continue
            if len(matches) > 1:
                ambiguous.append(display)
                continue
            entry = matches[0]
            if entry.state == ARMED and entry.serial is not None:
                # Best-effort disarm; a failure only means the site is gone
                # already (or the prover is wedged) — the entry goes anyway.
                try:
                    await _toggle_site(client, entry.file_path,
                                       entry.serial, False)
                except IsabelleToolError:
                    logger.warning("disarm on delete failed for %s",
                                   entry.file_path)
            registry.entries.remove(entry)
            deleted += 1
    parts = [DELETED_COUNT.format(count=plural(deleted, "breakpoint"))]
    if no_match:
        parts.append(NO_MATCH_HEADER)
        parts.extend(f"  {ref}" for ref in no_match)
    if ambiguous:
        parts.append(AMBIGUOUS_HEADER)
        parts.extend(f"  {ref}" for ref in ambiguous)
    return "\n".join(parts)


def list_breakpoints(
    client: IsabelleLSPClient, file_path: str | None,
) -> str:
    """section 4.4: the registry, nothing else."""
    require_debug(client)
    registry.sync_hits(client)
    entries = registry.scoped_entries(file_path)
    if not entries:
        return NO_BREAKPOINTS
    rows = "\n".join(
        f"  - {registry.entry_row(client, e)}" for e in entries)
    return "breakpoints:\n" + rows


async def list_breakable_sites(
    client: IsabelleLSPClient, file_path: str,
    start_line: int | None, end_line: int | None,
) -> str:
    """section 4.5: where breakpoints CAN go, with the registry overlay and
    the not-yet-evaluated ranges named separately."""
    require_debug(client)
    registry.sync_hits(client)
    file_path = os.path.realpath(file_path)
    start = start_line if start_line is not None else 1
    end = end_line if end_line is not None else 10 ** 9
    if start < 1 or end < start:
        raise IsabelleToolError(
            f"invalid line range: start_line {start}, end_line {end}")
    async with registry.lock:
        sites, _lines = await fetch_sites(client, file_path)
        in_range = [s for s in sites if start <= s.line <= end]
        armed_serials = {
            e.serial: e for e in registry.entries
            if e.file_path == file_path and e.state == ARMED
        }
        shown = in_range[:SITE_LISTING_CAP]
        rows = []
        for s in shown:
            entry = armed_serials.get(s.serial)
            if entry is None:
                tag = "breakable"
            elif entry.enabled:
                tag = "already enabled"
            else:
                tag = "already set but disabled"
            rows.append(
                f"  - line {s.line} before {cartouche(s.anchor)}, {tag}")
        not_evaluated = _unevaluated_spans(client, file_path, start, end) \
            if not file_path.endswith(".ML") else []
        out: list[str] = []
        if rows:
            out.append("sites:")
            out.extend(rows)
        elif not_evaluated:
            out.append("sites:")
        elif not sites:
            out.append(NO_SITES_IN_FILE)
        else:
            last = min(end, max(s.line for s in sites))
            hi = end if end_line is not None else last
            out.append(NO_SITES_IN_RANGE.format(span=_line_span(start, hi)))
        if not_evaluated:
            one = (len(not_evaluated) == 1
                   and not_evaluated[0][0] == not_evaluated[0][1])
            template = NOT_EVALUATED_LINE_ONE if one else NOT_EVALUATED_LINE
            out.append(template.format(spans=_format_spans(not_evaluated)))
        if len(in_range) > len(shown):
            out.append(TRUNCATED_LINE.format(
                shown=len(shown), total=len(in_range),
                span=_line_span(shown[0].line, shown[-1].line),
                resume=shown[-1].line + 1))
        return "\n".join(out)


def _unevaluated_spans(
    client: IsabelleLSPClient, file_path: str, start: int, end: int,
) -> list[tuple[int, int]]:
    """1-indexed unevaluated line spans within [start, end], merged; reuses
    the decoration machinery isabelle_command_status reads."""
    tracker = client.get_processing_tracker(file_path)
    n_lines = len(_file_lines(client, file_path))
    if tracker is None:
        # No decoration ever: the whole (clipped) range is unknown.
        hi = min(end, n_lines) if n_lines else end
        return [(start, hi)] if hi >= start else []
    spans = []
    for sl, _, el, _ in tracker.get_unprocessed_ranges():
        lo, hi = max(sl + 1, start), min(el + 1, end)
        if lo <= hi:
            spans.append((lo, hi))
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def _line_span(lo: int, hi: int) -> str:
    return f"line {lo}" if lo == hi else f"lines {lo}-{hi}"


def _format_spans(spans: list[tuple[int, int]]) -> str:
    return ", ".join(_line_span(lo, hi) for lo, hi in spans)


async def enable_all_breakpoints(
    client: IsabelleLSPClient, file_path: str | None,
) -> str:
    """section 4.6: set every (scoped) entry enabled AND arm every entry
    whose site currently exists — THE re-arming action of the manual model.
    An entry is recorded armed only on the toggle's positive
    acknowledgement."""
    require_debug(client)
    registry.sync_hits(client)
    async with registry.lock:
        entries = registry.scoped_entries(file_path)
        if not entries:
            return ENABLE_NONE_REGISTERED
        for entry in entries:
            entry.enabled = True
        armed_rows: list[str] = []
        pending_counts: dict[str, int] = {}
        by_file: dict[str, list[Breakpoint]] = {}
        for entry in entries:
            by_file.setdefault(entry.file_path, []).append(entry)
        for path, group in by_file.items():
            try:
                sites, _lines = await fetch_sites(client, path)
            except FileNotOpenInProver:
                for entry in group:
                    registry.demote(client, entry, TAG_NOT_EVALUATED)
                    _count(pending_counts, TAG_NOT_EVALUATED)
                continue
            except IsabelleToolError:
                # The listing failed for any other reason (the prover
                # answered a failure word, or the request itself died): the
                # sites' real state is unknown, and saying "not evaluated
                # yet" would be a guess.
                for entry in group:
                    registry.demote(client, entry, TAG_WIRE_FAILURE)
                    _count(pending_counts, TAG_WIRE_FAILURE)
                continue
            for entry in group:
                row = await _arm_entry(client, entry, sites, pending_counts)
                if row is not None:
                    armed_rows.append(row)
        _merge_same_site(client, entries)
        out = []
        if armed_rows:
            out.append(ARMED_HEADER.format(n=len(armed_rows)))
            out.extend(f"  - {r}" for r in armed_rows)
        else:
            out.append(ARMED_NONE)
        if pending_counts:
            counts = ", ".join(
                f"{n} ({tag})" for tag, n in pending_counts.items())
            out.append(STILL_PENDING.format(counts=counts))
        return "\n".join(out)


def _count(counts: dict[str, int], tag: str) -> None:
    counts[tag] = counts.get(tag, 0) + 1


def _resolve_entry_site(entry: Breakpoint, sites: list[Site]) -> Site | None:
    """Arming-time site resolution (section 5): by anchor snippet, nearest
    to the recorded line; an equal-distance tie resolves to the earlier
    line. A site matches when its statement text still starts with the
    recorded snippet (the snippet identifies the statement; the
    uniqueness-driven length may differ after edits)."""
    if entry.serial is not None:
        by_serial = [s for s in sites if s.serial == entry.serial]
        if by_serial:
            return by_serial[0]
    candidates = [
        s for s in sites
        if s.anchor == entry.anchor or s.anchor.startswith(entry.anchor)
        or entry.anchor.startswith(s.anchor)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: (abs(s.line - entry.line), s.line))


async def _arm_entry(
    client: IsabelleLSPClient, entry: Breakpoint, sites: list[Site],
    pending_counts: dict[str, int],
) -> str | None:
    """Try to arm one entry; returns its listing row when armed, else None
    after tagging it pending. Caller holds the lock."""
    site = _resolve_entry_site(entry, sites)
    if site is None:
        registry.demote(client, entry, TAG_CODE_NOT_FOUND)
        _count(pending_counts, TAG_CODE_NOT_FOUND)
        return None
    if site.state == "unfinished":
        registry.demote(client, entry, TAG_STILL_EVALUATING)
        _count(pending_counts, TAG_STILL_EVALUATING)
        return None
    try:
        reply = await _toggle_site(client, entry.file_path, site.serial, True)
    except IsabelleToolError:
        registry.demote(client, entry, TAG_WIRE_FAILURE)
        _count(pending_counts, TAG_WIRE_FAILURE)
        return None
    status = reply.get("status")
    if status == "ok":
        entry.state = ARMED
        entry.serial = site.serial
        entry.reason = None
        entry.line = site.line
        entry.anchor = site.anchor
        return registry.entry_row(client, entry)
    if status == "unfinished":
        registry.demote(client, entry, TAG_STILL_EVALUATING)
        _count(pending_counts, TAG_STILL_EVALUATING)
    elif status == "unknown_breakpoint":
        registry.demote(client, entry, TAG_CODE_NOT_FOUND)
        _count(pending_counts, TAG_CODE_NOT_FOUND)
    else:
        registry.demote(client, entry, TAG_WIRE_FAILURE)
        _count(pending_counts, TAG_WIRE_FAILURE)
    return None


def _merge_same_site(
    client: IsabelleLSPClient, entries: list[Breakpoint],
) -> None:
    """Entries that resolved to the same site merge into one (section 5),
    reported by a notice. Caller holds the lock."""
    seen: dict[tuple[str, int], Breakpoint] = {}
    for entry in entries:
        if entry.state != ARMED or entry.serial is None:
            continue
        key = (entry.file_path, entry.serial)
        keep = seen.get(key)
        if keep is None:
            seen[key] = entry
        elif entry in registry.entries:
            registry.entries.remove(entry)
            registry.add_notice(NOTICE_MERGED.format(
                where_a=registry.where(client, entry),
                anchor_a=cartouche(entry.anchor),
                where_b=registry.where(client, keep),
                anchor_b=cartouche(keep.anchor)))


async def disable_all_breakpoints(
    client: IsabelleLSPClient, file_path: str | None,
) -> str:
    """section 4.7: absolute off — the enabled flag goes false everywhere in
    scope and armed sites are switched off; entries stay registered (armed
    is about having a live site, the flag is about whether it stops)."""
    require_debug(client)
    registry.sync_hits(client)
    async with registry.lock:
        entries = registry.scoped_entries(file_path)
        if not entries:
            return NO_BREAKPOINTS
        armed_count = 0
        pending_count = 0
        for entry in entries:
            entry.enabled = False
            if entry.state != ARMED or entry.serial is None:
                pending_count += 1
                continue
            try:
                reply = await _toggle_site(
                    client, entry.file_path, entry.serial, False)
            except IsabelleToolError:
                registry.demote(client, entry, TAG_WIRE_FAILURE)
                pending_count += 1
                continue
            if reply.get("status") == "ok":
                armed_count += 1
            elif reply.get("status") == "unknown_breakpoint":
                registry.demote(client, entry, TAG_CODE_NOT_FOUND)
                pending_count += 1
            else:
                registry.demote(client, entry, TAG_WIRE_FAILURE)
                pending_count += 1
        return DISABLED_RESULT.format(
            armed=armed_count,
            breakpoints="breakpoint" if armed_count == 1 else "breakpoints",
            pending=pending_count,
            entries="entry" if pending_count == 1 else "entries")


# ── Hits: state, eval, locals, continue, step, abort ───────────────────


def _hit_block(hit: Hit) -> str:
    header = HIT_HEADER.format(hit_id=hit.hit_id, thread=hit.thread)
    return header + "\n" + CALL_STACK_HEADER + "\n" \
        + format_call_stack(_stack_rows(hit.stack))


def _stack_rows(stack: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(function, position) pairs for one call stack. Frames carry file/line
    only when the prover-side position properties do (typically ML compiled
    from real .ML files); command-relative positions resolve in Phase D."""
    rows: list[tuple[str, str]] = []
    for frame in stack:
        function = str(frame.get("function") or "?")
        file, line = frame.get("file"), frame.get("line")
        position = f"{file}:{line}" if file and line is not None else ""
        rows.append((function, position))
    return rows


def debug_state(client: IsabelleLSPClient) -> str:
    """section 4.8: all live hits with their call stacks."""
    require_debug(client)
    registry.sync_hits(client)
    hits = list(registry.hits.values())
    if not hits:
        return NO_THREAD_STOPPED
    head = HITS_LIVE.format(
        n=len(hits), hits="hit is" if len(hits) == 1 else "hits are")
    return head + "\n\n" + "\n\n".join(_hit_block(h) for h in hits)


def _check_eval_fence(hit: Hit) -> None:
    if hit.eval_task is not None and not hit.eval_task.done():
        raise IsabelleToolError(EVAL_OUTSTANDING)


def _render_eval_reply(
    reply: dict[str, Any], hit_id: str, timeout: float,
) -> str:
    status = reply.get("status")
    if status == "ok":
        texts = []
        for m in reply.get("messages", []):
            kind, text = m.get("kind", ""), m.get("text", "")
            texts.append(text if kind in ("", "writeln")
                         else f"[{kind}] {text}")
        return "\n".join(texts) if texts else EVAL_NO_OUTPUT
    if status == "timeout":
        raise IsabelleToolError(EVAL_BACKSTOP_TIMEOUT.format(
            seconds=int(timeout)))
    if status == "busy":
        raise IsabelleToolError(EVAL_BUSY)
    if status == "resumed":
        raise IsabelleToolError(EVAL_RESUMED)
    if status == "not_stopped":
        raise IsabelleToolError(EVAL_NOT_STOPPED.format(hit_id=hit_id))
    raise IsabelleToolError(EVAL_CRASHED)


async def eval_at_breakpoint(
    client: IsabelleLSPClient, expr: str, hit_id: str | None,
    frame: int, timeout: float,
) -> str:
    """section 4.9: one ML expression in a stack frame's scope."""
    require_debug(client)
    registry.sync_hits(client)
    if not expr or not expr.strip():
        # An empty expr is wire-legal (it compiles to `val it = ( );`), so
        # the refusal must happen here, before the wire.
        raise IsabelleToolError(EMPTY_EXPR)
    hit = registry.resolve_hit(hit_id)
    _check_eval_fence(hit)
    # create_task so a cancelled tool call leaves the round trip running
    # and the abort tool can still wait on its reply.
    task = asyncio.create_task(client.debugger_eval(
        hit.thread, expr, frame=frame, timeout=timeout,
        request_timeout=timeout + REQUEST_MARGIN))
    hit.eval_task = task
    reply = await task
    registry.sync_hits(client)
    return _render_eval_reply(reply, hit.hit_id, timeout)


async def locals_at_breakpoint(
    client: IsabelleLSPClient, hit_id: str | None,
    frame: int, timeout: float,
) -> str:
    """section 4.10: all locals of a frame, via the prelude's printer under
    the eval verb (same wire shape and fences as an eval)."""
    require_debug(client)
    registry.sync_hits(client)
    hit = registry.resolve_hit(hit_id)
    _check_eval_fence(hit)
    task = asyncio.create_task(client.debugger_print_vals(
        hit.thread, frame=frame, timeout=timeout,
        request_timeout=timeout + REQUEST_MARGIN))
    hit.eval_task = task
    reply = await task
    registry.sync_hits(client)
    return _render_eval_reply(reply, hit.hit_id, timeout)


async def continue_breakpoint(
    client: IsabelleLSPClient, hit_id: str | None,
) -> str:
    """section 4.11: resume one hit, or all of them when hit_id is omitted
    (the one tool where omission with several hits means "all")."""
    require_debug(client)
    registry.sync_hits(client)
    if hit_id is not None:
        hits = [registry.resolve_hit(hit_id)]
    else:
        hits = list(registry.hits.values())
        if not hits:
            return NO_HIT_LIVE
    for hit in hits:
        _check_eval_fence(hit)
    for hit in hits:
        hit.pending_ending = ENDED_CONTINUED
        await client.debugger_input(
            hit.thread, ["continue"],
            request_timeout=TOGGLE_TIMEOUT)
    await client.wait_debugger_event(
        lambda c: all(not c.debugger_threads.get(h.thread) for h in hits),
        timeout=CONTINUE_WAIT)
    registry.sync_hits(client)
    lines = []
    for hit in hits:
        if hit.hit_id in registry.retired:
            lines.append(RESUMED_ONE.format(
                hit_id=hit.hit_id, thread=hit.thread))
        else:
            hit.pending_ending = None
            lines.append(NOT_RESUMED.format(
                hit_id=hit.hit_id, seconds=int(CONTINUE_WAIT)))
    if len(lines) == 1:
        return lines[0]
    return RESUMED_MANY_HEADER.format(n=len(hits)) + "\n" \
        + "\n".join(f"  - {ln}" for ln in lines)


STEP_VERBS = {"step": "step", "step_over": "step_over", "step_out": "step_out"}


async def step_at_breakpoint(
    client: IsabelleLSPClient, mode: str, hit_id: str | None,
) -> str:
    """section 4.12: single-step a hit's thread; both outcomes (stopped
    again / did not stop) are normal, and the wait bound never aborts
    anything."""
    require_debug(client)
    registry.sync_hits(client)
    verb = STEP_VERBS.get(mode)
    if verb is None:
        raise IsabelleToolError(
            f"mode must be one of {sorted(STEP_VERBS)}, got {mode!r}")
    hit = registry.resolve_hit(hit_id)
    _check_eval_fence(hit)
    hit.stepping = True
    hit.pending_ending = None
    states_seen = len(client.debugger_state_history)
    await client.debugger_input(
        hit.thread, [verb], request_timeout=TOGGLE_TIMEOUT)
    deadline = time.monotonic() + STEP_WAIT
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        arrived = await client.wait_debugger_event(
            lambda c: len(c.debugger_state_history) > states_seen,
            timeout=remaining)
        if not arrived:
            break
        states_seen = len(client.debugger_state_history)
        if client.debugger_threads.get(hit.thread):
            break  # stopped again — same hit, new position
    if client.debugger_threads.get(hit.thread):
        hit.stepping = False
        registry.sync_hits(client)
        return STEP_STOPPED_AGAIN.format(hit_id=hit.hit_id) + "\n" \
            + _hit_block(hit)
    hit.stepping = False
    hit.pending_ending = ENDED_STEP_LEFT
    registry.sync_hits(client)
    if hit.hit_id in registry.hits:
        # No state notification carried the absence yet; retire on the live
        # map's evidence (the thread is not stopped).
        registry._retire(hit, ENDED_STEP_LEFT)
    return STEP_DID_NOT_STOP.format(seconds=int(STEP_WAIT))


async def abort_eval_at_breakpoint(
    client: IsabelleLSPClient, hit_id: str | None,
) -> str:
    """section 4.13: outcome-based bounded retry. The Scala side is
    stateless; this loop confirms by outcome — the targeted evaluation's
    own reply — and in the debt case re-sends until the abort reply flips
    to no_evaluation."""
    require_debug(client)
    registry.sync_hits(client)
    hit = registry.resolve_hit(hit_id)
    target = hit.eval_task \
        if hit.eval_task is not None and not hit.eval_task.done() else None
    deadline = time.monotonic() + ABORT_TOTAL
    sent = False
    while True:
        reply = await client.debugger_abort(
            hit.thread, request_timeout=TOGGLE_TIMEOUT)
        status = reply.get("status")
        if status == "no_evaluation":
            if not sent:
                raise IsabelleToolError(ABORT_NOTHING)
            return ABORT_OK  # debt cleared: the evaluation has ended
        sent = True  # `aborting`: the flag command went out once
        if target is not None:
            done, _ = await asyncio.wait({target}, timeout=ABORT_PERIOD)
            if done:
                return ABORT_OK  # the target's own reply arrived: settled
        else:
            await asyncio.sleep(ABORT_PERIOD)
        if time.monotonic() >= deadline:
            raise IsabelleToolError(ABORT_UNCONFIRMED.format(
                seconds=int(ABORT_TOTAL)))
