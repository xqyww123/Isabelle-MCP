# ML Debugger Support — Design

Status: **draft, superseded in parts, and shelved as of 2026-08-11.** Nothing in
this document is implemented yet.

> **Read [`DEBUGGER_REVIEW_AND_DECISIONS.md`](DEBUGGER_REVIEW_AND_DECISIONS.md)
> first.** Ten review concerns against this document survived an adversarial
> review and none of the fixes have been folded in yet. §6.4 in particular rests
> on a premise now believed false, and §4.10/§4.11's `thread` parameter has been
> superseded by a stop identifier. That file also records the decisions taken
> after this draft was written, and why the work is currently shelved.

This document specifies interactive ML-debugger support for Isabelle-MCP: setting
breakpoints in Isabelle/ML code, being notified when evaluation stops at a
breakpoint, inspecting the stopped thread (call stack, local variables,
arbitrary ML evaluation in a stack frame), and resuming (continue / step).

The design builds exclusively on machinery that already exists in
Isabelle2025-2 and in this repository. No patch to the Isabelle distribution is
required, no session heap is invalidated, and the prover-side ML needs no
changes (all `Debugger.*` protocol commands are already registered in every
prover process by `src/Pure/Tools/debugger.ML`).

---

## 1. Glossary

These terms are used consistently throughout this document and MUST be used
consistently in the implementation (code, docstrings, user-facing messages).

- **breakpoint site** — a stopping location inserted by the Poly/ML compiler
  into instrumented ML code (one `bool ref` per site, initially `false`).
  Sites exist at statement boundaries chosen by the compiler; the front-end
  cannot create sites, only enable or disable existing ones. Each site is
  identified on the PIDE wire by a **serial** (an integer assigned at
  compile time and reported as `ML_breakpoint` markup on the site's source
  position).
- **breakpoint** — an entry in the client-side **breakpoint registry** (below):
  the user's *intent* to stop at a particular source location. A breakpoint is
  realized by enabling the breakpoint site at that location, and survives
  recompilation (which destroys and recreates sites) by re-resolution.
- **breakpoint registry** — the table of breakpoints kept by the Python layer.
  It is the single source of truth; the enabled/disabled state of prover-side
  sites is a projection of it and can be rebuilt from it at any time.
- **armed / pending / lost** — the three states of a registry entry
  (see §5).
- **anchor snippet** — the source text immediately following a breakpoint
  site, recorded in the registry to identify the site without column numbers
  and to re-anchor it after edits.
- **stopped thread** — a prover thread currently halted inside the debugger
  loop, waiting for debugger input. Identified by its Isabelle thread name
  (e.g. `Isabelle.worker-17`).
- **frame** — one entry of a stopped thread's call stack. Frame `0` is the
  innermost frame (where execution stopped).
- **debug notice** — a one-line message about a breakpoint state change
  (armed, re-armed after recompilation, moved, lost, …), buffered by the
  Python layer and delivered appended to the next tool result (§6.3).

## 2. Prerequisites and launch

### 2.1 The `debug` launch parameter

Debugger instrumentation is controlled by the Isabelle system option
`ML_debugger` (default `false`). When enabled, *newly compiled* ML code —
`ML ‹…›` blocks, `ML_file`-loaded files, etc. in the theories the agent
evaluates — is compiled with debug information and breakpoint sites. Code
already compiled into the session heap is unaffected (and cannot be debugged
unless the heap itself was built with `ML_debugger=true`, which we do not do).
`ML_debugger` is not a build-identity option: enabling it does not invalidate
any existing heap.

Because instrumentation slows down compiled ML and the Isabelle documentation
warns that instrumenting critical infrastructure may deadlock, debugging is
**opt-in per session**:

- `isabelle_launch` gains a parameter `debug: bool = false`. When true, the
  server is spawned with an additional `-o ML_debugger=true`.
- When `debug` was not enabled, every breakpoint/debugger tool fails fast with:
  `"Debugging is not enabled in this session. Re-launch with debug=true."`
- There is no way to enable debugging without relaunching (accepted trade-off).

`Debugger.init` (installing the prover-side break hook) is **implicit**: the
Scala server sends it lazily before the first debugger-related action of the
session (and re-sends on session restart). No init/exit tool is exposed.

### 2.2 Where breakpoints can exist

A location has toggleable breakpoint sites only if all of the following hold
(consequences of the prover-side mechanism, reported to the user via
`isabelle_list_breakpoints` simply as presence/absence of sites):

1. the enclosing ML command was compiled while `ML_debugger` was on (i.e. the
   session was launched with `debug=true` and the command was evaluated in it);
2. the command is part of PIDE-visible source (a file under evaluation);
3. the enclosing command has **finished** evaluating — sites of a command that
   is still running (or failed) cannot be toggled yet.

## 3. Positions without columns

No tool in this design accepts a column number. Following the repository's
existing conventions:

- `isabelle_hover` / `isabelle_definition` / `isabelle_local_occurrences` use
  `line` + `symbol` (first/all occurrences of a text on the line);
- `isabelle_evaluate_to` uses `line` + optional `after_text` snippet, matched
  on token boundaries, ASCII and Unicode forms equivalent, first occurrence.

Breakpoint tools use `line` + optional `at_text`:

- `at_text` given: the position that matters is the **first character** of the
  first occurrence of `at_text` on `line` (occurrence matching is
  ASCII/Unicode-equivalent, like `after_text` of `isabelle_evaluate_to`; the
  extent of the snippet only serves to find the occurrence). The chosen site
  is the one **at or nearest before** that character — "stop before executing
  this code", so resolution never looks past the snippet's start.
- `at_text` omitted: the **first** breakpoint site on `line`.

Conversely, tools never *return* raw column numbers as the primary identity of
a site; a site is always presented as `line` + its anchor snippet (a short
stretch of the source text starting at the site, computed by the Python layer
from the site's range and the file content).

## 4. Tools

Ten new tools, plus one changed parameter on `isabelle_launch`. Names follow
the existing `isabelle_` prefix convention. All `file_path` arguments are
absolute paths (realpath-normalized as elsewhere in the server).

Tools that return prose (`ToolResult` text, like the evaluation family) are
marked *text result*; tools with structured output list their result model.
Every result — text or structured — additionally carries pending debug notices
(§6.3).

### 4.1 `isabelle_launch` (changed)

New parameter appended to the existing schema:

```json
{
  "debug": {
    "type": "boolean",
    "default": false,
    "description": "Enable the ML debugger for this session (compiles newly evaluated ML with instrumentation; slows ML compilation/execution). Required for all breakpoint tools."
  }
}
```

### 4.2 `isabelle_set_breakpoint`

Register a breakpoint and enable its site (or leave it pending if the site
does not exist yet, §5). *Text result*: the resolved site (`line`, anchor
snippet), the entry's state, and — when resolution had to pick among several
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
      "description": "Line number (1-indexed) of the breakpoint site"
    },
    "at_text": {
      "type": ["string", "null"],
      "default": null,
      "description": "Optional text snippet on the line. Its first occurrence is located, and the breakpoint site AT or nearest BEFORE the snippet's first character is used (i.e. the anchor is the snippet's start position; execution stops before that code runs). Without at_text, the first site on the line. ASCII and Unicode symbol forms are equivalent."
    }
  },
  "required": ["file_path", "line"]
}
```

### 4.3 `isabelle_del_breakpoint`

Remove a breakpoint from the registry and disable its site (if armed). The
arguments identify the registry entry the same way `isabelle_set_breakpoint`
created it; `lost` entries can be deleted too. *Text result*: confirmation.

Input schema: identical to `isabelle_set_breakpoint`.

### 4.4 `isabelle_list_breakpoints`

Two purposes in one tool:

- **Without `file_path`**: list the whole breakpoint registry (every entry
  with file, line, anchor snippet, enabled flag, state).
- **With `file_path`** (optionally a line range): additionally list every
  **available breakpoint site** in that range of the file — this is how the
  agent discovers where breakpoints *can* be placed — each as `line` + anchor
  snippet, marked with whether a registry entry is attached to it.

```json
{
  "type": "object",
  "properties": {
    "file_path": {
      "type": ["string", "null"],
      "default": null,
      "description": "Absolute path to a .thy or .ML file. Omit to list only the breakpoint registry."
    },
    "start_line": {
      "type": ["integer", "null"],
      "minimum": 1,
      "default": null,
      "description": "First line (1-indexed) of the range to scan for available sites. Default: whole file."
    },
    "end_line": {
      "type": ["integer", "null"],
      "minimum": 1,
      "default": null,
      "description": "Last line (1-indexed, inclusive) of the range. Default: whole file."
    }
  },
  "required": []
}
```

Structured result model:

```json
{
  "type": "object",
  "properties": {
    "registry": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "file_path": {"type": "string"},
          "line": {"type": "integer", "description": "Current resolved line (1-indexed); the recorded line for pending/lost entries"},
          "anchor": {"type": "string", "description": "Anchor snippet: source text starting at the site"},
          "enabled": {"type": "boolean", "description": "User-intended state (see enable/disable_all)"},
          "state": {"type": "string", "enum": ["armed", "pending", "lost"]}
        },
        "required": ["file_path", "line", "anchor", "enabled", "state"]
      }
    },
    "available_sites": {
      "type": ["array", "null"],
      "description": "Only when file_path was given; null otherwise",
      "items": {
        "type": "object",
        "properties": {
          "line": {"type": "integer"},
          "anchor": {"type": "string"},
          "registered": {"type": "boolean", "description": "Whether a registry entry is attached to this site"}
        },
        "required": ["line", "anchor", "registered"]
      }
    },
    "notices": {
      "type": "array",
      "items": {"type": "string"},
      "description": "Pending debug notices (§6.3)"
    }
  },
  "required": ["registry", "notices"]
}
```

### 4.5 `isabelle_enable_all_breakpoints` / 4.6 `isabelle_disable_all_breakpoints`

Flip the `enabled` flag of **every** registry entry (and push the new state to
all armed sites). Pending and lost entries keep the flag for when they become
armed. Use case: temporarily silence all breakpoints for an undisturbed run,
then restore them. No parameters. *Text result*: counts (how many armed sites
were toggled, how many entries were pending/lost).

```json
{"type": "object", "properties": {}, "required": []}
```

### 4.7 `isabelle_debug_state`

Report all stopped threads with their call stacks. Callable at any time (also
returns "no thread is stopped"). *Structured result*:

```json
{
  "type": "object",
  "properties": {
    "stopped_threads": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "thread": {"type": "string", "description": "Isabelle thread name, e.g. \"Isabelle.worker-17\""},
          "stack": {
            "type": "array",
            "description": "Frames from innermost (index 0) outwards",
            "items": {
              "type": "object",
              "properties": {
                "frame": {"type": "integer", "description": "Frame index, 0 = innermost"},
                "function": {"type": "string", "description": "ML function name"},
                "file_path": {"type": ["string", "null"]},
                "line": {"type": ["integer", "null"], "description": "1-indexed, when the frame maps to visible source"}
              },
              "required": ["frame", "function"]
            }
          }
        },
        "required": ["thread", "stack"]
      }
    },
    "notices": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["stopped_threads", "notices"]
}
```

Input schema: no parameters.

### 4.8 `isabelle_eval_at_breakpoint`

Evaluate an arbitrary Isabelle/ML expression in the scope of a stack frame of
a stopped thread: the frame's ML name space (including enclosing scopes) is
merged into the evaluation environment, so the frame's local bindings are
directly usable in the expression. Antiquotations work. The evaluation is
never itself instrumented (no recursive debugging). *Text result*: the
evaluation's `writeln`/`warning`/`error` output.

```json
{
  "type": "object",
  "properties": {
    "expr": {
      "type": "string",
      "description": "Isabelle/ML source to evaluate in the frame's scope"
    },
    "thread": {
      "type": ["string", "null"],
      "default": null,
      "description": "Thread name as reported by isabelle_debug_state. May be omitted when exactly one thread is stopped; with several stopped threads it is required (the error lists them)."
    },
    "frame": {
      "type": "integer",
      "minimum": 0,
      "default": 0,
      "description": "Stack frame index (0 = innermost)"
    }
  },
  "required": ["expr"]
}
```

Deliberately **not** exposed in v1 (prover supports them; defaults are used):
strict-SML mode, and the explicit evaluation-context text (`eval`'s first
argument slot stays empty).

### 4.9 `isabelle_locals_at_breakpoint`

Print **all local variables** of a stack frame with their types and values
(prover-side `print_vals`; uses the frame's own bindings, not enclosing
scopes). This is the cheap first look before reaching for
`isabelle_eval_at_breakpoint`. *Text result*.

Input schema: like `isabelle_eval_at_breakpoint` without `expr`:

```json
{
  "type": "object",
  "properties": {
    "thread": {
      "type": ["string", "null"],
      "default": null,
      "description": "Thread name; may be omitted when exactly one thread is stopped"
    },
    "frame": {
      "type": "integer",
      "minimum": 0,
      "default": 0,
      "description": "Stack frame index (0 = innermost)"
    }
  },
  "required": []
}
```

### 4.10 `isabelle_continue_breakpoint`

Resume execution. With `thread` given, resumes that thread; without it,
resumes **all** stopped threads (the common single-thread case just works).
Enabled breakpoints stay enabled — execution stops again at the next hit.
*Text result*: which threads were resumed.

```json
{
  "type": "object",
  "properties": {
    "thread": {
      "type": ["string", "null"],
      "default": null,
      "description": "Thread name to resume. Omit to resume all stopped threads."
    }
  },
  "required": []
}
```

### 4.11 `isabelle_step_at_breakpoint`

Single-step a stopped thread. The three modes map 1:1 onto the prover's
stepping verbs:

- `step` — run to the next breakpoint site, entering calls;
- `step_over` — run to the next site at the same or shallower stack depth;
- `step_out` — run until the current function returns.

After the step the thread stops again and the new state is reported like a
breakpoint hit (§6). *Text result*: the new innermost position and stack.

```json
{
  "type": "object",
  "properties": {
    "mode": {
      "type": "string",
      "enum": ["step", "step_over", "step_out"],
      "description": "Stepping mode"
    },
    "thread": {
      "type": ["string", "null"],
      "default": null,
      "description": "Thread name; may be omitted when exactly one thread is stopped"
    }
  },
  "required": ["mode"]
}
```

## 5. The breakpoint registry and its lifecycle

Each registry entry stores: `file_path`, recorded `line`, **anchor snippet**,
`enabled` flag, current `state`, and (when armed) the site's current serial
plus the identity needed to toggle it on the wire.

States and transitions:

- **pending** — no toggleable site currently exists at the location (enclosing
  command not yet evaluated, still running, or session lacks the evaluation).
  Entries are created pending when `isabelle_set_breakpoint` cannot resolve a
  site yet; the tool result says so explicitly.
- **armed** — resolved to a live site; the site's `bool ref` mirrors the
  entry's `enabled` flag.
- **lost** — reconciliation (below) found no matching site anymore. The entry
  is kept (so the agent can inspect/delete it or the site may reappear), and
  the loss is reported as a debug notice.

**Reconciliation** runs after every evaluation round completes (and cheaply
after each file-freshness resync): for every registry entry, re-resolve the
site in the current snapshot —

1. same position, same serial → nothing to do, no notice;
2. matching site found (by anchor snippet, nearest to the recorded line —
   this also survives edits that shifted line numbers), but the serial
   changed → the enclosing command was recompiled and the old `bool ref` is
   dead; re-enable the new site according to `enabled`, update recorded
   line, and emit a notice, e.g.
   `Breakpoint FILE:12 (before ‹fold upd args›) was invalidated by recompilation — re-armed.`
   (plus `— moved to line 14` when the line changed);
3. no matching site → state becomes `lost`, notice emitted with the nearest
   available site as a suggestion;
4. a `pending` entry whose site has now appeared → arm it, notice emitted.

Every state change is reported **exactly once**, as a debug notice.

## 6. Breakpoint hits and how the agent learns about them

### 6.1 Hit = a completion signal of evaluation

The typical situation: the agent called `isabelle_evaluate_to` and is waiting
(or polling `isabelle_evaluation_status`). A stopped thread means the
evaluation cannot progress past that command, so:

- The `evaluate_to` wait loop gains a third exit condition besides
  "target processed" and timeout-with-progress: **a thread stopped in the
  debugger**. The result then leads with a hit report:

  ```
  Breakpoint hit: FILE line 12 (before ‹fold upd args›), thread Isabelle.worker-17.
  Call stack:
    0  my_function        FILE:12
    1  outer_function     FILE:34
    …
  Locals of frame 0:
    args = [t1, t2] : term list
    …
  Evaluation is paused, NOT finished. You can inspect with
  isabelle_eval_at_breakpoint / isabelle_locals_at_breakpoint /
  isabelle_step_at_breakpoint, or resume with isabelle_continue_breakpoint.
  ```

  The locals of frame 0 are included automatically (one implicit
  `print_vals`), saving a round trip in the common case.
- `isabelle_evaluation_status` reports the same "paused at breakpoint" section
  whenever stopped threads exist, so polling never misreads a stop as a hang.

### 6.2 Hits outside an evaluation wait

A background re-evaluation (triggered by a file save) can also hit an enabled
breakpoint while no `evaluate_to` is waiting. The stop is recorded in the
Python-side state and surfaced: (a) as a debug notice on the next tool call of
any kind (the existing per-call freshness hook is the natural place), and
(b) in `isabelle_debug_state` / `isabelle_evaluation_status` at any time.

### 6.3 Debug notices

MCP has no server-initiated push channel into the agent's conversation, so all
asynchronous events (reconciliation results, hits outside a wait, threads that
resumed because a command was recompiled, …) are buffered as debug notices and
**appended to the next tool result**, whatever the tool. Structured results
carry them in a `notices` array; text results append a `Debug notices:`
section. The buffer is cleared on delivery; each notice is delivered exactly
once.

### 6.4 Interaction with cancellation

A thread stopped in the debugger loop is **uninterruptible**; the existing
cancellation path cannot touch it. Therefore `isabelle_cancel_evaluation` is
extended: if stopped threads exist, it first resumes them all (debugger
`continue`), then proceeds with the normal cancellation. The result mentions
that threads were resumed out of the debugger. Additionally, to keep a
cancelled run from immediately re-stopping, the resume performed by *cancel*
temporarily disables all armed sites for the duration of the cancellation and
restores them afterwards (registry `enabled` flags are not changed).

## 7. Wire protocol: LSP extension messages

All messages follow the existing `PIDE/*` LSP-extension style of this
project. Design constraint: the server side is a thin adapter over Isabelle's
existing debugger API — it must not reimplement any debugger logic.

Client → server **requests**:

- `PIDE/debugger_breakpoints {uri, range?}` →
  `{breakpoints: [{range, serial, state}]}` — every breakpoint site
  (`ML_breakpoint` markup) in the given snapshot range. The server returns
  only ranges and serials; anchor snippets are computed client-side from the
  ranges and the file content.
- `PIDE/debugger_toggle_breakpoint {uri, serial, state}` → `{ok}` or an error
  (unknown serial / enclosing command not finished).
- `PIDE/debugger_eval {thread, frame, expr}` → `{messages: [...]}` — sends the
  debugger `eval` verb, then awaits the corresponding `debugger_output`
  protocol messages for that thread (bounded by a timeout; partial output is
  returned with a note). Same shape for `PIDE/debugger_print_vals
  {thread, frame}`.
- `PIDE/debugger_input {thread, verbs...}` → `{ok}` — raw resume verbs:
  `continue`, `step`, `step_over`, `step_out`.

Server → client **notifications**:

- `PIDE/debugger_state {threads: [{thread, stack: [{position, function}]}]}` —
  pushed on every `session.debugger_updates` event (thread stopped, resumed,
  stack changed). An empty stack for a thread means it resumed. This is the
  signal the evaluation wait loop listens for.
- `PIDE/debugger_output {thread, messages}` — spontaneous debugger output not
  consumed by a pending eval request (e.g. errors), surfaced as debug notices.

Implicit init: the Scala server calls `session.debugger.init(...)` (which
emits the prover protocol command `Debugger.init`) before serving the first
debugger request, and its session-ready hook re-issues it after a prover
restart. `Debugger.exit` is sent on shutdown only.

Reserved for later (protocol names claimed now, not implemented in v1):
`PIDE/debugger_break {state}` — the global "stop at the next site wherever
execution currently is" switch, useful for diagnosing hangs.

## 8. Out of scope for v1

- Per-breakpoint enable/disable tools (the registry makes them trivial to add
  later).
- Global break — "stop at the next site wherever execution currently is"
  (protocol slot reserved, §7).
- Strict-SML evaluation mode and the explicit evaluation-context argument of
  the prover's `eval` verb (defaults are used, §4.8).
- Exception tracing (`ML_exception_debugger`) — an independent,
  non-interactive feature.

## 9. Companion document

How this specification gets implemented — affected source files, the probe
experiments that must validate the load-bearing assumptions first, and the
staging of the work — is planned separately in
[`DEBUGGER_IMPLEMENTATION_PLAN.md`](DEBUGGER_IMPLEMENTATION_PLAN.md).
