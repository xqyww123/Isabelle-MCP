# Evaluation Model Redesign v0.3.0

Date: 2026-05-26 (updated from 2026-05-25 problem analysis)

This document records all findings from the audit of the v0.2.0 async
evaluation model and the design for v0.3.0, based on source-code analysis
and empirical testing of both Isabelle2024 and Isabelle2025-2
`vscode_server`.

---

## 1. Isabelle Execution Model (Ground Truth)

### 1.1 Intra-file execution is sequential with forked proofs

The eval chain within a single file is strictly sequential: commands are
processed in document order.  If line N is processed, all lines before N
are guaranteed to also be processed.  There are no gaps.

However, with `parallel_proofs >= 1` (default), proof bodies are forked
to background threads.  The main eval chain immediately continues past
`qed` with a future-result placeholder.  This means:

- A proof at line 30 (`by auto`) can show as `background_running1` while
  commands at lines 35–50 have already finished processing.
- The `PIDE/decoration` notification reflects this: line 30's range is
  in `_running`, while lines 35–50 have no unprocessed/running status.

Diagnostic commands (`thm`, `term`, etc.) are also forked when
`parallel_proofs >= 1`.

### 1.2 Inter-file parallelism

When file A imports B and C (where B and C are independent), Isabelle
processes B and C **in parallel** via the Future system.  A's commands
wait until all imports are finished (`finished_import` check in
`document.ML`).

### 1.3 Dependency files are auto-loaded

When file A.thy is opened via `didOpen` and imports B.thy, the
`vscode_server` automatically discovers B.thy via `resolve_dependencies`
(transitive), reads it from disk, creates an internal model with
`external(true)`, and processes it fully (`node_required`).

The client does **not** need to send `didOpen` for B.thy.

### 1.4 Dependency files get NO decorations (EMPIRICALLY VERIFIED)

Auto-loaded dependency files have `external_file = true` →
`node_visible = false`.  The `publish()` method in `vscode_model.scala`
returns empty decorations for invisible nodes.

**Empirically verified (2026-05-25):** Created A.thy importing local
B.thy, opened only A.thy via didOpen.  B.thy received zero
PIDE/decoration notifications — both when B.thy was error-free and when
it contained a failing proof.

### 1.5 Dependency diagnostics are ONLY sent when errors exist (EMPIRICALLY VERIFIED)

`publishDiagnostics` is only sent when the diagnostics list **changes**
from the previous state.

**Empirically verified (2026-05-25):**
- B.thy with a failing proof: both A.thy and B.thy received diagnostics.
- B.thy error-free: neither received diagnostics.

Consequence: `diagnostic_cache` alone cannot discover clean dependencies.

### 1.6 didOpen for dependencies: decoration cost (EMPIRICALLY VERIFIED)

Opening a previously-external file via `didOpen` flips
`external_file=false` → `node_visible=true`.  This does NOT re-process
the file (it's already processed), but it enables decoration output.

**Decoration rendering cost** (from source analysis):
- Each visible file requires O(document_lines) `snapshot.cumulate` calls
  per flush cycle (dominated by `text_overview_color` scanning every line).
- For already-processed files, the markup computation is real work.
- For 60 visible files, each flush cycle does ~60× the rendering work.

**Recommendation:** Only `didOpen` files when line-level detail is needed.

### 1.7 Perspective and dependencies

With `vscode_caret_perspective = 1`, the perspective covers ~1 line
around the caret.  Dependency files are always fully processed
(`node_required`), regardless of perspective.

### 1.8 Isabelle does not stop at errors

An error at line 10 does not prevent processing line 11.  Subsequent
commands are still parsed and checked.

### 1.9 `consolidated` flag (EMPIRICALLY VERIFIED)

`consolidated = true` means all commands have been executed AND the
kernel's post-processing task completed.  It does **not** require
success — a theory with `failed > 0` will still reach
`consolidated = true`.

`consolidated` and `ok` are **independent**:

| Scenario | consolidated | ok |
|----------|-------------|-----|
| All succeeded | true | true |
| Has errors, processing done | **true** | **false** |
| Still processing | false | — |
| Canceled | **false** (never consolidates) | — |

**Empirically verified (2026-05-25):** B.thy with failing proof reached
`consolidated=true, ok=false` after the proof completed.

### 1.10 Malformed theories NEVER consolidate (EMPIRICALLY VERIFIED)

**Empirically verified (2026-05-26):**

| Theory | consolidated | ok | percentage | Detail |
|--------|-------------|-----|-----------|--------|
| Missing `end` | **false forever** | true | 99% | All commands finished, can't consolidate |
| Complete gibberish | **false forever** | false | 99% | 1 failed, never consolidates |
| Header only (no begin/end) | **false forever** | false | 50% | 1 permanently unprocessed |

**Root cause (from source):** `finished_result_theory` calls
`Toplevel.end_theory` which requires the state to be at Toplevel (after
`end`).  Without a proper `end` command, this fails → `could_consolidate`
returns false → consolidation never attempted.

### 1.11 A.thy can consolidate before B.thy

A.thy importing B.thy: if B.thy has a forked proof still running, A.thy
can reach `consolidated=true` first, because the forked proof's result
is registered as a future in B.thy's theory state.  A.thy sees the
future and proceeds.

Consequence: **A.thy's `consolidated` does not guarantee dependencies
are consolidated.**  Must check dependencies explicitly.

### 1.12 Decoration timing

Decorations are event-driven with a two-tier debounce:
- Tier 1 (0.1 s): `Delay.first` — fires on first event after quiet.
- Tier 2 (0.5 s): `Delay.last` — resets on every new event.

Typical latency: ~0.6 s after processing quiesces.

### 1.13 PIDE/theory_status (CUSTOM PATCH)

A patched `PIDE/theory_status` LSP request (applied to Isabelle2025-2)
returns `Document_Status.Node_Status` for ALL loaded theories — both
explicitly opened and auto-loaded dependencies.

See `contrib/my_better_isabelle_prover/.../patches/theory_status.md` for
full protocol documentation.

Key fields per theory: `node_name`, `theory_name`, `external`,
`imports` (dependency graph), `ok`, `total`, `unprocessed`, `running`,
`warned`, `failed`, `finished`, `canceled`, `consolidated`, `percentage`.

---

## 2. v0.3.0 Design

### 2.1 Two-tier status reporting

| Tier | Source | Coverage | Provides |
|------|--------|----------|----------|
| File-level | `PIDE/theory_status` (request) | ALL theories | running/failed/consolidated/percentage + dependency graph |
| Line-level | `PIDE/decoration` (push) + ProcessingTracker | Only `didOpen` files | Specific running/unprocessed line ranges + elapsed time |

### 2.2 didOpen strategy

Only `didOpen` two categories of files:
- **Target file** (the file being evaluated)
- **Files with `ok=false`** from theory_status (to get line-level error detail)

All other files rely on theory_status file-level data.

### 2.3 evaluate_to completion condition

`evaluate_to(file, line)` is complete when:
1. Target line is NOT in any `_unprocessed` range (running is OK — a forked proof
   at line N means the eval chain has passed line N; the proof runs in the
   background and queries return valid results, possibly with a "still running" note)
2. All recursive dependencies are "done"

A dependency is "done" if:
```
canceled == true                                  # canceled (never consolidates)
OR consolidated == true                           # normal completion
OR (running == 0 AND unprocessed == 0)            # all commands tried, can't consolidate
OR (running == 0 AND ok == false)                 # has errors, nothing running
```

This handles malformed theories that never consolidate (§1.10) and canceled
theories (§1.9).

### 2.4 Evaluation guard (query tools)

`check_evaluation_guard` blocks only on **unprocessed** lines (not running):
- Line unprocessed → auto-start evaluation
- Line running → allow query, add `note` warning to response
- Line processed → allow query normally

### 2.5 EvaluationResult structure

```
EvaluationResult:
    status: "complete" | "in_progress" | "no_evaluation" | "cancelled"
    theories: list[TheoryStatus]           ← from PIDE/theory_status
    running_commands: list[RunningCommand]  ← from ProcessingTracker (opened files)
    errors: list[DiagnosticMessage]        ← from diagnostic_cache
    destination_line: int | None
    message: str
```

Both `evaluate_to` and `evaluation_status` return this same structure.

### 2.6 RunningCommand model

```
RunningCommand:
    file_path: str
    start_line: int         # 1-indexed
    end_line: int           # 1-indexed
    text: str               # command source text
    elapsed_seconds: float  # time since first seen as running
```

Elapsed time tracked via onset timestamp diffing in ProcessingTracker.

### 2.7 Evaluation cancellation (EMPIRICALLY VERIFIED)

**PIDE/cancel_execution** is a custom LSP request (patched into
Isabelle2025-2) that atomically stops ALL processing globally — both the
target file and all dependency theories.

See `contrib/my_better_isabelle_prover/.../patches/cancel_execution.md`
for full protocol documentation and test results.

#### Implementation

```ml
fun cancel_execution () =
  let
    val groups = change_state_result (fn (_, nodes, execs) =>
      let val groups = Inttab.fold (fn (_, (gs, _)) => fn acc => gs @ acc) execs []
      in (groups, (Document_ID.none, nodes, execs)) end);
  in List.app Future.cancel_group groups end;
```

Atomic operation:
1. Set `execution_id := Document_ID.none` → prevents new commands
2. Collect all `Future.group` from execs → `Future.cancel_group` each →
   `interrupt_thread` on running workers

**Empirically verified (2026-05-27):** running=1 → running=0 within 2s.
Stops ALL theories including dependencies.

#### Cancel + prevent restart

`PIDE/cancel_execution` alone stops processing, but any subsequent
`textDocument/didChange` triggers `Document.update` → new `execution_id`
→ processing resumes.  To prevent automatic restart:

1. `PIDE/cancel_execution` → stops all processing globally
2. `PIDE/caret_update` to line 0 → restricts perspective
3. `textDocument/didChange` (append space) → triggers `Document.update`
   with restricted perspective; only header area re-processes

Recovery: `evaluate_to` moves caret to target line + edit → normal
perspective → processing resumes naturally.

#### Failed approaches (for reference)

**Empirically verified (2026-05-26):** Without `PIDE/cancel_execution`:

| Approach | Result | Why |
|----------|--------|-----|
| Caret move only | No effect | Caret not flushed without edit |
| Edit only (caret at end) | No effect | Command re-executes (still in perspective) |
| Insert+delete pair | No effect | `Delay.last(0.1s)` batches → zero net change |
| Caret-to-0 + edit | Stops target only | Dependencies remain `node_required=true` |

#### Note on `private_interrupts`

`Execution.fork` wraps command bodies in `private_interrupts`.  Thread
interrupts are stored but NOT immediately raised — commands must call
`Isabelle_Thread.expose_interrupt()`.  Proof methods do this periodically
(interrupted within ms).  Blocking calls like `OS.Process.sleep` do not
check and continue until natural completion.

`Task_Queue.enqueue` registers each task in ALL ancestor groups via
`fold_groups`, so `cancel_group` on a parent group correctly finds and
interrupts tasks in subgroups created by `Execution.fork`.

---

## 3. Design Constraints from Isabelle's Protocol

1. **One global caret** — state panel queries must serialize via caret lock.
2. **No decoration for `external` files** — must `didOpen` to get line-level data.
3. **No document version in `publishDiagnostics`** — diagnostics cannot be correlated with versions.
4. **Decoration debounce** — ~0.5 s latency between processing completion and notification.
5. **Malformed theories never consolidate** — completion checks must have fallbacks.
6. **`Delay.last` batching** — rapid edits are batched; insert+delete pairs produce no net change.
7. **`private_interrupts`** — worker threads defer interrupts; not immediate for all commands.

---

## 4. Verified Non-Issues

### 4.1 Tracker initialization for instantly-processed files

`color_decorations` always generates entries for ALL types (with empty
content if no ranges).  The tracker initializes correctly.

### 4.2 `range_processed` overlap logic

Correct and does not assume contiguous processing.

### 4.3 Processing is monotonic within the eval chain

If line N has no unprocessed/running status, all lines before N are also
processed.  Only forked proofs create apparent out-of-order "running".
