# Debugger Phase A repair round — full review-team verdict (2026-08-14)

Raw record of the judge's final verdict from the adversarial verification of the
consolidated repair plan (4 attack lenses -> 37 findings -> 2 refuters, both must
uphold -> 29 survived -> this judgment). The ACTIONABLE distillation lives in
DEBUGGER_IMPLEMENTATION_PLAN.md ("Phase A repair round"); where the two disagree,
the plan wins (it postdates this record). Line references refer to commit 62008cf.

---

Spot-checks done against the code (debugger.scala:308-324 abort handler, mcp_prelude.ML:303-344 wrapper, :427-437 debug_abort, lsp_client.py:1262-1298 sync_dirty_files) — the load-bearing evidence in the surviving findings holds. Final judgment follows.

# Final judgment on the repair plan (R1–R11)

## R1 — Dispatcher-side backstop bookkeeping: VERDICT: ADJUST

Direction and mechanism are right. Two exact adjustments:

1. **Identity check by Scala-side serial, not client token.** `params.token` arrives verbatim from the wire (lsp.scala:1027-1032) and no eval sender exists yet to enforce uniqueness; two equal tokens on one thread would let a stale fired-timer closure answer the fresh eval with TIMEOUT plus spurious debt — the very race R1 closes. Give `Pending` a fresh `Counter` serial (or compare the `Pending` reference itself); the timer closure captures the serial it was armed for. The client token remains for LSP correlation only.
2. **Look-up-compare-remove, not take-then-re-insert.** Once the backstop body runs dispatcher-side, all four mutators of `pending`/`debt` are thread-confined (all_messages consumer, start_eval, moved backstop, moved prover_exit), so the correct shape is: `pending.value.get(thread)` → if serial matches, remove + respond TIMEOUT + add debt; else do nothing. The re-insert dance is a leftover of the lock-free style with no atomicity gained.

The token check itself is confirmed necessary: `Event_Timer.cancel` returns false once fired (event_timer.scala:24), and a fired task's posted closure can run after a completion plus fresh registration on the same thread.

## R2 — Abort as outcome-based bounded retry: VERDICT: REWORK

The drafted dispatcher-side retry state machine is broken on two independent counts, both blocking:

1. **Retry re-sends can kill the wrong evaluation.** The ML registration table is keyed by thread name only (mcp_prelude.ML:427-437, registration at :310-312). Concrete failure: abort targets eval#1; eval#1 completes normally (entry removed, no debt, thread still stopped); the client starts eval#2 on the same thread; the next 2 s tick sees pending(thread) nonempty, concludes "not settled", re-sends `Isabelle_MCP.debug_abort` — eval#2 is killed unasked. The same path exists through debt. The rationale "re-sending is harmless by the flag-lives-in-registration-entry design" is false across settlement boundaries: the original design was safe only because the abort was sent exactly once while the Scala pending entry it checked still existed.
2. **The promised outcome classification (aborted-error / other completion / TIMEOUT) cannot be built from what the adapter observes.** An aborted eval exits as an ordinary `Fail "…ABORTED"` printed as an error message with the thread still stopped (mcp_prelude.ML:317-320, :331-339) — wire-identical to a normal failed eval (`status = if (stack.nonEmpty) OK else RESUMED`, debugger.scala:137, is the only status source). Sniffing message text is spoofable by the evaluated expression itself; the abort-delivered-but-body-won case returns a plain success (mcp_prelude.ML:340-343); and the debt-settling completion is eaten with nothing recorded (debugger.scala:143-151). The reply contract as written is unimplementable.

**Replacement (fold of all three surviving R2 findings, which converge):**

- **Scala stays stateless.** Fix the current handler's real bug: it consults only `pending` (debugger.scala:311-323), so an indebted thread — precisely the runaway-eval case abort exists for — is wrongly answered `no_evaluation`. New behavior: if the thread has neither a pending entry nor debt → reply `no_evaluation`; else send `Isabelle_MCP.debug_abort` once and reply `aborting` immediately. No timers, no waiters, no per-thread outcome memory.
- **The retry loop lives in the Python abort tool** (which owns outstanding-request state by design, DEBUGGER_DESIGN.md §4.13/§6.1, and where all retry/bound policy already lives by repo convention — lsp_client.py:202-209, :922-941). Loop: send `PIDE/debugger_abort`; wait min(2 s, the targeted eval's own future); repeat, bounded ~30 s. Termination: (a) the targeted eval's own reply arrives → stop re-sending immediately and report settled — this pins the abort to the evaluation it targeted and closes the wrong-target window, because the tool never re-sends after the target settles and the agent cannot start eval#2 while blocked in the abort tool; (b) in the debt case (eval future already resolved as TIMEOUT), keep re-sending until the abort reply flips to `no_evaluation` (debt cleared) — no wrong-target risk there since the busy fence refuses new evals while debt is owed; (c) at the bound, report honestly: "abort requested repeatedly; the evaluation has not ended" (never claim "delivered" — the ML entry may never have existed).
- **Drop outcome classification from the abort reply entirely.** The eval's own LSP reply, which the client also holds, carries how the evaluation ended. Abort vocabulary: `no_evaluation` / `aborting`; the Python tool distinguishes first-poll `no_evaluation` ("nothing to abort") from later-poll `no_evaluation` ("settled").

The ML silent no-op and the settling criterion (no pending AND no debt) are unchanged; worst-case latency is the same one retry period; the pre-registration lost-abort window is still covered by the 2 s re-send.

## R3 — Self-compiling wrapper: VERDICT: SOUND (two one-line additions)

All questioned mechanics are experimentally confirmed (scratchpad/r3_probe.ML via ML_process): the exact flags record compiles field-for-field; the token-list concatenation type-checks; `val it = ( expr );` echoes correctly; declarations are rejected with a clear wrapper-token error; `\ddd` escapes round-trip including `\000`/`\255`; the literal encoder's pure-ASCII output makes the outer `Symbol.encode` a no-op. Additions:

1. The `val _` envelope's silence rests on one guard: `verbose = true` still flushes ONE `writeln` with the EMPTY string (ml_compiler.ML:199,212), and only `Debugger.writeln_message`'s `if msg = "" then ()` (debugger.ML:21-22) keeps the wire silent. Add a prelude comment saying so, and pin it with a probe (R11).
2. Empty `expr` now compiles to `val it = ( );` — a valid unit expression — where the old embedding was a syntax error. Refuse empty/whitespace-only `expr` in the Python tool layer before composing; one sentence in §4.9.

## R4 — Acknowledged toggle: VERDICT: ADJUST

The machinery reuse is verified sound (inline mcp_define with error capture, `Toplevel.presentation_context` total in 2025-2, protocol-thread `Output.protocol_message` per the pong precedent). Four adjustments:

1. **A second Query_Handler cannot exist.** `Protocol_Handlers.State.init` errors on a duplicate handler class name and on duplicate protocol functions (protocol_handlers.scala:24, :27-28); `isabelle_mcp_query_result` is already claimed by the private instance at language_server.scala:757. State explicitly: toggle (and R5's listing) correlate through the EXISTING `Language_Server.query_handler` instance — expose it to Debugger_Adapter (package-private accessor or register/take facade). Never construct a second instance.
2. **Name the third pre-check's status.** Serial-not-found-in-markup (currently the "unknown breakpoint serial" error string, debugger.scala:248-249) stays Scala-side and needs a wire word. Reusing `unknown_breakpoint` for both "site gone from markup" and "ref absent in the command's context" is acceptable — but write the dual meaning into §7.1.
3. **`cancelled` is unreachable** for an inline no-fork handler (the entry is always taken by mcp_finish before the next protocol command runs). Drop it from the advertised toggle statuses or mark it vestigial-shared-machinery.
4. **Migrate the existing probes in the same commit** as the wire change: tests/integration/test_debugger_probes.py:185 asserts `reply.get("ok") is True`, :265 asserts the old boolean `state` field, and the `_toggle`/`_breakpoints` helpers (:148-168) send the old param sets. R11 schedules only new probes; without this the standing 7-test green gate goes red.

## R5 — Prover-truth state reads for listings: VERDICT: ADJUST

The folded shape (one Python-visible request, ML round trip internal) and the async handler pattern are right; the non-blocking constraint is verified to hold (snapshot reads plus a stream write on the main loop, reply from the callback/timer — the proof_state/find_theorems discipline at language_server.scala:769-795). Two adjustments:

1. **`PIDE/debugger_breakpoints` must gain token+timeout**, exactly as R4 gives the toggle. The async pattern R5 mandates requires a correlation token and an Event_Timer answering timeout; the current request/reply (lsp.scala:972-995) has neither, so a wedged prover would hang the listing forever — the exact hang class this machinery exists to prevent.
2. **Decide the reply shape, including the timeout outcome, and make R10 match** (see R10). Concretely: `{status, open, breakpoints:[{range, serial, state}]}` where top-level status ∈ ok / timeout / crashed (breakpoints present only on ok) and per-serial state ∈ true / false / unresolvable(status-word).

Also carries the same query_handler-reuse constraint as R4 (one exposure, shared).

## R6 — Refuse eval on a not-stopped thread: VERDICT: ADJUST

Keep the narrowing — it converts the poisoned-queue hazard (input queued forever, executed at an arbitrary later stop) into an immediate refusal. One adjustment, confirmed by two independent findings: **do not reuse `resumed`**. RESUMED is defined as "completion arrived with an empty stack" (debugger.scala:49) — the input was delivered and the expression may have run, side effects included; the refusal sends nothing. Python renders one sentence per status word, so sharing the word forces a lie in one direction — an agent that evaluated a side-effecting expression cannot learn whether it ran, which decides whether to re-send. Coin `not_stopped` (R10 adds it to §7.1; R4 is already coining words this round, so vocabulary economy does not bind). Also add to §7.4 the benign inverse race the check introduces: an eval racing a NEW hit whose debugger_state has not reached the adapter is refused although the thread is stopped — harmless (client retries after the hit notification), and impossible when the client acts on a hit notification it already received, since the state callback precedes the refusal check on the same dispatcher.

## R7 — Bound the stock output buffer: VERDICT: ADJUST (one line)

Verified clean: clear_output is protocol-free (state.change + delay_update posting to an outlet nothing of ours consumes), and ordering guarantees a dispatcher-side clear can never run ahead of the outputs it clears. One addition: **also call `clear_output(thread)` in the debt-clearing branch** (when the counter reaches zero, debugger.scala:143-151). The indebted thread IS the abandoned runaway evaluation — the one whose buffered output is most plausibly large — and neither of the plan's two trigger points covers it.

## R8 — prover_exit via dispatcher + probe timing: VERDICT: SOUND

Verified on every teardown path: protocol_handlers.exit runs on the manager thread (session.scala:623-625), confirming the ghost-thread race is real; in stop() the exit message is fully handled before `prover.await_reset()` returns, and `dispatcher.shutdown()` drains the FIFO mailbox before its None sentinel, so a posted prover_exit always runs; the direct `adapter.exit()` happens before `session.stop()` with the dispatcher alive. No changes.

## R9 — Ranged didChange: VERDICT: REWORK

The direction (ranged sync) stands; the concrete form changes substantially. Two blocking holes, one decided design question, and three hardening rules:

1. **[blocking] Character offsets are UTF-16 code units**, not codepoints — the server does Java String index arithmetic (line.scala:164-235). Astral glyphs DO reach doc.content through the unicode guard's warn-only paths (unicode_guard.py:81-84, :96, :100; verified: `ascii_of_unicode('𝒜 x 中')` is not ASCII, so the ORIGINAL text with 𝒜 = 2 UTF-16 units is pushed). Specify: character = UTF-16 units of the line prefix (`sum(2 if ord(c) > 0xFFFF else 1)`); build ONE shared offset→(line, utf16-column) converter used for every emitted position; unit-test with an astral glyph before the edit point.
2. **[blocking] A rejected ranged didChange is silently dropped server-side** — didChange has no reply, the server catches all throwables into a window/logMessage (language_server.scala:565), while lsp_client.py:1289-1298 has already committed doc.content and refreshed stat_sig: permanent, silent divergence, prover checking phantom text. Today's whole-document didChange makes this class impossible; the draft removes the only self-healing lever. Required: **keep the full-text didChange code path as a recovery form**, and add a divergence hook — in `_surface_server_message`, on a type=1 message containing "Failed to apply document change" (stable text, vscode_model.scala:172), drop stat_sig and force a full-text didChange of the open documents (the force_interrupt self-healing pattern, lsp_client.py:1225-1227). Plus a property test of the range computation against a Python reimplementation of `Line.Document.change` on randomized pairs.
3. **Design question settled: multi-range, not single contiguous range.** The server applies sequential contentChanges correctly (each re-reads the current model — vscode_resources.scala:186-200), so difflib hunks in original coordinates work when disjoint and ordered bottom-up. The draft's cost estimate for single-range is wrong: the whole removed+reinserted slice becomes unparsed commands and everything between two distant hunks gets a fresh Document_ID and re-executes (thy_syntax.scala:122-149, :210-255) — and the stat backstop's batching of multi-edit bursts is precisely what produces distant hunks; in a sledgehammer-heavy theory that is minutes to hours, the pain R9 exists to remove. Land: line-level `difflib.SequenceMatcher`, one contentChange per non-equal opcode, all hunks in ONE didChange (one version bump, one note_edit_sent), descending position order. Record honestly: multi-range widens failure 2 into partial application (foreach aborts mid-list) — the recovery hook covers that too.
4. **Overlap clamp**: trim the common prefix first, then trim the common suffix over at most min(len(old), len(new)) − prefix characters; otherwise "aba"→"ababa" emits start>stop, which the server rejects during decode (line.scala:66-68) and failure 2 swallows. Test "aba"→"ababa" and a duplicated-line case.
5. **EOF anchoring**: newline is separator, not terminator (line.scala:99); deleting the last line with the natural (n,0) whole-line range leaves a phantom trailing newline — silent divergence with no server error. Any hunk whose old side reaches end-of-file without trailing newline anchors at (last_kept_line, utf16-length) and carries/omits the leading "\n". Enumerate the EOF cases in unit tests (delete/replace last line, append without trailing newline, add/remove the trailing newline). The shared converter over character offsets makes these uniform.
6. **Pin the wire shape**: a malformed "range" object silently decodes as the FULL-DOCUMENT form (lsp.scala:293-295) — a serialization bug would truncate the theory to a fragment, not error. One unit test asserting the exact emitted JSON shape, plus a comment at the emission site.
7. **Rewrite the verify-bullets as settled where they now are**: CRLF is safe end-to-end (universal newlines both sides, server normalizes and rejects \r); the diff base doc.content is exactly the sanitized text the server holds (including force_interrupt's mirrored synthetic space — whose healing now emits a minimal hunk, strictly better than today); version/note_edit_sent interplay unchanged, and the `content != doc.content` gate stays as the empty-diff guard the existing tests pin. Only UTF-16, clamp, EOF, silent-drop recovery, and the multi-range mechanics remain open, and this rework closes them.

## R10 — Docs and version: VERDICT: ADJUST

1. **Fix the listing reply contradiction** (found independently twice): §7.1's listing shape becomes `{status, open, breakpoints:[{range, serial, state}]}` per R5's adjustment — "without state" was a change in the OPPOSITE direction from R5, whose entire point is prover-truth states in the listing.
2. §4.13 must now describe the reworked abort: stateless Scala (`no_evaluation` / `aborting`, pending-or-debt check) plus the Python retry loop; delete the outcome-classification vocabulary.
3. §7.1 also gains `not_stopped` (R6), the `unknown_breakpoint` dual-meaning note, and the toggle/listing token+timeout parameters.
4. Version bump to "4" is verified coherent (single constant pair, no other pin anywhere, Python carries none) — keep the caution in R10's text that because the prelude is not in build.props hashes, the bump lands in the SAME commit as the round's last prelude change, not per-item.

## R11 — Probes: VERDICT: ADJUST

1. **Add the migration of existing probes**: update `_toggle`/`_breakpoints` helpers and the shape assertions in tests/integration/test_debugger_probes.py (:148-168, :185, :265) in the same commit that changes each wire shape — R11 currently schedules only new probes, so the standing green gate would go red at the gate itself.
2. Retarget the abort probes at the reworked shape: abort during compile window succeeds via Python retry within ~2 periods; **abort on an indebted thread replies `aborting`, not `no_evaluation`** (pins the Scala check fix); bound-reached honest reply; later-poll `no_evaluation` = settled.
3. Add the R3 guard probe: the `val _` envelope stays silent only because of `Debugger.writeln_message`'s empty-drop — pin it.
4. Add R9's tests: update test_resync_detects_and_pushes_change to assert the ranged shape explicitly (today it passes by luck — the hunk text happens to contain "v2"); offline property test of the diff emitter vs a Python model of Line.Document.change; astral-column, EOF, clamp, multi-hunk-descending cases; exact-wire-shape assertion; one e2e case with two distant edits in one sync asserting both take effect and (reuse probe) the middle commands did not re-run; keep test_file_sync_e2e.py green as the behavioral net.

# Implementation ordering

1. **R1 + R8 + R7 first, as one commit** — dispatcher confinement is the foundation every other item's reasoning stands on (R2's stateless check, R6's threads.value read, the look-up-compare-remove shape all assume it), and R7's trigger points live inside the very branches R1 touches. Include R2's Scala-side check fix (pending-or-debt) here: it is two lines in the same file.
2. **R3 next** — it changes the `Pending` record (drops strip_unit_echo) which R1 also touches (adds serial); doing them adjacently avoids churn. R3 is otherwise independent and fully verified.
3. **R4 then R5** — R4 lands the query_handler exposure once; R5 reuses it. Migrate the existing probe helpers/assertions in the same commits (R11 item 1).
4. **Prelude discipline**: R3, R4, R5 all touch mcp_prelude.ML — sequence them contiguously and bump the version to "4" exactly once, in the same commit as the last prelude change, with the jar rebuild and check_component gate.
5. **R6** any time after R1 (needs the dispatcher-side threads read); its `not_stopped` word ships with the R10 §7.1 update.
6. **R2's Python retry loop** lands with the abort tool (Phase C timing is fine); the Scala side is already correct after step 1, and the integration probes for it come with R11.
7. **R9 is fully independent of the debugger items** and can proceed in parallel: build the shared offset→(line, utf16-column) converter and the offline property tests FIRST, then the multi-range emitter, then the divergence-recovery hook, then the e2e cases. Do not start the emitter before the converter tests pass — every EOF/astral/clamp case falls out of the converter.
8. **R10 last**, once the wire shapes have stopped moving.
