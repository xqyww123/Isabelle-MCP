# ML Debugger Support — Design

Status: **authoritative specification, rewritten 2026-08-13.** This revision
folds in everything that accumulated against the 2026-08-11 draft: the ten
surviving concerns of the adversarial review, the decisions recorded in
[`DEBUGGER_REVIEW_AND_DECISIONS.md`](DEBUGGER_REVIEW_AND_DECISIONS.md) (§2 and
§2bis there), and the facts established by the 2026-08-13 source studies of the
distribution. That file remains the record of *why*; this file is the record of
*what*. Where the two disagree, this file wins (it is newer). Section numbers
changed in the rewrite; references like "§6.4" in older documents point at the
2026-08-11 draft, not at this text.

Nothing in this document is implemented yet. Load-bearing claims marked
**(probe)** are source-derived and must be confirmed by the integration-test
probes of [`DEBUGGER_IMPLEMENTATION_PLAN.md`](DEBUGGER_IMPLEMENTATION_PLAN.md)
before code is built on them.

This document specifies interactive ML-debugger support for Isabelle-MCP:
setting breakpoints in Isabelle/ML code, being notified when evaluation stops
at one, inspecting the stopped thread (call stack, local variables, arbitrary
ML evaluation in a stack frame), and resuming (continue / step).

The design builds exclusively on machinery that already exists in
Isabelle2025-2 and in this repository. No patch to the Isabelle distribution is
required and no session heap is invalidated. The prover-side `Debugger.*`
protocol commands are already registered in every prover process
(`src/Pure/Tools/debugger.ML`); our own additions live in the component's ML
prelude (`mcp_prelude.ML`), which we own.

---

## 1. Glossary

These terms are used consistently throughout this document and MUST be used
consistently in the implementation (code, docstrings, user-facing messages).

- **breakable site** — a stopping location inserted by the Poly/ML compiler
  into instrumented ML code (one `bool ref` per site, initially `false`).
  Sites exist at statement boundaries chosen by the compiler; the front-end
  cannot create sites, only enable or disable existing ones. Each site is
  identified on the PIDE wire by a **serial** (an integer assigned at compile
  time, reported as `ML_breakpoint` markup). Sites exist only inside function
  bodies — `let`/nested-`local` declarations and bodies, expression sequences,
  conditional branches, `while` bodies, `fn`/`case`/`handle` alternatives and
  `fun` clauses — never on top-level `val`/`fun` declarations, and only in ML
  compiled while the debugger option was on.
- **breakpoint** — an entry in the client-side **breakpoint registry**: the
  user's *intent* to stop at a particular source location. A breakpoint is
  realized by enabling the breakable site there, and survives recompilation
  (which destroys and recreates sites) by re-resolution.
- **breakpoint registry** — the table of breakpoints kept by the Python layer.
  It is the single source of truth; the enabled/disabled state of prover-side
  sites is a projection of it and can be rebuilt from it at any time.
- **armed / pending** — the two states of a registry entry (§5).
- **anchor snippet** — source text starting at a site's statement, whole ML
  tokens, extended until unique within its line (§3.2). Recorded in the
  registry to identify the site without column numbers and to re-anchor it
  after edits; printed as `before ‹fold upd args›`.
- **hit** — one occasion of a thread halting in the debugger. Each hit gets a
  **hit identifier** (`hit_id`); the thread name appears in reports as
  information, never as an input. A step keeps the same hit (a controlled
  resume-and-restop inside it); the hit is retired when the thread resumes via
  continue, is swept up by cancellation, steps without stopping again, has
  its execution superseded by an edit (PIDE cancels the old execution and the
  thread is interrupted — the retired-id error names this ending too), or the
  prover goes away — **the hit table is cleared on every prover teardown path**
  (terminate, session switch, crash recovery, relaunch), each hit retired as
  "the prover was terminated". Using a retired id is an error naming how the
  hit ended, including that ending. (Thread names restart their counter with
  each prover process, so hits must never survive their prover: a fresh
  prover's `worker-3` is not the old one.)
- **frame** — one entry of a hit's call stack. Frame `0` is the innermost
  frame (where execution stopped).
- **debugger notice** — a one-line message about an asynchronous debugger
  event (a breakpoint demoted to pending, a hit outside a wait, …), buffered
  by the Python layer and appended to the next tool result (§6.3), under the
  header `Debugger notices:`.

## 2. Prerequisites and launch

### 2.1 The `debug` launch parameter

Debugger instrumentation is controlled by the Isabelle system option
`ML_debugger` (default `false`). When enabled, *newly compiled* ML code —
`ML ‹…›` blocks, `ML_file`-loaded files, etc. in the theories the agent
evaluates — is compiled with debug information and breakable sites. Code
already compiled into the session heap is unaffected. `ML_debugger` is not a
build-identity option: enabling it invalidates no heap.

Because instrumentation slows compiled ML and instrumenting critical
infrastructure may deadlock, debugging is **opt-in per session**:

- `isabelle_launch` gains `debug: bool = false`. When true, the server is
  spawned with an additional `-o ML_debugger=true`.
- > **Superseded 2026-08-31 (§6B/D-B19; see SPECIFICATION.md §4.3.1)**: any
  > identity change (session or `debug`) now restarts the prover, a live
  > breakpoint hit refuses the restart, and `isabelle_launch` returns one
  > sentence instead of `SessionInfo`. The error described below — and the
  > `debug` parameter description quoted later in this document — no longer
  > exist.
- **`debug` is part of the launch identity.** When the requested session name
  matches the running one but the `debug` value differs, `isabelle_launch`
  **errors** — in both directions — telling the agent to call
  `isabelle_terminate` first and then launch with the wanted `debug` value.
  No automatic teardown, so a routine launch can never silently kill a running
  debug session. The error applies only when the running prover would
  otherwise be reused: a request naming a *different* session already implies
  a relaunch, and `debug` simply applies to the new prover. (`session_dirs`
  stays out of the identity check.)
- `SessionInfo` gains a `debug: bool` field, so every launch/session-info
  result reports the live state.
- When `debug` is off, every breakpoint/debugger tool fails fast:
  `"Debugging is not enabled in this session. Call isabelle_terminate, then
  isabelle_launch with debug=true."`

`Debugger.init` (installing the prover-side break hook) is **implicit**: the
Scala server sends it lazily before the first debugger-related action and
re-sends it on session restart. No init/exit tool is exposed.

### 2.2 Breakpoints exist only on evaluated code

A location has a usable breakable site only if all of the following hold:

1. the enclosing ML command was compiled while `ML_debugger` was on;
2. the command is part of PIDE-visible source (a file under evaluation);
3. the enclosing command has **finished** evaluating — the prover refuses to
   toggle sites of a running or unevaluated command (`Command.eval_finished`).

`isabelle_set_breakpoint` on a location with no live site is an **error**
telling the agent what to do (§4.2); no registry entry is created. (The
former `pending`-on-creation behaviour is removed; `pending` still exists,
but only as the state an *armed* entry falls back to — §5.)

Two consequences the tool descriptions must state:

- Hitting a breakpoint requires running the code **twice**: once to compile
  the sites, once to hit them.
- The command that compiled a site can never be stopped at by it: arming
  requires that command to have finished, and by then it has already run.
  A breakpoint takes effect only for code entered from a command that runs
  after the compiling command finished.

(The review's fix (4) also asked for this rule in the pending/re-armed
notices; that clause is superseded by the short-tag decision — notices carry
only §4.4's tags, and the rule lives in the tool descriptions.)

**The three working motions** — "run twice" read literally (call
`isabelle_evaluate_to` again on an unchanged file) does nothing, since
evaluation is frontier-based; what works is taught explicitly (§4 preamble):

1. *Setting a breakpoint for the first time*: evaluate **up to the end of the
   defining ML block**, set the breakpoint (the site exists and the prover is
   idle, so it arms on the spot), then evaluate onward — the caller runs for
   the first time and hits.
2. *Re-triggering an armed breakpoint*: edit **strictly after** the defining
   block (a whitespace edit between the definer and the caller suffices) and
   re-evaluate. The definer's execution is reused, so its sites and the
   breakpoint survive; only the caller re-runs, and hits.
3. *After editing the definer or anything upstream of it*: the definer
   re-executes and its sites die (execution objects are chained — changing
   anything upstream changes the definer's input state, so even textually
   unchanged code re-runs). Entries are demoted to pending with a notice.
   Evaluate up to the end of the defining block, call
   `isabelle_enable_all_breakpoints` to arm the reborn sites, then evaluate
   onward.

The one losing move — teach it as such — is editing at or before the definer
and then evaluating to the end in one go: the sites are reborn disabled and
nothing arms them mid-run (§5).

## 3. Addressing without columns

No tool accepts or returns a column number, following the repository's
conventions (`isabelle_hover` uses `line` + `symbol`; `isabelle_evaluate_to`
uses `line` + `after_text`).

### 3.1 `at_text`

Breakpoint tools use `line` + optional `at_text`:

- `at_text` given: its first occurrence on `line` is located (ASCII and
  Unicode symbol forms equivalent), and the chosen site is the one **at or
  nearest before** the occurrence's first character — "stop before executing
  this code". The backward search is **bounded to the line**: if the line has
  sites but none at or before the anchor, that is an error listing the line's
  sites (§4.2, message 5), never a silent match on an earlier line.
- `at_text` omitted: the **first** site on `line`.
- **Ambiguity is refused only when it matters**: if `at_text` occurs several
  times on the line but every occurrence resolves to the same site, it is
  served; if the occurrences resolve to *different* sites, it is an error
  listing the line's sites. (Repetition that cannot change the answer —
  `val c = f x + f x` — is common and must not be rejected; repetition that
  can — `val a = g x; val b = g x` — must not be resolved silently. This
  deliberately diverges from the first-occurrence rule of `isabelle_hover`
  and `isabelle_evaluate_to`: a misplaced breakpoint costs a whole debugging
  round trip.)

### 3.2 The anchor snippet

Sites are presented as `line` + anchor snippet. The snippet is computed by the
Python layer from the site's position and the file content:

- It starts at the **statement's first character** and consists of **whole ML
  tokens**, extended token by token until the text is **unique within its
  line**. No length cap: a snippet equal to the rest of the line cannot occur
  twice in that line, so uniqueness is always reached without crossing the
  line. Uniqueness (not any weaker condition) also guarantees the snippet
  passes §3.1's ambiguity rule when passed back as `at_text`.
- In prose it is printed as `before ‹fold upd args›`. The cartouche is a
  **delimiter, not decoration** — snippets may contain commas, and the
  cartouche marks exactly the text to copy back as `at_text`. A truncation
  marker, if ever needed, goes **outside** the cartouche.
  (Implementation note: ML source may itself contain nested cartouches, and
  token-boundary truncation could in principle cut inside one.)

### 3.3 Facts about site positions (probe)

From source study of `ml_compiler.ML` and the bundled Poly/ML, pending probe
confirmation:

- The reported markup position is **shifted one symbol left** of the
  statement's first character (`ml_compiler.ML:41-50`). It normally lands on
  whitespace, and on the *newline ending the previous line* when the statement
  starts in column 1. **Anchor at the markup range's end, not its start** —
  otherwise column-1 statements yield an empty anchor. (Exception: a site at
  the chunk's first symbol is not shifted.)
- Derive the **line** from the corrected position too: PIDE strips position
  properties when incorporating reports, and the range start would report
  column-1 statements one line early.
- The markup carries **no extent** (Isabelle discards Poly/ML's end offset),
  so the snippet cannot be derived from a statement span; it is tokenised
  forward by us.
- Sites inside antiquotation expansions are silently not reported.

### 3.4 `.ML` files

Code in a `.ML` file is compiled by **evaluating the theory whose `ML_file`
command loads it** — a `.ML` file cannot be evaluated directly (it is a
dependency blob, never an open document). This must be said wherever a `.ML`
target is refused: refusal message 1's "evaluate the file first" becomes, for
`.ML` targets, "evaluate the theory that loads it (its `ML_file` command)",
naming the theory when the server knows it. Anchor snippets for `.ML` files
are computed from the file content **as last synced to the prover** (the
copy that was compiled), not a fresher disk copy. Per-position evaluation
status exists only for `.thy` documents, so `isabelle_list_breakable_sites`
on a `.ML` file omits `not_evaluated` ranges; when it finds no sites at all
it says instead that the loading theory has not been evaluated (or that no
loading command is known). Site markup for blob-loaded code resolves through
the loading command's blobs **(probe)**.

## 4. Tools

Twelve new tools, plus one changed parameter on `isabelle_launch`. Names
follow the `isabelle_` prefix convention; all `file_path` arguments are
absolute paths (realpath-normalized as elsewhere; `isabelle_del_breakpoints`
additionally accepts the project-root-relative form its listings print,
§4.3).

**All twelve are text results** (`output_schema=None` plus a formatter in
`utils/formatters.py`); `models.py` gains nothing. This is distinct from the
YAML change to the seven pre-existing structured tools (a separate change that
lands first): those serialise a model, these are formatted prose, and neither
style is imposed on the other. Every result additionally carries pending
debugger notices (§6.3).

The MCP server instructions (`instructions.py`) gain a debugger section that
teaches, once, what an agent cannot be assumed to know — the three concepts
hit, frame and breakable site; **the three working motions of §2.2** (and the
one losing move); the manual arming rule of §5 (nothing re-arms in the
background); and that `isabelle_eval_at_breakpoint` takes a single
expression, with temporaries written `let val x = … in … end` — so tool
descriptions and reports stay lean and do not define terms inline. Its text
is drafted at implementation time.

### 4.1 `isabelle_launch` (changed)

New parameter appended to the existing schema:

```json
{
  "debug": {
    "type": "boolean",
    "default": false,
    "description": "Enable the ML debugger for this session (compiles newly evaluated ML with instrumentation; slows ML compilation/execution). Required for all breakpoint tools. Changing it requires isabelle_terminate first; launching with the same session name and a different debug value is an error."
  }
}
```

### 4.2 `isabelle_set_breakpoint`

Register a breakpoint and enable its site. *Text result*: the resolved site
(`line`, anchor snippet), and — when resolution had to pick among several
sites — the other candidates on the line.

```json
{
  "type": "object",
  "properties": {
    "file_path": {
      "type": "string",
      "description": "Absolute path to the .thy or .ML file"
    },
    "line": {
      "type": "integer",
      "minimum": 1,
      "description": "Line number (1-indexed) of the breakable site"
    },
    "at_text": {
      "type": ["string", "null"],
      "default": null,
      "description": "Optional text snippet on the line. The breakable site at or nearest before its first occurrence is used (execution stops before that code runs). Without at_text, the first site on the line. ASCII and Unicode symbol forms are equivalent."
    }
  },
  "required": ["file_path", "line"]
}
```

Refusal messages, final wording (each lives in Python, unit-tested verbatim;
`{where}` is `file:line`):

1. Line not evaluated yet:
   > There is no breakable site at {where} — that line has not been evaluated
   > yet. Breakpoints can only be set on code the prover has already compiled,
   > so evaluate the file first.

   For a `.ML` target the last clause becomes the §3.4 variant ("evaluate the
   theory that loads it (its `ML_file` command)", naming the theory when
   known).
2. Command still running:
   > There is no breakable site at {where} yet — the command there has not
   > finished evaluating. A site can only be used once its command has
   > finished. Retry in a few seconds.

   When the command is unfinished because its thread is **stopped at a
   breakpoint**, retrying is useless; the variant is:
   > There is no breakable site at {where} yet — the command there is stopped
   > at a breakpoint and has not finished evaluating. Resume it with
   > isabelle_continue_breakpoint first.
3. Evaluated, but no site on that line:
   > The command at {where} has been evaluated, but the compiler placed no
   > breakable site on that line. Breakable sites exist only inside ML code,
   > at statement boundaries the compiler chooses. The nearest breakable
   > sites in this file are:
   >
   >       line 14 before ‹fold upd args›
   >       line 17 before ‹writeln (string_of_int n)›
   >
   > Pass one of these as line + at_text.

   The listing shows the nearest 8 sites, 4 each side of the requested line,
   in source order, with no backfill when one side has fewer. When the file
   has more sites than shown, the listing ends with the approved pointer:
   > (12 more breakable sites in this file — use isabelle_list_breakable_sites
   > to see them.)

   When the whole file has none, the listing and its lead-in are replaced by:
   > This file has no breakable sites at all — it contains no ML code that
   > was compiled in this prover.
4. `at_text` given but **not occurring on the line at all** (message 3 wins
   whenever the line has no sites, regardless of `at_text`):
   > ‹{at_text}› does not occur on {where}. The sites on that line are:
   >
   >       before ‹fold upd args›
   >       before ‹Symtab.update tab›
   >
   > Pass one of these as at_text, or omit at_text to use the first site on
   > the line.
5. `at_text` given but no site at or before it on the line (also used for the
   ambiguity refusal of §3.1):
   > There is no breakable site at or before ‹{at_text}› on {where}. The
   > sites on that line are:
   >
   >       before ‹fold upd args›
   >       before ‹Symtab.update tab›
   >
   > Pass one of these as at_text, or omit at_text to use the first site on
   > the line.

### 4.3 `isabelle_del_breakpoints`

Remove breakpoints from the registry and disable their sites (if armed).
Takes a **list** (precedent: `isabelle_command_status(positions)`); deleting
one is a one-element list. Each reference is matched against the **registry**
— file, recorded line, anchor snippet — never against live sites, so an entry
with no current site is still deletable. There is no "delete all" tool: list,
then pass the lot.

**Best-effort semantics**: entries that match are deleted; the result reports
what did not — `deleted 4; 1 matched no entry: Foo.thy:12 before ‹fold upd
args› — call isabelle_list_breakpoints for the current entries`. A reference
matching several entries is skipped and reported, never guessed.

Listings print paths **project-root-relative** (the repository's display
convention); `isabelle_del_breakpoints` accepts both that form and absolute
paths (relative ones are joined to the project root and realpath-normalized),
so a listing row can be passed back verbatim.

```json
{
  "type": "object",
  "properties": {
    "breakpoints": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "properties": {
          "file_path": {"type": "string"},
          "line": {"type": "integer", "minimum": 1},
          "at_text": {
            "type": ["string", "null"],
            "default": null,
            "description": "Anchor snippet of the entry, as shown by isabelle_list_breakpoints. May be omitted when the line has only one entry."
          }
        },
        "required": ["file_path", "line"]
      }
    }
  },
  "required": ["breakpoints"]
}
```

### 4.4 `isabelle_list_breakpoints`

List the breakpoint registry — nothing else (site discovery is §4.5).

```json
{
  "type": "object",
  "properties": {
    "file_path": {
      "type": ["string", "null"],
      "default": null,
      "description": "Absolute path; restricts the listing to one file. Omit for the whole registry."
    }
  },
  "required": []
}
```

*Text result*, one entry per line, location first:

```
breakpoints:
  - Foo.thy:14 before ‹fold upd args›, enabled, armed
  - Bar.thy:7 before ‹Symtab.update tab›, enabled, pending (not evaluated yet)
  - Baz.thy:22 before ‹writeln msg›, enabled, pending (still evaluating)
  - Qux.thy:9 before ‹the_default 0 x›, enabled, pending (code not found — it
    may have been edited away; delete this breakpoint or set it again)
```

The parenthesised reasons are **short tags**, not sentences: a prover relaunch
turns every entry pending at once, and a full sentence per row would repeat a
dozen times and stop being read. The two self-resolving causes get short tags;
the one needing a decision (code edited away) stays long because it is rare.
The same tags are used verbatim in debugger notices — one concept, one
wording — and the full explanations live once in the tool description.

### 4.5 `isabelle_list_breakable_sites`

List where breakpoints *can* go. This is the discovery tool, and discovery is
essential: setting a breakpoint where no site exists is an error (§2.2), and
top-level declarations carry no sites (§1), so the agent cannot work it out
from the source alone.

```json
{
  "type": "object",
  "properties": {
    "file_path": {
      "type": "string",
      "description": "Absolute path to the .thy or .ML file"
    },
    "start_line": {
      "type": ["integer", "null"], "minimum": 1, "default": null,
      "description": "First line (1-indexed) of the range. Default: whole file."
    },
    "end_line": {
      "type": ["integer", "null"], "minimum": 1, "default": null,
      "description": "Last line (1-indexed, inclusive). Default: whole file."
    }
  },
  "required": ["file_path"]
}
```

*Text result*:

```
sites:
  - line 14 before ‹fold upd args›, already enabled
  - line 15 before ‹writeln msg›, already set but disabled
  - line 17 before ‹Symtab.update tab›, breakable
not_evaluated: lines 20-46, lines 58-73 — evaluate up to those lines to see their sites
truncated: showing 40 of 137 sites, lines 14-52 — narrow the range (e.g. start_line 53) to see the rest
```

- The third field is three-valued — `already enabled` / `already set but
  disabled` / `breakable` — because "a breakpoint is attached" and "it will
  stop" are different questions (a disable-all leaves entries attached but
  off). No further identification of the attached breakpoint is needed: a
  site and the breakpoint on it share file, line and anchor snippet exactly.
- Sites are reported for the **evaluated part** of the range; line ranges
  with no sites merely because they are not evaluated yet are named
  separately (`not_evaluated`), reusing the per-position status machinery of
  `isabelle_command_status`. An empty `sites:` with a `not_evaluated:` line
  means "not known yet", never "nothing there".
- At most **40** sites are shown, in source order; a truncated listing says
  so and names the line to resume from. `not_evaluated` and `truncated` are
  omitted entirely when empty.

### 4.6 `isabelle_enable_all_breakpoints` / 4.7 `isabelle_disable_all_breakpoints`

`enable_all` sets the `enabled` flag of **every** registry entry to true AND
**arms every entry whose site currently exists** — under the manual arming
model (§5) this is *the* re-arming action after recompilation, a prover
relaunch, or a cancellation. An optional `file_path` parameter scopes both
the flag and the arming to one file (motion 3 rarely wants week-old
breakpoints in other files back). Entries that cannot arm stay pending and
are reported with their reason tags. `disable_all` sets the flag false and
switches off the sites of all armed entries (the entries stay in the armed
state — "armed" is about having a live site, the flag is about whether it
stops); pending entries keep the flag for when they are armed again. Use
case for the pair: silence all breakpoints for an undisturbed run, then
restore them.

**Absolute state, not a flip**: calling either twice is idempotent. (The
prover-side toggle helper is flip-shaped; the adapter realises the absolute
state as "read the current site state, toggle only if it differs", computing
the acknowledgement from its own snapshot — never from the distribution's
Scala-side mirror after a failed toggle.)

**An entry is recorded armed only on the toggle's positive acknowledgement.**
On failure it stays pending, tagged by cause: `still evaluating` for the
enclosing-command-not-finished error (the serial is alive — this failure does
*not* coincide with serial death; the coincidence claim holds only for the
unknown-serial error, where recompilation has already minted successors), a
demotion for unknown-serial, and pending with a wire-failure note when the
request itself died in flight — the one state the warning fence must never
vouch for.

*Text result*: the **armed entries listed** (one row each, §4.4's format —
counts alone would hide that motion 3 just re-armed a forgotten breakpoint
in an imported library), plus counts of entries pending per reason tag.

```json
{
  "type": "object",
  "properties": {
    "file_path": {
      "type": ["string", "null"],
      "default": null,
      "description": "Restrict to breakpoints in this file. Omit for all breakpoints."
    }
  },
  "required": []
}
```

During an active evaluation the call is allowed; it arms what has finished
compiling, and whether the current run stops at a just-armed site is a race
(§5) — arm between rounds for deterministic behaviour.

### 4.8 `isabelle_debug_state`

Report all live hits with their call stacks. Callable at any time. *Text
result*; no parameters.

No hits:

```
No thread is stopped.
```

Otherwise one block per hit (no implicit locals fetch here — that is only in
the hit report, §6.1, and only for frame 0):

```
2 hits are live.

Hit h1: Foo.thy:14 before ‹fold upd args›, thread Isabelle.worker-3.
Call stack (innermost first; the number is the frame parameter):
  frame 0  lookup          Foo.thy:14
  frame 1  resolve         Foo.thy:52
  frame 2  Symtab.fold     (library code)

Hit h2: Bar.thy:30 before ‹Symtab.update tab›, thread Isabelle.worker-7.
Call stack (innermost first; the number is the frame parameter):
  frame 0  update_all      Bar.thy:30
```

Frame positions that resolve to visible source are `file:line`; frames of
code compiled into the heap show `(library code)`. (Which frames resolve, and
the exact placeholder wording, are refined by probe.)

### 4.9 `isabelle_eval_at_breakpoint`

Evaluate an Isabelle/ML expression in the scope of a stack frame of a hit:
the frame's ML name space (including enclosing scopes) is merged into the
compilation environment, so the frame's local bindings are directly usable.
Antiquotations work. The evaluation is never itself instrumented. `expr` is a
single **expression** — a bare declaration fails with a parse error at the
wrapper's own tokens (the expression is compiled inside `val it = ( … );`,
§7.3); a temporary binding is written `let val x = … in … end`, and bindings
do not survive to the next call (each evaluation rebuilds its context and
restores it afterwards). A paren-balanced "smuggle" such as
`1); val x = (2` compiles as two declarations — but they run INSIDE the
wrapper's protection (deadline, abort, classification all apply) and their
bindings die with the discarded context, so the trick buys nothing. An
empty or whitespace-only `expr` is refused by the tool layer before the wire
(it would compile to the well-formed `val it = ( );`). *Text result*: the
evaluation's `writeln`/`warning`/`error` output; an expression that raises
is a normal, completed round trip whose output is the error message (not a
tool error).

```json
{
  "type": "object",
  "properties": {
    "expr": {
      "type": "string",
      "description": "Isabelle/ML expression to evaluate"
    },
    "hit_id": {
      "type": ["string", "null"],
      "default": null,
      "description": "Identifier of one hit — one occasion of a thread halting in the debugger — as reported by the hit report (e.g. hit_id: \"h1\"). Not a thread name, and not stable across resume: when the thread continues and halts again, that is a new hit with a new id. May be omitted when exactly one hit is live; with several live hits it is required (the error lists them)."
    },
    "frame": {
      "type": "integer",
      "minimum": 0,
      "default": 0,
      "description": "Stack frame index, as numbered in the call stack of the hit report (0 = innermost)."
    },
    "timeout": {
      "type": "number",
      "default": 180,
      "description": "Seconds before the evaluation is cut off inside the prover. On timeout the expression ends with an error and the thread stays at the breakpoint, still debuggable. A tight allocation-free loop may offer no safe point and then cannot be cut off; isabelle_cancel_evaluation remains the global way out."
    }
  },
  "required": ["expr"]
}
```

Using a retired `hit_id` is an error naming how that hit ended. Issuing a
second evaluation — or a continue or step — on a hit whose previous
evaluation has not returned is refused immediately with a sentence saying the
previous evaluation has not returned and naming
`isabelle_abort_eval_at_breakpoint` as the way to end it (no queueing — a
queued verb would run at an arbitrary later moment).

Breakable sites crossed **during** an evaluation at a hit never fire: the
prover's break hook declines while the thread is debugging. To stop inside a
callee, resume and trigger it from normal execution. (Stated here and in
§7.4; the tool description carries one sentence of it.)

Deliberately not exposed in v1 (prover supports them; defaults are used):
strict-SML mode, and the explicit evaluation-context text.

### 4.10 `isabelle_locals_at_breakpoint`

Print **all local variables** of a stack frame with their types and values
(the frame's own bindings, not enclosing scopes). The cheap first look before
`isabelle_eval_at_breakpoint`. *Text result*.

**Mechanism** (differs from the stock `print_vals` verb, deliberately): the
listing is produced by a small prelude function using
`PolyML.DebuggerInterface` (`debugState` on the stopped thread — the live
stack equals the loop-entry capture, since the eval is uninstrumented —
`debugLocalNameSpace`, and `printWithType` at `ML_print_depth`), invoked
**through the eval verb under `debug_eval`** (§7.3), emitting via
`Debugger.writeln_message` so the output reaches the `debugger_output`
channel. This is a stated exception to "no debugger logic is reimplemented",
and it is what makes the `timeout` parameter real: under the `print_vals`
verb the printing runs in the debugger loop's own code, where nothing of
ours can enforce anything.

**Reaching `PolyML.DebuggerInterface` needs one extra step (probed
2026-08-14, works with one correction).** The raw global namespace every
heap inherits carries only a four-entry `PolyML` stub — `ML_Bootstrap.thy`
shadows the original during Pure's bootstrap. The full binding survives in
exactly one place: theory `ML_Bootstrap`'s own Isabelle/ML environment
(`structure PolyML = PolyML` in its first ML block). The prelude therefore,
once at load time, compiles `structure Isabelle_MCP_PolyML = PolyML` under
`Context.Theory (Thy_Info.get_theory "ML_Bootstrap")`, landing the binding
back in the raw global namespace for the rest of the prelude. **The
correction (measured against the original claim "`ML_write_global` is still
true there"): `ML_write_global` is FALSE in that theory's final context**, so
the write stayed in context tables and the raw namespace saw nothing; the
prelude forces the config back to true for this one compilation
(`Config.put_generic ML_Env.ML_write_global true`), after which the write
lands globally — verified against the HOL heap, and re-verified at every
debug-enabled server start (the prelude compiles the locals printer against
the re-exposed structure, and a `--use` failure is fatal and surfaced).
~7 lines, version-coupled. **Fallback if the re-exposure fails on some
heap**: locals revert to the stock `print_vals` verb and the prover-side
locals timeout is given up — the Scala backstop and the debt fence then
govern, and §7.3's policy line changes accordingly.

Output matches stock `print_vals` byte for byte with no filtering: the
composed text binds the call as `val _ = …;`, which echoes nothing (§7.3's
empty-writeln guard), so the listing is the only output — and a genuine
`val it = (): unit` produced by an agent's own evaluation is legitimate
output that survives untouched.

The cost of printing is real and unbounded by depth: Isabelle's installed
printers for its core types build the **complete** pretty tree (full syntax
elaboration of the value) and prune to `ML_print_depth` afterwards — depth
bounds the output, not the computation. Hence, inside the listing, **each
variable additionally gets its own 5 s bound — implemented as per-value
elapsed/abort checks against the ONE envelope, never as nested
`Timeout.apply`**: a nested inner timer that fires can drain the outer
deadline's (or the abort's, or a genuine cancellation's) pending interrupt
and swallow it, leaving the outer guard fictitious for the rest of the
listing. A value that cannot be
printed in time renders as `<printing timed out>` (no type — type layout can
itself be slow) while the rest still print. The 5 s figure is internal
policy, not a parameter.

Input schema: as §4.9 without `expr` (same `hit_id` and `frame`); `timeout`
keeps the 180 s default but its description reads:
> Seconds before the whole listing is cut off inside the prover. Each
> variable additionally gets a short per-value bound; a value that cannot be
> printed in time is shown as `<printing timed out>` while the rest still
> print. The thread stays at the breakpoint either way.

### 4.11 `isabelle_continue_breakpoint`

Resume execution. With `hit_id`, resumes that hit's thread and retires the
hit; without it, resumes **all** live hits (the common single-hit case just
works — this is the one tool where omission with several hits means "all",
not an error). Enabled breakpoints stay enabled — execution stops again at
the next hit. *Text result*: which hits were resumed.

```json
{
  "type": "object",
  "properties": {
    "hit_id": {
      "type": ["string", "null"],
      "default": null,
      "description": "Hit to resume, as reported by the hit report. Omit to resume all live hits."
    }
  },
  "required": []
}
```

### 4.12 `isabelle_step_at_breakpoint`

Single-step a hit's thread. The three modes map onto the prover's stepping
verbs:

- `step` — run to the next breakable site, entering calls;
- `step_over` — run to the next site at the same or shallower stack depth;
- `step_out` — run to the next site at a shallower stack depth (which may not
  exist).

**Two outcomes, both normal.** Stepping only stops inside instrumented code,
so the tool waits a bounded time and reports either:

- *stopped again*: the same hit at its new position, formatted like a hit
  report (§6.1); or
- *did not stop*: "The thread resumed and did not stop again within {N}s —
  execution left the instrumented region (stepping only stops in ML compiled
  with debugging in this session)." The hit is then retired. **The wait bound
  never aborts anything** — a thread that does not stop again is a normal
  outcome, not a failure.

(Caveat recorded for the implementation: the prover's stepping flag is
thread-local, set with a bare assignment, and cleared only by a `continue` at
a later break; a worker can in principle carry it into an unrelated task and
stop there. Such a stray halt is reported as an anomaly with its own wording,
and sending `continue` to it clears the flag. Probed.)

```json
{
  "type": "object",
  "properties": {
    "mode": {
      "type": "string",
      "enum": ["step", "step_over", "step_out"],
      "description": "Stepping mode"
    },
    "hit_id": {
      "type": ["string", "null"],
      "default": null,
      "description": "Hit to step, as reported by the hit report. May be omitted when exactly one hit is live."
    }
  },
  "required": ["mode"]
}
```

### 4.13 `isabelle_abort_eval_at_breakpoint`

End the outstanding evaluation on a hit **now**, without waiting for its
timeout: sets the wrapper's abort flag (§7.3); the evaluation ends with an
ordinary error at its next safe point and **the thread stays at the
breakpoint, still debuggable**.

**Mechanism — outcome-based bounded retry (user decision 2026-08-14; the
Scala side is stateless).** `PIDE/debugger_abort` answers `no_evaluation`
when the thread has neither a pending evaluation nor an owed state (settled
— an indebted thread counts as abortable: it is exactly the runaway abort
exists for), else sends the prelude flag command ONCE and answers `aborting`
immediately. An abort acknowledged as `aborting` can still be LOST (the
pre-registration window, §7.3), so the TOOL confirms by outcome and retries:

- send `PIDE/debugger_abort`; wait min(~2 s, the targeted evaluation's own
  outstanding reply);
- if that reply arrived, the evaluation ended — report how it ended and stop
  re-sending. Pinning the retry to the target's own reply structurally
  closes the wrong-target window: the tool never re-sends after the target
  settles, and the caller is blocked inside the tool, so no next evaluation
  can start meanwhile.
- in the debt case (the backstop already answered that evaluation `timeout`)
  keep re-sending until the abort reply flips to `no_evaluation` — safe,
  because the busy fence refuses new evaluations while the debt is owed;
- bound ~30 s total, then report honestly that the abort was requested
  repeatedly but the evaluation has not ended (the allocation-free-loop
  limit; `isabelle_cancel_evaluation` remains the global way out). Never
  claim delivery that was not observed.

The ML-side silent no-op for an unregistered thread is load-bearing
staleness protection: re-sending is harmless because the flag lives in the
registration entry (below). Rejected alternatives, for the record: a
prover-side pre-abort table (a stale-abort landmine needing token
threading); a Scala-side retry state machine (its re-sends cross settlement
boundaries and kill the NEXT evaluation — the ML table is keyed by thread
name only); a raw interrupt in the window (escapes the error wrapper, kills
the command, poisons the theory tail).

*Text result* on `aborting` + confirmed settlement:
> Abort requested. The evaluation on this hit will end with an error at its
> next safe point; the thread stays at the breakpoint.

On `no_evaluation` (nothing outstanding), an **error** (not a success text):
> No evaluation is in progress on this hit — there is nothing to abort.

A retired `hit_id` gets the standard retired-hit error.

The abort flag's lifetime is bound to the per-evaluation registration entry
(§7.3): it is created with `debug_eval`'s registration and dies with it, and
setting it is mutually excluded against deregistration — so an abort racing a
natural completion is delivered to the still-live evaluation, or refused with
the error above, or — the third outcome — accepted against an evaluation
whose body has already produced its value, in which case it has no effect
and the evaluation reports success. It can never leak into the next
evaluation.

Honest limits (stated in the description): the same safe-point caveat as the
timeout — a tight allocation-free loop cannot be cut off; and since the
agent's own eval call blocks, this tool's uses are parallel tool-call clients
and clearing an evaluation whose prover-side deadline mechanism failed while
the Scala backstop already answered.

```json
{
  "type": "object",
  "properties": {
    "hit_id": {
      "type": ["string", "null"],
      "default": null,
      "description": "Hit whose outstanding evaluation to abort, as reported by the hit report. May be omitted when exactly one hit is live."
    }
  },
  "required": []
}
```

## 5. The breakpoint registry and its lifecycle

Each registry entry stores: `file_path`, recorded `line`, anchor snippet,
`enabled` flag, current `state`, and (when armed) the site's current serial
plus the identity needed to toggle it on the wire.

**Two states.**

- **armed** — resolved to a live site; the site's `bool ref` mirrors the
  entry's `enabled` flag.
- **pending** — no live site right now; the entry arms again on the next
  explicit enable (§4.6) once a site exists. The *reason* — the file has not
  been evaluated in this prover, the enclosing command is still running, the
  code was edited away — is carried as a short tag in listings and notices
  (§4.4), not as a state name: at the moment of demotion, "not evaluated
  yet" and "code not found" can be indistinguishable, and the agent acts on
  the tag anyway.

Entries are only ever created armed (§2.2). `pending` is entered when an
armed entry's site disappears: the enclosing command was edited and
recompiled (directly, or because anything upstream changed — execution
objects are chained), the prover was relaunched, the session was switched,
or `isabelle_cancel_evaluation` ran (its synthetic edit re-creates command
ids and thus invalidates every serial **(probe)**).

**The manual arming model.** The pending→armed transition happens **only
through an explicit tool action**: `isabelle_set_breakpoint` for a new entry,
`isabelle_enable_all_breakpoints` for existing ones. Nothing re-arms in the
background — deliberately: automatic re-arming would race the very run it is
re-arming for (the prover starts the next command the instant the compiling
one finishes, while an arming round trip takes hundreds of milliseconds), it
would make background runs' behaviour depend on who won, and it puts wire
operations on paths that race the explicit tools. Under the manual model,
arming in the taught workflow happens between evaluation rounds, with no
opponent (mid-run arming is allowed but racy — see below); the agent's
workflow is §2.2's three motions.

**Background bookkeeping** (all that remains of reconciliation): observe site
death — on evaluation events, file resyncs, **dependency-blob edits** (`.ML`
files reach the prover through Isabelle's own file watcher, not ours; their
stat signatures are already tracked), prover relaunch, and after
cancellation — demote armed entries to pending, and emit one debugger notice
per state change (§4.4's tags; a reason-tag change *within* pending also
emits one, same once-only rule). The bookkeeping never toggles a site. Site
resolution — by anchor snippet, nearest to the recorded line, updating the
recorded line — runs at **arming time**, inside the explicit tools. Entries
that resolve to the same site are merged into one (reported by a notice);
an equal-distance tie in "nearest" resolves to the earlier line.

**Concurrency.** One registry lock serialises every read-check-mutate
sequence (a deletion's match-disarm-remove, an enable's resolve-arm, the
bookkeeping's demotions). All site toggles happen inside the explicit tools
under that lock, which is what makes §1's projection invariant ("site state
can be rebuilt from the registry at any time") actually hold.

**The forgotten-re-enable fence.** The cost of the manual model is that a
forgotten re-enable makes a run miss silently. Therefore `evaluate_to` warns
when the run cannot stop where the agent thinks it can. The trigger counts,
over the target's **import closure** (a run re-executes invalidated upstream
theories too; `.ML` blobs count via their loading theory):

- enabled entries that are pending — except those tagged `code not found`,
  which have had their arming attempt and failed (a warning that fires on
  every healthy run stops being read; those entries are §4.4's business);
- armed entries whose recorded position is no longer processed (`.thy`: the
  decoration tracker already knows; `.ML`: the blob's stat signature changed
  since arming) — sites that this very run is about to rebury.

The warning is emitted both as a line in `evaluate_to`'s result and as a
debugger notice (§6.3), so the query tools' auto-start path — which discards
a promptly-completed evaluation's view — still delivers it:
> {N} breakpoints in the files this run executes are not armed — it will not
> stop at them. Call isabelle_enable_all_breakpoints to arm them.

One structural blind spot, stated honestly: the fence cannot fire on the run
whose own edit kills the sites — at call time the registry still says armed
and nothing has recompiled yet. Motion 3's discipline (§2.2) is the only
protection there; the second trigger bullet narrows the window (an already
re-synced edit marks the position unprocessed) but does not close it.

**Explicit arming during an active evaluation** is allowed — `set_breakpoint`
and `enable_all` arm whatever has finished compiling, taking effect for code
that has not yet run — but whether the *current* run stops at a just-armed
site is a race between the arming round trip and the run's progress. For
deterministic behaviour, arm between rounds; that is the taught workflow, and
it is the workflow, not the system, that has no opponent. (Refusing mid-run
calls instead would break arming while a hit is live, which motion-adjacent
workflows need.)

The `enabled` flag's readers, so no simplification pass deletes it: the
warning trigger above (after `disable_all`, pending entries are
enabled=false and correctly do not warn — deliberate silence stays quiet)
and the listings (§4.4's rows, §4.5's `already set but disabled`).

**Registry lifetime.** The registry is cleared when the MCP server process
restarts (it lives in memory only). It is **retained** across a prover
relaunch and across a session switch: entries become pending (each demotion
reported by a debugger notice) and arm again on the next explicit enable
(reported by that tool's own result).

## 6. Hits and how the agent learns about them

### 6.1 Hit = a completion signal of evaluation

The typical situation: the agent called `isabelle_evaluate_to` and is waiting
(or polling `isabelle_evaluation_status`). A hit means the evaluation cannot
progress past that command, so the wait loop gains a third exit condition
besides "target processed" and timeout-with-progress: **a hit in the current
evaluation's theory set**. Position→node resolution is a Scala-side
responsibility (the worked precedent: `Document.Snapshot.current_command` /
`PIDE/commands_at_lines`); a hit elsewhere becomes a debugger notice instead
of ending the wait, and an unattributable hit fails open (ends the wait, as
any exit does today).

The result then leads with the hit report:

```
Breakpoint hit: Foo.thy:14 before ‹fold upd args›, hit h1, thread Isabelle.worker-3.
Call stack (innermost first; the number is the frame parameter):
  frame 0  lookup          Foo.thy:14
  frame 1  resolve         Foo.thy:52
  frame 2  check_theory    Foo.thy:88
Locals of frame 0:
  key = "HOL.eq" : string
  tab = {...} : term Symtab.table
Evaluation is paused, NOT finished. Inspect with isabelle_eval_at_breakpoint /
isabelle_locals_at_breakpoint / isabelle_step_at_breakpoint, or resume with
isabelle_continue_breakpoint.
```

- The report lists **all** live hits (further blocks as in §4.8) — parallel
  evaluation makes several stopped threads a first-class case, not an edge.
- Call-stack rows are labelled with the parameter name (`frame 0`, …), and
  the header says so, so report and schema share the exact string.
- The locals of frame 0 are fetched implicitly **for every hit in the
  report**, concurrently (the rendezvous is per-thread, so the fetches run in
  parallel and the time does not stack), all under one **10 s bound that is
  never destructive**: the 10 s IS the fetch's prover-side `debug_eval`
  deadline — at expiry the evaluation ends with an ordinary exception, the
  thread stays at the breakpoint, only the listing is lost. The fetch **is
  registered in the Python outstanding-request state and is abortable**; an
  agent call colliding with it is refused with its own sentence ("an
  implicit locals fetch is still running — retry in a few seconds or call
  isabelle_abort_eval_at_breakpoint"), never with the previous-evaluation
  sentence about an evaluation the agent never issued. Inside each fetch the
  per-value 5 s bound of §4.10 applies, so one pathological value costs one
  `<printing timed out>` line, not the section. On a whole-fetch timeout the
  section reads `Locals of frame 0 could not be fetched in time — use
  isabelle_locals_at_breakpoint to fetch them.`
- The tail does not define terms; the instructions section (§4 preamble)
  teaches the concepts.

`isabelle_evaluation_status` reports a "paused at a breakpoint" section
whenever hits are live (wording refined by the decoration probe), so polling
never misreads a stop as a hang. **`isabelle_evaluate_to` is refused while
any hit is live**: the frontier cannot advance, and `evaluate_to` still moves
the caret. The refusal leads with the live hits and names the next actions:
> **Superseded 2026-08-31 (D-C4/D-C6)**: the paused lead is now
> "Breakpoint hit: h1. The affected evaluation is paused." and the refusal
> "Breakpoint hit: hit id h1 at Foo.thy:14 before ‹fold upd args›.
> `isabelle_evaluate_to` cannot run while a thread is stopped at a
> breakpoint." — no tail sentence. The quote below is the retired wording.
> Evaluation is paused at a breakpoint — hit h1 at Foo.thy:14 before ‹fold
> upd args›. isabelle_evaluate_to cannot run while a hit is live. Inspect
> with isabelle_debug_state, resume with isabelle_continue_breakpoint.

This refusal **consumes** the queued notice of the hit it names (delivering
the same hit twice in two formats would only confuse); the refusal lives in
`evaluate_to` itself, so the query tools' auto-start path inherits it and the
caret never moves during a hit. The seven query tools are served normally per their
position-explicit guard — a line whose command finished before the hit
answers normally; the stopped command itself answers `unfinished`; an
unevaluated line is refused by the guard naming the evaluation target.
**(probe:** whether the prover-side query forks are still scheduled with
several workers parked at breakpoints; if they starve, the Scala side fails
fast with an explicit sentence rather than hanging.**)**

### 6.2 Hits outside an evaluation wait

A background re-evaluation (triggered by a file save) can hit an armed,
enabled breakpoint while nothing is waiting. The hit is recorded and surfaced: as a
debugger notice on the next tool call of any kind, and in
`isabelle_debug_state` / `isabelle_evaluation_status` at any time.

(A hit is position-identified, not registry-identified: an edit can demote
the entry while its old execution's thread is still stopped, so
`isabelle_list_breakpoints` may transiently show `pending` where
`isabelle_debug_state` shows a live hit. That is coherent, not a bug to fix.)

### 6.3 Debugger notices

MCP has no server-initiated push channel, so all asynchronous events
(demotions to pending, hits outside a wait, stray halts, …) are buffered as
debugger notices and appended to the next tool result, whatever the tool,
under a `Debugger notices:` header. Delivery reuses the existing
warning-injection middleware (`UnicodeWarningMiddleware`'s pattern: append as
an extra text block after a successful call; keep the queue on error). The
buffer is cleared on delivery; each notice is delivered exactly once. Notices
use §4.4's reason tags verbatim.

### 6.4 Interaction with cancellation

`isabelle_cancel_evaluation` runs the existing cancellation path
**unchanged** — no resuming of stopped threads first, no temporary disabling
of sites. (The 2026-08-11 draft's contrary design rested on the false premise
that a stopped thread is uninterruptible; the review deleted it.) A thread
leaves the hit table when it disappears from the prover's pushed debugger
state; its hits are retired as "swept up by cancellation". After the cancel,
the demote-and-notify bookkeeping runs (§5). If a release-stopped-threads
fallback is ever
wanted, Isabelle's own mechanism is `Debugger.exit` (which frees every thread
idle at a breakpoint), not per-thread resume.

## 7. Wire protocol and prover-side machinery

All messages follow the existing `PIDE/*` LSP-extension style. The server
side remains a thin adapter over Isabelle's existing debugger API
(`session.debugger`) — no debugger logic is reimplemented. (The alternative —
redefining the `Debugger.*` protocol commands in our prelude to carry richer
stop messages — is recorded and rejected for v1: it would mean maintaining a
copied debugger loop across Isabelle upgrades.)

### 7.1 Requests and notifications

Client → server requests (timeouts are JSON numbers of seconds, chosen by the
Python side, as in the query protocol):

- `PIDE/debugger_breakpoints {uri, range?, token, timeout}` →
  `{status, open, breakpoints: [{range, serial, state}]}` — every breakable
  site (`ML_breakpoint` markup) in the snapshot range, with **prover-truth
  enabled-states**: the listing resolves every serial against the real
  breakpoint ref in ONE batched round trip (prelude command
  `Isabelle_MCP.breakpoint_states`), so `state` is JSON `true`/`false`, or
  the word saying why that site's command could not be resolved (`undefined`
  / `unfinished` / `interrupted` / `failed` / `unknown_breakpoint`).
  Top-level `status` is `ok`/`timeout`/`crashed`/`outdated` — the request is
  async like the queries, so a wedged prover answers `timeout` instead of
  blocking the main loop, and a snapshot with pending edits answers
  `outdated` (the toggle's word for the identical condition; retry once the
  edits are incorporated). `outdated` covers ONLY the pending-edit window: an
  up-to-date snapshot whose commands have not yet been ML-compiled still
  answers `ok` with no sites — breakpoint markup exists only after
  compilation — so `ok` with an empty list does not by itself mean the file
  has no breakable sites. The server returns ranges and serials as found;
  anchor snippets are computed client-side (§3.2, including the one-symbol
  shift correction of §3.3).
- `PIDE/debugger_toggle_breakpoint {uri, serial, state, token, timeout}` →
  `{status, was?}` — the **acknowledged toggle**: the write happens
  prover-side on the real breakpoint ref (prelude command
  `Isabelle_MCP.toggle_breakpoint`, inline on the protocol thread; `state` is
  absolute, so a retry is idempotent), and an `ok` reply carries `was`, the
  previous value. Statuses raised before the prover is asked:
  `file_not_open` / `outdated` / `unknown_breakpoint`; from the prover: the
  query vocabulary (`undefined` / `unfinished` / `interrupted` / `failed` /
  `crashed`) plus `unknown_breakpoint` — one word for both a serial the
  snapshot markup does not know (Scala side) and one the resolved command's
  context does not know (ML side). Only an `ok` may record an arming
  client-side; a `timeout` leaves the write possibly applied, which is
  harmless under absolute semantics and visible in the next listing's prover
  truth.
- `PIDE/debugger_eval {token, thread, frame, expr, timeout}` →
  `{status, messages: [{kind, text}]}`; same shape without `expr` for
  `PIDE/debugger_print_vals` — which is **realised through the eval verb**,
  sending a call to the prelude's locals printer under `debug_eval` (§4.10),
  never the stock `print_vals` verb. Statuses: `ok` / `resumed` / `timeout` /
  `crashed` / `busy` / `not_stopped`. `not_stopped` is a refusal issued
  before anything is sent — the thread is not stopped — and promises the
  expression never ran; it is deliberately not `resumed`, which means "input
  delivered, the expression may have run". The sentences live in Python.
- `PIDE/debugger_abort {token|thread}` → `{status}`: `no_evaluation`
  (nothing outstanding and no state owed — the thread is settled) or
  `aborting` (the flag command was sent once; how the evaluation actually
  ended arrives through its own reply). Stateless on the Scala side; the
  confirmation loop is the abort tool's (§4.13).
- `PIDE/debugger_input {thread, verbs...}` → `{ok}` — resume/step verbs.

Server → client notifications:

- `PIDE/debugger_state {threads: [{thread, stack: [{position, function}]}]}` —
  forwarded thread stacks. A thread's **absence** from the array means it
  resumed (the Scala side removes the entry; an "empty stack" never appears).
- `PIDE/debugger_output {thread, messages}` — spontaneous debugger output not
  consumed by a pending eval, surfaced as debugger notices.

### 7.2 How the adapter consumes debugger messages

Two hard constraints from the distribution:

- `Session` installs `Debugger.Handler` unconditionally, and a second
  protocol handler claiming `debugger_state`/`debugger_output` throws at
  init. **Our consumer therefore subscribes to `session.all_messages`**,
  matching cheaply on the two markup kinds and ignoring everything else
  (**probe**: cost under load).
- `session.debugger_updates` is unusable as a signal: its event carries no
  payload and is coalesced behind a 100 ms delay.

**Completion signal.** The ML debugger loop emits **exactly one
`debugger_state` per input**, always after that input's output (same thread,
same channel, in order); the resume verbs emit the final one with an empty
stack. So: clear output, send the verb, treat the next `debugger_state` for
that thread as end-of-output; an empty-stack one means the thread is no
longer stopped (answered as `resumed`, with whatever output was collected).
A round trip with no output before its `debugger_state` is a successful empty
result, not a timeout. Output is accumulated in our own consumer, gated on
the pending entry — never read from `Debugger.State`'s per-thread buffer,
where late output from an abandoned request would be indistinguishable.

**Once-only discipline and the debt fence.** Requests live in a synchronized
per-thread table; taking an entry out is the permission to answer it (the
query protocol's invariant, reused verbatim; `Event_Timer` arms the Scala
backstop the same way). If a backstop ever fires with the prover-side
mechanism failed, the thread owes one `debugger_state`: a per-thread debt
counter eats exactly that many completion signals, output for a thread with
no pending entry is discarded, and new evaluations on an indebted thread are
refused immediately with a sentence saying the previous evaluation has not
returned and naming `isabelle_abort_eval_at_breakpoint` (no new status word —
it is a sentence, not a token). The debt clears itself when the late
`debugger_state` arrives. With the prover-side timeout this state should be
nearly unreachable; it is the backstop's backstop.

**Registration ordering.** The hit's own entry `debugger_state` must not be
mistaken for a completion: the check-register-send step is ordered behind
already-posted message callbacks (dispatcher-side execution; **probe** that
the guard is load-bearing).

The machinery is written alongside the query machinery, not merged into it
(`Query_Handler` is a protocol handler keyed token→one reply; this consumer
cannot be a protocol handler and is keyed thread→stream-until-sentinel).
`query.scala` is not touched.

### 7.3 The eval wrapper `Isabelle_MCP.debug_eval`

We construct the ML text sent to `Debugger.eval`; the agent's expression
travels as ONE ML string literal (encoded by the Scala side: `Symbol.encode`
first, then printable ASCII verbatim and every other UTF-8 byte as `\ddd`)
and is compiled by a prelude function INSIDE the wrapper's protection — the
composed text is constant-shape, so expression content can neither escape
the wrapper nor extend the unprotected pre-registration window:

```sml
val _ = Isabelle_MCP.debug_eval_string (Time.fromSeconds 180) "⟨literal⟩";
```

`debug_eval_string` compiles `val it = ( ⟨expression⟩ );` with
`ML_Context.eval` under `verbose = true`, writing through
`Debugger.writeln_message`/`warning_message` — so the result binding is
echoed exactly as the debugger's own evaluate would. The outer `val _`
envelope binds nothing; its silence on the wire rests on one guard: the
debugger's evaluate flushes ONE `writeln` carrying the EMPTY string for the
wildcard binding, and only `Debugger.writeln_message`'s empty-message drop
keeps it off the wire (a probe pins this). The locals call needs no literal
and stays a direct call:

```sml
val _ = Isabelle_MCP.debug_eval (Time.fromSeconds 180) (fn () => Isabelle_MCP.debug_locals 0);
```

`debug_eval` does three things in one place:

1. **Registers the running thread** in a prelude-side table keyed by the
   debugger's own thread name — the same string the protocol messages carry —
   and removes it on every exit path. This runs *before* the expression:
   a runaway thread no longer reads its input queue, so anything sent later
   would queue forever (hard ordering, not an optimisation). The abort flag
   lives **inside this registration entry**: created with it, dead with it,
   so a stale abort can never touch a later evaluation.
2. **Enforces the deadline.** On expiry the expression ends with an ordinary
   exception (the `Timeout.apply` pattern), which the debugger's error
   wrapper catches and prints — the loop returns to waiting for input and
   **the thread stays at the breakpoint**.
3. **Watches the abort flag**, set by `PIDE/debugger_abort` via a prelude
   protocol command, converted to an ordinary exception the same way — the
   on-demand version of the deadline (§4.13).

**How the containment actually works — binding requirements, not
suggestions.** Both the deadline and the abort are physically delivered as
**thread interrupts** (that is the only way to break into a running
expression); what keeps them from killing the command is *classification
inside the wrapper*, and stock `Timeout.apply` classifies only its own
timer's interrupt — an abort interrupt passed through it would be re-raised
as a genuine interrupt, escape the debugger's error wrapper (which re-raises
interrupts), and kill the debugged command with the poisoned tail of point
(2) below. Therefore:

- Every interrupt leaving the expression must pass **one outermost
  classifier** in `debug_eval`: deadline expired or abort flag set → an
  ordinary exception; anything else → re-raised (a genuine cancellation must
  still kill the command). **Ties break toward the ordinary exception**: a
  genuine cancellation arriving while the deadline/abort predicate is true is
  physically indistinguishable (all three senders deliver the same interrupt,
  and pending interrupts coalesce) and gets swallowed — safely, because
  cancelled Future groups are **retried by the scheduler every cycle**, so
  the swallowed cancellation is re-delivered at the loop's next input wait
  and the thread dies normally then. This rescue is load-bearing; do not
  re-report the swallow as a cancellation-semantics bug.
- Deregistration is **mutually excluded** against the abort sender's
  interrupt, and every exit path **drains pending interrupts under
  `no_interrupts`** before returning — a late interrupt left undrained would
  be delivered at the loop's next input wait, outside any wrapper, and kill
  the command after a successful evaluation. **The classifier applies to the
  drained result too**: drained interrupt with deadline/abort true → drop;
  with neither → **re-raise** (stock `Timeout.apply`'s discipline — a
  genuine cancellation arriving just after the body completes must not be
  eaten into a success).
- The deadline and the per-value bounds are **raw `Event_Timer`/elapsed-time
  checks, unscaled** — `Timeout.apply`-based bounds would silently multiply
  by `timeout_scale` and drift from the verbatim figures and the 210 s Scala
  backstop.

The wrapper is also the home of the **locals printer** (§4.10): the same
envelope, the expression being a call to the prelude function that prints a
frame's variables with per-value 5 s bounds.

The string-literal embedding closes the old escape hole structurally (no
client-side token-balance check exists any more; see §4.9 for what a
paren-balanced "smuggle" can still do INSIDE the protection). The honest
residual window: registration happens inside `debug_eval`, so before it
there remain the input-queue round trip, a linear lexer scan of the
constant-shape composed text, and the debugger loop's frame-scope merge —
an abort landing in that window is acknowledged and lost, which the abort
tool's retry loop re-covers within one period (§4.13); expression content
can no longer extend this window.

Two implementation requirements from the adversarial review:

- The wrapper **sets the interrupt attributes explicitly**
  (`Thread_Attributes.private_interrupts`), never inherits them: Poly/ML's
  asynchronous interrupt really is delivered only once, nothing in the
  debugger loop re-arms it, and a breakpoint taken in a deferred-interrupt
  region would otherwise be uncuttable from the start. Setting the attribute
  re-arms delivery.
- If an interrupt is ever used as a fallback anyway, `Execution.discontinue`
  must come first — without it the theory tail is permanently and silently
  poisoned (the interrupted command's result is memoised as a non-retryable
  failure; every later command fails reading it; none emits status markup;
  only an edit at or before the command recovers it). The existing global
  `Isabelle_MCP.cancel_execution` is safe precisely because it discontinues
  first.

**Timeout policy**: prover-side default 180 s, per-call `timeout` parameter
on eval and locals (both genuinely prover-side — locals goes through the
eval verb, §4.10); per-value print bound 5 s inside locals; Scala-side
backstop 210 s; implicit frame-0 locals fetch 10 s (its own prover-side
deadline, never destructive);
step/continue wait bounds **30 s**, report and never abort.

The prelude and the jar version-check each other (`mcp_prelude_version`);
debugger additions to the prelude bump both sides together.

### 7.4 Known limits

Stated honestly in tool descriptions where they bite:

- A **tight allocation-free loop** may offer no safe point; then neither the
  deadline nor the abort flag can end it, and `isabelle_cancel_evaluation`
  (global) is the only way out. **(probe:** the go/no-go experiment.**)**
- A breakpoint taken on the **protocol thread** (e.g. inside a print
  function) deadlocks the whole session — the thread that would read our
  resume command is the stopped one. This is why the global "break at the
  next site" switch stays out of v1 (§8) and why breakpoints are only ever
  explicit sites.
- Breakable sites crossed during an evaluation at a hit never fire (§4.9):
  the break hook declines while the thread is debugging.
- Every evaluation at a hit (including the locals printer) needs a generic
  context on the stopped thread; a hit on a thread without one fails the
  eval — same limit as the stock debugger.
- The constructed eval text compiles under the frame's merged name space: a
  local *structure* named `Isabelle_MCP` or `Time` in scope at the breakpoint
  would shadow the envelope's names. Vanishingly rare; noted, not defended.
- **`Isabelle_MCP_PolyML` is globally visible** (accepted, user decision
  2026-08-14): the §4.10 re-exposure writes the full `PolyML` binding into
  the raw global namespace for the whole session, so any user ML can reach
  `Isabelle_MCP_PolyML.DebuggerInterface` etc. Hiding it would be cosmetic —
  five lines of user ML re-derive the same binding — and the probes rely on
  it (the deliberately-slow `addPrettyPrinter` printer).
- The `not_stopped` refusal (§7.1) **narrows** the poisoned-input-queue
  window, it does not close it: a thread resuming between the check and the
  prover's dequeue still leaves a queued input that poisons its next stop.
  The benign inverse — an eval refused although the thread just stopped,
  because its state has not arrived — cannot bite a client that acts on a
  received hit notification: the state callback precedes the check on the
  same dispatcher.

## 8. Out of scope for v1

- Per-breakpoint enable/disable tools (the registry makes them trivial later).
- Global break — "stop at the next site wherever execution currently is"
  (protocol name `PIDE/debugger_break` stays reserved; hard reason in §7.4).
- Strict-SML evaluation mode and the explicit evaluation-context argument of
  the prover's `eval` verb.
- Exception tracing (`ML_exception_debugger`) — independent, non-interactive.
- Prelude takeover of the `Debugger.*` protocol commands (§7 preamble). (The
  one admitted exception to "no debugger logic is reimplemented" is the
  ~10-line locals printer of §4.10; it is bounded, and it exists because the
  stock `print_vals` verb offers no timeout hook.)
- Running the agent's expression in a forked task instead of on the stopped
  thread (would keep the thread responsive forever, but diverges from the
  stock debugger's semantics and thread-local state; recorded, not pursued).
- A "detach the debugger" tool and a "delete all breakpoints" tool — both
  reachable through existing tools; a tool slot is paid for on every request.

## 9. Companion documents

- [`DEBUGGER_IMPLEMENTATION_PLAN.md`](DEBUGGER_IMPLEMENTATION_PLAN.md) — how
  this gets built: the probe experiments (integration tests, not
  scaffolding), affected files, phases and gates.
- [`DEBUGGER_REVIEW_AND_DECISIONS.md`](DEBUGGER_REVIEW_AND_DECISIONS.md) —
  the historical record: the adversarial review of the 2026-08-11 draft, the
  decisions taken in conversation, and the source-study findings this
  revision rests on.
