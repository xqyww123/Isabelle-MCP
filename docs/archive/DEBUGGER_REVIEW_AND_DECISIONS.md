# ML Debugger — Review Outcome and Decisions Taken

Status: **folded 2026-08-13 — this file is now the historical record only.**
The work was shelved on 2026-08-11 pending the query-tools upgrade; that
upgrade shipped, the work resumed, and everything in this file — the review
outcome (§1), the decisions of §2 and §2bis, and the source-study findings —
has been folded into the rewritten [`DEBUGGER_DESIGN.md`](DEBUGGER_DESIGN.md)
and [`DEBUGGER_IMPLEMENTATION_PLAN.md`](DEBUGGER_IMPLEMENTATION_PLAN.md).
**Those two documents are authoritative; where this file disagrees, they win.**
Section references like "§6.4" below point into the superseded 2026-08-11
draft of the design, whose numbering the rewrite did not keep. §1.3's list of
rejected claims remains binding: do not re-litigate them.

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

Confirmed 2026-08-13: the error applies only when the running session would
otherwise be reused. A request naming a *different* session already implies a
relaunch the agent asked for, so `debug` simply applies to the new prover,
without an error.

Still open: the launch-identity check currently compares the session name only
(`server.py:315`); `session_dirs` does not take part either. Whether `debug` is
special-cased or the identity check is made whole is undecided.

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

Superseded in part on 2026-08-13 — see §2.4.

---

## 2bis. Decisions taken on resumption (2026-08-13)

The query-tools upgrade that displaced this work has shipped, so the work
resumed. Everything below was decided in conversation after that.

### 2.4 All query tools are served while a thread is stopped

§2.3's split into "four caret-free tools and two that move the caret" is
obsolete: after the upgrade **all seven** query tools (the six of §2.3 plus
`isabelle_command_status`) are position-explicit and move no caret, and the
guard decides per requested position rather than by a global flag
(`check_evaluation_guard`, `evaluation.py:1181`).

Decided: `isabelle_goal`'s proof-state half and `isabelle_find_theorems` are
served while a thread is stopped, exactly like the rest. Three cases, all of
which the shipped machinery already handles and none of which needs new
behaviour: a line whose command finished before the stop is answered normally;
the command that is itself stopped at a breakpoint answers `unfinished`, since
it has not finished; a line not yet evaluated is refused by the guard, whose
message names the current evaluation target.

`isabelle_evaluate_to` stays refused while any stop is live — that half of §2.3
is unaffected, and its reason survives the upgrade: `evaluate_to` still moves
the caret (`evaluation.py:757`).

One measurement gates this. Both tools now compute their answer in a forked ML
task inside the prover (`Isabelle_MCP.proof_state` / `Isabelle_MCP.find_theorems`,
urgent priority), while a thread stopped in the debugger loop holds a worker
slot. Whether such a fork is still scheduled when several workers are stopped
is unknown and must be measured, not inferred. If it starves, the fallback is
a fast, explicit failure on the Scala side rather than a hang.

### 2.5 Probes are integration tests, not scaffolding

The implementation plan's Phase 0 called for temporary probe scaffolding to be
removed before release. Replaced: Phase 0 and Phase 1 merge, the real Scala
requests are written first (they are thin adapters over `session.debugger`
by design), and the probes are integration tests driving those requests raw
from Python — the precedent is `tests/integration/test_query_tools_e2e.py:233`,
which drives `PIDE/find_theorems_at_position` directly with no MCP tool in
between, and which is how a real defect was caught during the upgrade.

Nothing is thrown away, and the probes become permanent regressions on
assumptions an Isabelle upgrade could silently invalidate. They never ship as
runtime code: they live in `tests/integration/`, marked `integration`,
deselected by default, skipped without `isabelle` on PATH. The gate is
unchanged — if probe 1 or 2 fails, stop and revisit the specification.

Rebuilding our own jar is treated as costless. Modifying the Isabelle
distribution is not permitted.

### 2.6 A breakpoint can only be set on evaluated code; there are no pending entries

The prover requires the enclosing command to have finished before a site can be
toggled (`Command.eval_finished`, `debugger.ML:279-288`, which otherwise errors
"Bad exec for command"). Decided: `isabelle_set_breakpoint` on a location with
no live toggleable site is an **error** telling the agent to evaluate first. The
`pending` state of §5 is therefore never created by the agent.

Note that the state itself cannot be abolished, only its creation: an armed
entry loses its site whenever the enclosing command is edited and re-evaluated,
and after a prover relaunch nothing has sites at all.

Decided with it: §5's three states collapse to **two — `armed` and `pending`**.
`lost` is deleted. `pending` keeps its meaning unchanged ("no live site right
now; it arms as soon as one appears"), which is exactly what both former states
mean once `pending` can no longer be created by the agent. The *reason* there is
no site — the file has not been evaluated in this prover, the enclosing command
is still running, the code was edited away — is carried as a sentence in the
debugger notice and in the listing, not as a state name.

Rationale: at the moment reconciliation runs, "the file was never evaluated
here" and "the code is gone" are indistinguishable, so a two-way split would
rest on unreliable inference; the agent acts on the reason sentence, not on the
state name; and the collapse removes the transition table in which review
concern (9) found the "a prover relaunch sends everything to `lost` forever"
defect.

Two consequences to state in the tool descriptions: hitting a breakpoint
requires running the code **twice** (once to compile the sites, once to hit
them), and the command that compiled a site can never be stopped at.

### 2.7 The eval rendezvous has a timeout that really interrupts

Established by source study (report of 2026-08-13, cited against the
distribution): there is no timeout anywhere on Isabelle's debugger path; the ML
loop blocks in `Synchronized.guarded_access` with no time limit and the Scala
side never waits. jEdit needs none because it is fire-and-forget with a human
as the completion detector — a mechanism a synchronous tool cannot copy.

Decided: the rendezvous gets an explicit timeout, **default 3 minutes**,
settable per call by a request parameter, implemented in the Scala adapter.

The primary thing it must protect against is an expression that never
terminates, so **the timeout must actually interrupt the working thread** — a
local give-up that leaves the worker wedged forever is not acceptable.

### 2.8 Terminology: debugger notice

The glossary term is **debugger notice**, not "debug notice"; the section
appended to text results is headed `Debugger notices:`.

### 2.9 Result rendering: YAML text, no structured output

Separate from the debugger, decided while settling how notices are delivered:
the seven tools that currently declare an output model (`isabelle_launch`,
`isabelle_session_info`, `isabelle_hover`, `isabelle_definition`,
`isabelle_local_occurrences`, `isabelle_goal`, `isabelle_find_theorems`) drop
`output_schema` and return **YAML text**. The six narrative tools are unchanged.
Tool functions keep returning their models internally; only the MCP boundary
changes, and the serialisation lives in one shared helper in
`utils/formatters.py`. `pyyaml` joins the runtime dependencies in
`pyproject.toml` and the conda recipe. `allow_unicode=True` is mandatory —
Isabelle output is full of Unicode symbols and the default escapes them.

This is to be done and committed as its own change **before** any debugger tool
is written, so the new tools are born consistent. It also dissolves the
`notices`-array question: debugger notices are simply text.

### 2.10 Breakpoint addressing and the anchor snippet

- A site is presented as `line` + **anchor snippet**, printed as
  `before ‹fold upd args›`. No tool accepts or returns a column number.
- The snippet is taken from the statement's start, **whole ML tokens only**,
  adding tokens until the snippet is unique within its line. No length cap: the
  snippet provably never needs to cross the line, since a text equal to the rest
  of the line cannot occur twice in that line.
- **Uniqueness, not the weaker "does not occur earlier on the line".** The
  weaker condition round-trips on its own, but it is unsafe against the `at_text`
  rule below: a snippet occurring again later on the line, with a site in
  between, would resolve differently at that later occurrence and so be rejected
  by our own rule. Uniqueness leaves exactly one occurrence and is therefore
  always accepted.
- **`at_text` ambiguity is refused only when it matters**: if the snippet occurs
  several times on the line but every occurrence resolves to the same site, it is
  served; if the occurrences resolve to different sites, it is an error listing
  the line's sites (the fourth message of §2.10's set). Repetition that cannot
  change the answer — `val c = f x + f x` — is common and must not be rejected;
  repetition that can — `val a = g x; val b = g x` — must not be resolved
  silently. This diverges from `isabelle_hover`'s `symbol` and
  `isabelle_evaluate_to`'s `after_text`, which take the first occurrence
  unconditionally; the divergence is accepted because a misplaced breakpoint
  costs a whole debugging round trip.
- **Batch deletion is best-effort**: entries that match are deleted, and the
  result reports what did not — `deleted 4; 1 matched no entry: …`. A reference
  matching several entries is skipped and reported, never guessed. Rationale: an
  agent passing back a whole listing should not have the call fail because one
  entry was already gone.
- **`isabelle_set_breakpoint` stays singular.** Setting has four distinct
  failure causes per position, and pluralising it would turn them from errors
  into rows of a per-item report for no proportionate gain.
- Any truncation marker goes **outside** the cartouche (`before ‹…›…`), so what
  is inside can always be copied back verbatim.
- Listings show at most **8** sites, 4 before and 4 after the requested line,
  with no backfill when one side has fewer; a truncated listing says so and
  points at `isabelle_list_breakpoints`.
- `isabelle_del_breakpoint` becomes **`isabelle_del_breakpoints`**, taking a
  list of references (the `isabelle_command_status(positions: list[...])`
  precedent). Each is matched against the **registry** — file, recorded line,
  anchor snippet — never against live sites, so an entry with no site is still
  deletable. There is no "delete all" tool: list, then pass the lot.
- No `isabelle_del_all_breakpoints` and no "detach" tool: both are reachable
  from existing tools, and a tool slot is a cost paid on every request. The
  tool count stays at ten.

### 2.10bis Listing the registry and listing the sites are two tools

§4.4's single tool did two unrelated jobs — reporting the breakpoints you have
set, and reporting the compiler-chosen locations where a breakpoint *can* go.
Split into two. The tool count becomes eleven.

Discovery matters more than the original design assumed: setting a breakpoint
where no site exists is now an error (§2.6), and top-level `val`/`fun`
declarations carry no sites at all (§2.14), so the agent cannot work out from
the source alone where a breakpoint may go.

- `isabelle_list_breakpoints(file_path: str | None = None)` — the registry;
  the optional path filters to one file.
- `isabelle_list_breakable_sites(file_path, start_line?, end_line?)` —
  `file_path` is **required** (sites are per file); the range defaults to the
  whole file.

The site listing reports sites for the evaluated part of the range and states
separately which line ranges have no sites merely because they are not evaluated
yet — reusing the per-position status machinery `isabelle_command_status`
already has. It shows at most **40** sites, taken in source order, and says so
when it truncates, naming the line to resume from.

### 2.10ter Output shape: text results, formatted by hand

All eleven debugger tools are **text results** (`output_schema=None` plus a
formatter). §4's split — structured for `isabelle_list_breakpoints` and
`isabelle_debug_state`, text for the rest — is dropped, and `models.py` gains
nothing for the debugger. This is deliberately *not* the same thing as §2.9's
YAML change to the seven pre-existing tools: those serialise a model, these are
formatted prose. Neither style is to be imposed on the other.

Consequence: these outputs are hand-formatted, so §2.9's "let the serialiser
handle quoting" does not apply to them; the cartouche is the delimiter.

One item per line, location first, then the attributes that decide what to do:

```
breakpoints:
  - Foo.thy:14 before ‹fold upd args›, enabled, armed
  - Bar.thy:7 before ‹Symtab.update tab›, enabled, pending (not evaluated yet)
  - Baz.thy:22 before ‹writeln msg›, enabled, pending (still evaluating)
  - Qux.thy:9 before ‹the_default 0 x›, enabled, pending (code not found — it
    may have been edited away; delete this breakpoint or set it again)
```

```
sites:
  - line 14 before ‹fold upd args›, already enabled
  - line 15 before ‹writeln msg›, already set but disabled
  - line 17 before ‹Symtab.update tab›, breakable
not_evaluated: lines 20-46, lines 58-73 — evaluate up to those lines to see their sites
truncated: showing 40 of 137 sites, lines 14-52 — narrow the range (e.g. start_line 53) to see the rest
```

`not_evaluated` and `truncated` are omitted entirely when empty.

The cartouche is a **delimiter, not decoration**: an anchor snippet may itself
contain commas, so without it the line could not be split back into its parts.
(Edge case for the implementation: ML source can contain nested cartouches and
token-boundary truncation could cut inside one.)

The site listing's third field is three-valued, because "a breakpoint is
attached" and "it will actually stop" are different questions — a
disable-all leaves entries attached but off. No further identification of the
attached breakpoint is needed: a site and the breakpoint on it share file, line
and anchor snippet exactly.

**Reasons are short tags, explanations live in the tool description.** A prover
relaunch turns every registry entry pending at once, so a full sentence per row
would repeat a dozen times and stop being read. The two self-resolving causes
get short tags (`not evaluated yet`, `still evaluating`); the one that needs a
decision from the agent — the code was edited away — stays long, because it is
rare and rarity is cheap. The same tags are used verbatim in the debugger
notices: one concept, one wording, with the full explanation stated once in the
tool description.

### 2.11 Registry lifetime

Cleared when the MCP server process restarts (it is in memory only). **Retained**
across a prover relaunch and across a session switch: entries become `pending`
and re-arm when the file is evaluated again, each with a debugger notice saying
why it is not armed.

### 2.12 The eval timeout is enforced inside the prover, not by interrupting

Superseding §2.7's "the timeout must actually interrupt the working thread".
Three independent source studies agree that **an interrupt cannot be contained
to the eval**: `Debugger.error_wrapper` uses `Exn.result`, which re-raises
interrupts (`debugger.ML:32-35`, `exn.ML:125-126`), so any interrupt escapes the
debugger loop and kills the command being debugged. Stock Isabelle has no
"abort the eval, stay at the breakpoint" path.

Decided instead: **we construct the ML text sent to `Debugger.eval`**, so the
agent's expression is wrapped before it is sent. `Timeout.TIMEOUT` is an
ordinary exception, so the error wrapper catches it, prints it, and the loop
returns to waiting for input — **the thread stays parked at the breakpoint and
remains debuggable**, no command dies, nothing is abandoned, and no exec id ever
has to be identified.

The wrapper is a function in **our own prelude**, called as
`Isabelle_MCP.debug_eval (Time.fromSeconds n) (fn () => ⟨expression⟩)`. It does
three things in one place: registers the running thread in a prelude-side table
keyed by the debugger's own thread name (absorbing what would otherwise be a
separate bootstrap round trip, and subject to a hard ordering rule — it must
happen before the agent's expression runs, since a runaway thread no longer
reads its input queue); enforces the deadline; and watches an abort flag so an
on-demand abort is possible. Both outcomes become ordinary exceptions.

Two implementation requirements, both from the adversarial review:

1. **The wrapper must set the interrupt attributes explicitly**
   (`Thread_Attributes.private_interrupts`), never inherit them. Poly/ML's
   `InterruptAsynchOnce` really is once — the bundled documentation says so
   (`contrib/polyml-5.9.2-2/src/basis/Thread.sml:91-97,123-127`) — and nothing
   in the debugger loop re-arms it, so a second runaway would be unkillable;
   and a breakpoint taken inside a deferred-interrupt region would be
   unkillable from the start. Setting the attribute re-arms it.
2. **If an interrupt is ever used as a fallback, `Execution.discontinue ()`
   must come first.** Without it the theory tail is permanently and silently
   poisoned: the interrupted command's result is memoised as a failure that
   `Lazy.is_finished` reports as done, every following command raises when it
   reads the previous result and is memoised the same way, and none of them
   emits status markup, so the node never consolidates and only an edit **at or
   before** the interrupted command recovers it. The existing global
   `Isabelle_MCP.cancel_execution` avoids this precisely because it
   discontinues first.

Known limit to state in the tool description if the go/no-go probe confirms it:
a tight allocation-free loop may offer no safe point, in which case no timeout
can reach it and only the global cancellation remains.

Also recorded: the global "break at the next site" switch stays out of v1, now
with a hard reason — a breakpoint taken on the single protocol thread deadlocks
the whole session, since the thread that would read our resume command is the
stopped one.

### 2.13 `isabelle_eval_at_breakpoint` takes an expression, not arbitrary ML

Confirming §4.8's existing wording. Temporary bindings are written
`let val x = … in … end`, which is an expression. This also keeps the wrapper's
shape simple (the expression goes inside `fn () => …`), and it costs nothing,
since a binding could not survive to the next evaluation anyway — each eval
rebuilds its context and restores it afterwards (`debugger.ML:150-177`).

### 2.13bis Terminology: breakable site; and the approved refusal messages

The glossary term for the compiler-inserted stopping location is **breakable
site** (superseding `DEBUGGER_DESIGN.md` §1's "breakpoint site"), keeping it
visibly distinct from **breakpoint**, the registry entry. The two would
otherwise differ by one word and agents would conflate them; the distinction is
our own, so it must carry its own weight — Isabelle itself does not make it
(its markup is `ML_breakpoint`, jEdit's tooltip says "breakpoint (enabled)").
Hence also the tool name `isabelle_list_breakable_sites` and the site listing's
third value `breakable`.

The approved `isabelle_set_breakpoint` refusal messages, final wording (each
lives in Python, unit-tested verbatim; `{where}` is `file:line`):

1. Line not evaluated yet:
   > There is no breakable site at {where} — that line has not been evaluated
   > yet. Breakpoints can only be set on code the prover has already compiled,
   > so evaluate the file first.
2. Command still running:
   > There is no breakable site at {where} yet — the command there has not
   > finished evaluating. A site can only be used once its command has
   > finished. Retry in a few seconds.
3. Evaluated, but no site on that line:
   > The command at {where} has been evaluated, but the compiler placed no
   > breakable site on that line. Breakable sites exist only inside ML code, at
   > statement boundaries the compiler chooses. The nearest breakable sites in
   > this file are:
   >
   >       line 14 before ‹fold upd args›
   >       line 17 before ‹writeln (string_of_int n)›
   >
   > Pass one of these as line + at_text.

   (nearest 8 sites, 4 each side of the requested line, no backfill; when the
   whole file has none, the tail is replaced by:)
   > This file has no breakable sites at all — it contains no ML code that was
   > compiled in this prover.
4. `at_text` given but no site at or before it on the line:
   > There is no breakable site at or before ‹{at_text}› on {where}. The sites
   > on that line are:
   >
   >       before ‹fold upd args›
   >       before ‹Symtab.update tab›
   >
   > Pass one of these as at_text, or omit at_text to use the first site on
   > the line.

Truncated listings elsewhere point at the site tool:
> (12 more breakable sites in this file — use isabelle_list_breakable_sites to
> see them.)

The approved `expr` parameter description of `isabelle_eval_at_breakpoint`:
> Isabelle/ML expression to evaluate in the frame's scope. Declarations are not
> accepted — to bind a temporary, write `let val x = … in … end`. Bindings do
> not survive to the next call.

### 2.13ter Final decisions of the resumption round (2026-08-13, late)

- **`hit` replaces `stop`.** One **hit** is one occasion of a thread halting in
  the debugger; the parameter is `hit_id`. Chosen over "stop" for its strong
  prior ("breakpoint hit" is standard debugger vocabulary and the hit report's
  first words), and over the proposed "execution id", which collides with
  PIDE's exec id — an existing, different concept in this very subsystem. A
  step keeps the same hit; a halt not caused by a breakpoint (stepping-flag
  leak) is an anomaly with its own wording. Everything §2.2 says about stop
  identifiers holds verbatim under the new name.
- **Concept teaching lives in the MCP server instructions** (`instructions.py`),
  not inline in the hit report: one section explaining hit / frame / breakable
  site, drafted at implementation time. The hit report tail stays lean and
  does not define terms.
- **Call stacks label frames with the parameter name**: the stack header reads
  `Call stack (innermost first; the number is the frame parameter):` and each
  row starts `frame 0`, `frame 1`, …, so the report and the schema share the
  exact string. Same principle as `line … before ‹…›` ↔ `line` + `at_text`.
- **`isabelle_abort_eval_at_breakpoint`** is added (tool count: twelve): sets
  the wrapper's abort flag; the evaluation ends with an ordinary error at its
  next safe point and the thread stays at the breakpoint. Errors when nothing
  is being evaluated on that hit, and on a retired hit id. Honest limits in
  the description: same safe-point caveat as the timeout, and its main uses
  are parallel-call clients and clearing the debt when only the Scala backstop
  fired.
- **`session_dirs` stays out of the launch identity** (rejected); `debug` is a
  special case in the identity check.
- **`expr` description is just** "Isabelle/ML expression to evaluate" — the
  longer draft explained internals no reader could use.
- Self-decided under standing approvals: prover-side eval timeout default
  180 s (per-call `timeout` parameter on eval **and** locals), Scala backstop
  210 s; implicit frame-0 locals fetch 10 s and never aborts; step/continue
  wait bounds never abort (a thread that does not stop again is a normal
  outcome, reported as such); review fixes (4) and (9) adopted as written;
  no new status word for "previous evaluation still outstanding" — it is a
  sentence, not a token; `query.scala` untouched, the debugger machinery is
  written alongside it.

### 2.13quater The second review round and its decisions (2026-08-14)

A two-turn adversarial review of the rewritten specification (four reviewers by
lens, then one refuter per finding batch) produced ~30 findings, of which 6
were killed outright, 4 merged, and ~20 survived — most weakened. All surviving
fixes were folded into the specification on 2026-08-14. Decisions taken in
conversation during that fold:

- **Locals go through the eval verb.** The promised prover-side timeout on
  `isabelle_locals_at_breakpoint` had no mechanism under the `print_vals`
  routing (the printing runs in the debugger loop's own code). Decided: a
  ~10-line prelude locals printer (via `PolyML.DebuggerInterface`, reachable
  from the prelude; `printWithType` at `ML_print_depth`, output identical to
  stock `print_vals`) called through the **eval verb** under `debug_eval` — a
  stated, bounded exception to "no debugger logic is reimplemented".
- **Per-value print timeouts.** Inside the locals printer, each variable gets
  its own short bound (5 s); a value that cannot be printed in time renders as
  `<printing timed out>` (no type — type layout can be the slow part) while
  the rest still print. The stock printers prune *output*, not *computation*
  (`ml_pp.ML` builds the full pretty tree before `ML_Pretty.prune`), so depth
  does not bound cost; the whole-call `timeout` stays as the outer guard.
- **Every hit in a report carries frame-0 locals**, fetched concurrently (the
  rendezvous is per-thread), all under the one 10 s bound.
- **The manual arming model.** Background reconciliation no longer re-arms
  anything: when recompilation (or relaunch, or cancellation) kills sites,
  entries are demoted to pending with a notice — and stay there until an
  **explicit tool action** arms them (`isabelle_set_breakpoint` for new
  entries, `isabelle_enable_all_breakpoints` for existing ones). This
  supersedes review fix (4)'s incremental re-arming, dissolves the same-round
  arming race (arming now happens between evaluation rounds, no opponent) and
  the dark-sites-in-background-runs finding, and shrinks the registry's
  concurrency surface (the background never toggles sites). The cost — a
  forgotten re-enable makes a run miss silently — is fenced by a warning line
  on `evaluate_to` whenever enabled-but-unarmed entries exist in the target
  file.
- **`pytest` deselects integration tests by default for real**: `addopts`
  lands in `pyproject.toml`, making the long-claimed behaviour true (the
  cross-suite state leak this prevents was actually observed during Phase Y).
- The step/continue wait bound is **30 s**.

### 2.14 Facts about breakable sites that change the design

From a source study of `ml_compiler.ML` and the bundled Poly/ML, pending
probe confirmation:

- **The reported position is shifted one symbol left of the statement's first
  character** (`ml_compiler.ML:41-50` decrements the offset). It normally lands
  on whitespace, and on the *newline ending the previous line* when the
  statement starts in column 1. Anchor at the range's **end**, not its start;
  otherwise column-1 statements yield an empty anchor.
- **Derive the line from the corrected position too.** PIDE strips position
  properties when incorporating reports (`command.scala:333`), so the markup's
  own (correct) line is not visible to us, and using the range start would
  report column-1 statements one line early.
- **The markup carries no extent** — Isabelle discards Poly/ML's end offset
  (`Position.no_range_position`). The anchor snippet cannot be derived from a
  statement span; it must be tokenised forward by us.
- **Top-level declarations have no sites at all.** Sites exist in `let`/nested
  `local` bodies, expression sequences, conditional branches, `while` bodies,
  match alternatives and `fun` clauses — not on top-level `val`/`fun`. This must
  be said in the tool descriptions; it also corroborates review concern (4) from
  a second direction.
- Sites inside antiquotation expansions are silently not reported.

## 3. Why this is shelved

Discussing the guard exposed a gap that is not specific to debugging: the MCP has an
evaluation target internally (`evaluation_state.file_path` + `destination_line`) but
never reports it, so an agent cannot tell what the server is working toward or why a
query was refused; and the caret-dependence of two query tools blocks them during
any evaluation, not merely during a debugger stop. That upgrade is being designed
first, and the debugger design must be re-checked against its outcome — in
particular §2.3 above and review concerns (7) and (8).

**Resolved 2026-08-13.** That upgrade shipped: the evaluation target is now
reported (the footer computed by `evaluation_footer` and the refusal messages
that name it), and no query tool depends on the caret any more. The re-check it
called for was carried out; its outcome is §2.4 for §2.3, §2.4 for concern (7),
and — for concern (8) — the observation that position→node resolution now has a
worked precedent on the Scala side (`Document.Snapshot.current_command` for a
single point, `PIDE/commands_at_lines` for a range), so it is no longer a design
question but a matter of following it. The work is no longer shelved.

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
