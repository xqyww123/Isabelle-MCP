# ML Debugger — Review Outcome and Decisions Taken

Status: **the debugger work is shelved as of 2026-08-11**, to be resumed after the
separate upgrade of the existing query tools and the evaluation-target reporting.
This file is the resume-from record: it holds everything decided or discovered
after [`DEBUGGER_DESIGN.md`](DEBUGGER_DESIGN.md) and
[`DEBUGGER_IMPLEMENTATION_PLAN.md`](DEBUGGER_IMPLEMENTATION_PLAN.md) were written,
none of which has been folded into those two documents yet.

**Do not read `DEBUGGER_DESIGN.md` at face value.** Ten review concerns against it
survived an adversarial debate (§1 below) and its §6.4 in particular rests on a
premise now believed false. Read this file alongside it.

---

## 1. Review outcome

A two-turn adversarial review (four independent reviewers by lens, consolidation,
one adversarial refuter per finding, then a judge) produced 30 raw findings,
consolidated to 10, all of which survived the debate in upheld or corrected form.
Four were downgraded in severity by the debate; 22 sub-claims were rejected
outright (§1.3) and must not be re-litigated.

### 1.1 Major concerns

**(1) §6.4's premise is false — a thread stopped in the debugger loop IS
interruptible.** `debugger_loop`'s `uninterruptible_body` wrapper reinstalls the
entry attributes for the loop body, and the loop blocks in `Synchronized.guarded_access`
→ `Multithreading.sync_wait`, which explicitly allows and re-raises interrupts
(`thread_attributes.ML:106-111`, `multithreading.ML:59-68`, `debugger.ML:231-238`).
The wrapper exists only so the trailing `debugger_state` (empty stack = resumed) is
still emitted before the interrupt propagates. Consequences: the whole "cancel first
resumes all stopped threads, temporarily disables all armed sites, restores them
afterwards" design is unnecessary and actively harmful (it resumes a thread stopped
just before a side-effecting call, letting it run on instead of being cut off);
the "restore afterwards" step cannot even be issued on this repository's cancel path
(the synthetic line-0 edit leaves every downstream command with a fresh unexecuted
exec, while `Debugger.breakpoint` requires the enclosing command finished), and its
failure would be invisible (rewritten into an uncorrelated system message); and the
undefined "disable window" contradicts §1's registry-is-source-of-truth invariant.
*Fix*: delete the premise and the workaround; cancel runs the existing path
unchanged; a thread leaves the stopped-thread table when it disappears from the
pushed debugger state. If a fallback is ever wanted, Isabelle's own mechanism is
`Debugger.exit` (`debugger.ML:258-260`), not per-thread resume.

**(2) §4.11 promises that a step always stops again; often it cannot.** Stepping
only stops inside compiler-instrumented code, and `step_out` merely sets a
shallower stack-depth bound — at the outermost instrumented frame it can never fire
again. Stepping off the last statement of an `ML ‹…›` block leaves instrumented
code entirely: the thread runs to completion and the tool's declared result (the
new stack) does not exist. jEdit makes the same promise but only in an asynchronous
dockable, where nothing-happens is harmless; a synchronous tool inherits an
unsatisfiable contract. Worse, the prover's stepping flag is thread-local, written
with a bare set (no scoping) and cleared only by `continue` at a later break, while
worker threads are long-lived and reused across unrelated tasks — a worker can carry
the flag into an unrelated command and stop there.
*Fix*: specify two outcomes with a wait bound ("stopped again at …" / "the thread
resumed and did not stop again — execution left the instrumented region"), reword
`step_out` as "run to the next breakpoint site at a shallower stack depth (which may
not exist)", warn that stepping only stops in ML compiled with debugging in this
session, define a stray stop as a reportable event, and record that sending
`continue` to that thread clears the flag at its next break.

**(3) §7's eval rendezvous has no completion signal and no request correlation.**
A timeout is the only stated criterion, and debugger output carries only a thread
name plus an emission-time serial. The Scala side clears the thread's output buffer
at *request* time, so when one evaluation exceeds the timeout and a second is
issued, the first's remaining output lands in the freshly cleared buffer and is
returned as the second's result — a silently wrong inspection result at a
breakpoint. A real completion signal is unused: after `eval`/`print_vals` the ML
loop re-emits a full `debugger_state` for that thread.
*Fix*: clear output, send the verb, treat the next `debugger_state` for that thread
as end-of-output, demote the timeout to a failure bound, and fence late output with
a request generation counter. Also correct §7's "an empty stack for a thread means
it resumed": the Scala side removes the entry entirely, so resumption is the
thread's *absence* from the pushed array.

**(4) A breakpoint cannot stop the command that compiled it, and as specified is
armed only after the round that would have hit it.** Arming requires the enclosing
command to have finished, while every recompilation mints fresh sites — so
straight-line top-level ML in the command being evaluated can never be stopped at,
in any round. Additionally §5 runs reconciliation only after a whole evaluation
round, so with a function defined in one `ML ‹…›` block and called in a later one,
the breakpoint arms only after the caller has already run. The second half is not
inherent: arming needs only the *enclosing* command finished.
*Fix*: run reconciliation incrementally during an evaluation (as commands report
finished) so a later caller in the same round can hit; and state in §2.2, in the
`isabelle_set_breakpoint` description and in the pending/re-armed notices that a
breakpoint takes effect only for code entered from a command that runs after the
compiling command finished.

**(5) The prescribed remedy "re-launch with debug=true" is silently ignored.**
`isabelle_launch` short-circuits on session identity alone (`server.py:275-278`),
returning before touching any client field, so the agent gets a successful
SessionInfo and is then refused again by the breakpoint tools. No result field
exposes the live debug state.
*Fix*: see the decision in §2.1 below.

### 1.2 Minor concerns

**(6) A resume/step verb for an explicitly named thread is an unvalidated
pass-through.** The prover queues debugger input per thread name unconditionally and
never discards it, so a verb aimed at a thread that already resumed waits in the
queue and is obeyed at that thread's *next* stop — possibly in an unrelated command,
with no error at any layer. *Fix*: subsumed by the stop-identifier decision (§2.2).

**(7) Whether a paused evaluation counts as active is never stated**, so the six
pre-existing query tools' behaviour during a stop is undefined; the default reading
refuses all of them, for the whole duration, even for long-processed lines.
*Fix*: see the decision in §2.3.

**(8) The wait loop's third exit condition fires on ANY stopped thread**, with no
rule tying a stop to the evaluation in flight — a background re-evaluation's stop
would make a later `evaluate_to` on an unrelated file return a hit report about it.
Related: `debugger_state` carries raw positions, which for theory-embedded ML are
exec-id based, so resolving them to file+line requires a snapshot and can only be
done on the Scala side — the spec assigns that to no layer.
*Fix*: make position→node resolution an explicit Scala-side responsibility; exit the
wait loop only for a stop in the current evaluation's theory set; other stops become
debug notices; an unattributable stop fails open (exits) as today.

**(9) §7 does not say who computes the toggle acknowledgement or how the absolute
`state` maps onto the flip-shaped Scala helper**, and §5's reconciliation triggers
omit prover relaunch (every serial dies with the prover, and only `pending` entries
have a recovery rule, so a relaunch sends everything to `lost` forever).
*Fix*: state that the ok/error is computed by the adapter from its own snapshot, and
that the absolute state is realized as "read the current breakpoint state, toggle
only if it differs"; add prover relaunch to the reconciliation triggers with an
armed→pending transition, and add a lost→armed recovery rule. **Also fix
`DEBUGGER_IMPLEMENTATION_PLAN.md` Phase 1**, which currently describes a bare flip
with the state argument dropped — shipping that would make a second
`isabelle_enable_all_breakpoints` disable everything.

**(10) `lost` registry entries are undeletable on a literal reading**, because §4.3
defines deletion by resolution against live sites while a `lost` entry has none.
*Fix*: identify the entry by file + recorded line + anchor snippet (so `pending` and
`lost` are deletable); a no-match errors naming the nearest entries; an ambiguous
match lists candidates and refuses. Separately, fix the anchor snippet's extent,
which §1/§3/§4.4 leave as "a short stretch" with no length.

Two further findings were cut only by the ten-item cap and should be fixed too:
the structured results need a `notices` field with no existing precedent in the
models (the repository's uniform warning-injection middleware is the reuse target),
and `at_text`'s nearest-before search must be **bounded to the line** — with no
site at or before the anchor on that line it is an error listing the line's
available sites, consistent with the omitted-`at_text` rule.

### 1.3 Rejected in the debate — do not re-litigate

Notable factual corrections to the reviewers themselves: Isabelle worker thread
names are unique per thread via a global counter (not recycled pool slots); the
prover-side breakpoint command is set-to-value, not a flip; `Debugger.Handler` is
installed in `Session`'s own constructor, so registering another protocol function
for the same markup would throw at init; the breakpoint `bool ref` is minted by the
Poly/ML compiler, only the serial is minted by Isabelle; `isabelle_evaluate_to`
waits for a frontier, so "just evaluate twice" does not re-execute anything; and
promoting the global break switch into v1 re-litigates declared scope.

## 2. Decisions taken in conversation

### 2.1 The `debug` launch parameter is part of the session identity

`debug` joins the launch-identity check. When the requested session name matches
the running one but the `debug` value differs, **`isabelle_launch` errors** — in
both directions — telling the agent to call `isabelle_terminate` first and then
launch with the wanted `debug` value. No automatic teardown-and-restart, so a
routine launch can never silently kill a running debug session. `SessionInfo`
gains a `debug: bool` field so every launch/session-info result reports the live
state.

Open sub-question, my reading pending confirmation: the error applies only when the
running session would otherwise be reused. A request naming a *different* session
already implies a relaunch the agent asked for, so `debug` simply applies to the
new prover, without an error.

### 2.2 Stops are identified by a stop identifier, not by a thread name

Isabelle evaluates in parallel, so several workers can be stopped at once, and a
thread name cannot distinguish "the stop I was looking at" from "a later stop of the
same thread". Terminology: a **stop** is one occasion of a thread halting in the
debugger; each stop gets a **stop identifier**.

- Every debugger tool takes `stop_id` instead of `thread`; the thread name stays in
  reports as information, never as an input.
- A stop identifier is created when a thread halts with no live stop of its own;
  a step **keeps** the same identifier (a controlled resume-and-restop inside the
  same stop); it is retired when the thread resumes via continue, is swept up by
  cancellation, or steps without stopping again.
- Using a retired identifier is an error naming how that stop ended — instead of
  queueing a verb in the prover that would fire at an unrelated later stop, which
  is exactly review concern (6).
- `stop_id` may be omitted when exactly one stop is live; omitting it in
  `isabelle_continue_breakpoint` still means "resume all live stops".
- The hit report lists **all** live stops, each with identifier, position, thread
  name and call stack. New stops occurring while the agent inspects ride on the
  debug-notice channel.

### 2.3 Guard policy while stopped

Four of the six query tools are position-explicit and never move the caret:
`isabelle_hover`, `isabelle_definition`, `isabelle_local_occurrences`,
`isabelle_command_output`. Two move the caret, but only in their second half —
`isabelle_goal` takes the command span caret-free and only the proof state needs
the caret; likewise `isabelle_find_theorems`.

Decided: the four caret-free tools are served whenever the file is already open and
the target line is processed (running lines keep the existing incomplete-output
warning), and `isabelle_evaluate_to` is refused outright while any stop is live —
not only because the frontier cannot advance, but because even a target that is
already processed would move the caret. What `isabelle_goal`'s proof-state half and
`isabelle_find_theorems` should do while stopped is pending experiment (see §4).

This policy is a special case of a broader upgrade to the existing tools, which is
the work that displaced this one; see §3.

## 3. Why this is shelved

Discussing the guard exposed a gap that is not specific to debugging: the MCP has an
evaluation target internally (`evaluation_state.file_path` + `destination_line`) but
never reports it, so an agent cannot tell what the server is working toward or why a
query was refused; and the caret-dependence of two query tools blocks them during
any evaluation, not merely during a debugger stop. That upgrade is being designed
first, and the debugger design must be re-checked against its outcome — in
particular §2.3 above and review concerns (7) and (8).

## 4. Probe list, updated

The Phase 0 probes in `DEBUGGER_IMPLEMENTATION_PLAN.md` still stand, with these
changes:

- Probe 5's expectation is **reversed**: the hypothesis is now that the existing
  cancellation path *does* interrupt a stopped thread. Measure it.
- New probe: with a thread stopped at a breakpoint, move the caret elsewhere and
  observe whether the stopped command's execution is discarded and the thread
  disturbed. This decides §2.3's pending half.
- New probe: how a command stopped at a breakpoint is decorated (running,
  unprocessed, or otherwise) — the paused-status wording depends on it.
- New probe: whether the prover's stepping flag really leaks onto a later,
  unrelated task of the same worker (review concern 2).
