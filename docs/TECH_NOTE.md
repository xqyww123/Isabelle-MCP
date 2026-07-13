# Tech Note: Diagnostics vs Decoration Channels, and the EvaluationResult Redesign

Date: 2026-06-05

This note records a source-code + empirical investigation into **how Isabelle's
`mcp_server` reports errors, warnings, and `sorry`/`oops`**, and the resulting
design for what `EvaluationResult` should surface and how it should be rendered.

All Scala/ML citations are from the vendored distribution at
`contrib/Isabelle2025-2/src/` (verified identical behaviour in `Isabelle2024/`).
Python citations are from `src/isabelle_mcp/`. Empirical runs used the MCP's own
`IsabelleLSPClient` driving a real `isabelle mcp_server` (HOL), with the
decoration handler subclassed to capture *all* decoration types (probe scripts
were kept under `/tmp/isa_mcp_test/`).

---

## 1. Two independent push channels

Isabelle's `mcp_server` pushes prover feedback to the client over **two
distinct LSP channels**, with **different content, granularity, and semantics**.
Neither is pulled — both are server-initiated notifications.

| | `textDocument/publishDiagnostics` | `PIDE/decoration` |
|---|---|---|
| Direction | push (server → client) | push (server → client) |
| Carries | error / (legacy-)warning messages | colour/markup ranges (status, bad, dotted, syntax) |
| Granularity | **whole-file full list** every time it changes | **per-type delta** (only changed decoration types) |
| Coverage | the file's complete diagnostic set | whole document, reflecting the **processed** region |
| MCP today | cached verbatim, used by tools | **mostly discarded** (only 2 types kept) |

### 1.1 Diagnostics channel — what is actually in it

The only severity mapping in the server (`vscode_rendering.scala:44-47`):

```scala
private val message_severity =
  Map(Markup.LEGACY -> LSP.DiagnosticSeverity.Warning,   // 2
      Markup.ERROR  -> LSP.DiagnosticSeverity.Error)      // 1
```

and the set of markup that becomes a diagnostic at all
(`vscode_rendering.scala:52-53`, plus the `Markup.Bad` case at `:145-149`):

```scala
private val diagnostics_elements = Markup.Elements(Markup.LEGACY, Markup.ERROR)
```

So the diagnostics channel contains **exactly three things**:

| markup | LSP severity | meaning |
|---|---|---|
| `Markup.ERROR` | 1 = Error | real errors (type errors, undefined consts, syntax…) |
| `Markup.Bad`   | **None** (omitted → client defaults to Error) | "bad" regions, e.g. `Failed to finish proof` |
| `Markup.LEGACY`| 2 = Warning | **only** legacy / deprecated-feature warnings |

Consequences:

- `DiagnosticSeverity.Information(3)` and `Hint(4)` are defined in `lsp.scala:500-505`
  but are **dead code** — never assigned. A full-tree grep finds only the two
  `Warning`/`Error` uses. So worrying about a flood of info/hint diagnostics is moot.
- **Plain `Markup.WARNING` is NOT a diagnostic.** Ordinary warnings only get a
  dotted-underline *decoration* (`dotted_warning`), never `publishDiagnostics`.
  Verified empirically: `ML ‹warning "…"›` produced `dotted_warning` +
  `text_overview_warning` but **zero** diagnostics.
- Therefore: **diagnostics ≈ "errors + legacy warnings"**, not "errors + all warnings".

MCP side: each `publishDiagnostics` carries one `uri` and the **full** list for
that file; the handler stores it with whole-list replacement
(`lsp_client.py:447-462`, `:458` `self.diagnostic_cache.diagnostics[file_path] = diagnostics`).
"All files" is assembled by the MCP iterating `open_documents`, not pushed at once.

### 1.2 Decoration channel — granularity and coverage

`publish` decides what to re-send (`vscode_model.scala:197-209`):

```scala
val changed_diagnostics =
  if (diagnostics == published_diagnostics) None else Some(diagnostics)   // whole list
val changed_decorations =
  if (decorations == published_decorations) None
  else if (published_decorations.isEmpty) Some(decorations)               // first time: all types
  else Some(for { (a,b) <- decorations zip published_decorations if a != b } yield a)  // only CHANGED types
```

- **Diagnostics**: when changed, the **entire** list is re-sent (full replace).
- **Decorations**: the first push carries **all** types; every later push carries
  **only the decoration types whose content changed**. Within an included type the
  range list is the full set for that type. → A client must **accumulate per type**
  across pushes; this is exactly what `ProcessingTracker.update` does
  (`processing.py:64-79`, replaces a type's ranges only when that type is present).
  Empirically confirmed: push 0 carried ~43 types; push 1 carried only the changed
  subset (`background_running1`, `background_bad`, …).

**Crucial — decoration covers the whole document, not the caret window.**
`decorations` iterates `model.content.text_range` (the entire file),
`vscode_rendering.scala:214-225`:

```scala
def decorations: List[VSCode_Model.Decoration] =
  color_decorations("background_", …, background(…, model.content.text_range, …)) :::
  …
  color_decorations("dotted_", …, dotted(model.content.text_range))
```

The per-line *content* reflects whether that line was **processed** (processed →
real markup like `background_bad`; not processed → `background_unprocessed1`).
**How far processing extends is set by the caret perspective**
(`vscode_model.scala:101-137`): the text-perspective window is
`[caret − cp, caret + cp + 1]` where `cp = vscode_caret_perspective`, and
processing extends sequentially up to the window's upper bound.

This was initially mis-stated as "decoration is limited to caret ±cp". **That is
wrong** — see the experiments in §3.

---

## 2. How `sorry` / `oops` are reported

### 2.1 `sorry`

`sorry` reports `Markup.bad ()` with text "Skipped proof"
(`skip_proof.ML:19-22`, invoked from `proof.ML:1221` / `:1225`), which the shared
renderer turns into the `bad` background colour (`rendering.scala:483-484`,
`Markup.BAD ∈ background_elements`). In the VSCode server this surfaces as a
**`PIDE/decoration` entry of type `background_bad`**.

It is **NOT** a diagnostic, **NOT** a command/theory status: command status uses
`proper_elements`/`liberal_elements` = `{ACCEPTED, FORKED, JOINED, RUNNING,
FINISHED, FAILED, CANCELED} ∪ {WARNING, LEGACY, ERROR}`
(`document_status.scala:97-102`); `bad` is none of these, so the `sorry` command
is counted as **`finished`** (`:243`), not `warned`/`failed`.

Empirically (theory with `lemma … sorry`):
- `isabelle_diagnostics` → empty for the sorry line; the only diagnostic was an
  unrelated genuine error.
- `command_output` at the sorry → `[normal] theorem …` only, no warning.
- `theory_status` → `warned: 0`, the sorry command among `finished`.
- `PIDE/decoration` → `background_bad` covering the `sorry` keyword. ✔

So: **`errors`/`theory_status` being clean does NOT mean "really proved" — it can
be `sorry`.** The only signal is `background_bad` on the decoration channel.

### 2.2 `oops`

`oops` produces **nothing** — no diagnostic, no `background_bad`, no warning.
It abandons the proof without axiomatically introducing the lemma, so there is
nothing to flag. Verified empirically (no decoration/diagnostic at the `oops`).

### 2.3 Distinguishing `sorry` from a genuine failure

`background_bad` is generic "bad" markup; it also covers a **failed** tactic
(e.g. `by simp` on a false goal also gets `background_bad` on `by`). They are
distinguished by the diagnostic channel:

> A `background_bad` range whose line has **no** ERROR/Bad diagnostic = a
> skipped (`sorry`) proof. A `background_bad` line that **also** has an error
> diagnostic is the genuine failure (already reported as an error).

Empirically: `sorry`@line9 → `background_bad`, no diagnostic; failed `by`@line16 →
`background_bad` **and** an error diagnostic.

---

## 3. Decoration coverage vs the caret — the decisive experiments

Setup: a theory containing a clean proof, a `sorry`, an `ML ‹warning …›`, an
`ML ‹writeln …›`, and a genuine error, with `vscode_caret_perspective = 1` (the
MCP default), driven through a real server.

**Experiment A — set & clear of `background_bad`.** Process with `sorry` present,
then `didChange` the `sorry` into a real `by simp` proof and re-process:
- Phase 1: `background_bad = [line8(sorry), line15(failed by)]`
- Phase 2: `background_bad = [line15]` — the sorry's mark was **cleared**.
→ Both *setting* and *clearing* are faithfully pushed (per-type full replace).

**Experiment B — coverage is the processed region, not the caret window.**
Caret at line 13 (0-indexed 12), `cp = 1` → caret window `[11..14]`:

```
background_bad:          [8]            >>> OUTSIDE the [11..14] window, still pushed <<<
dotted_warning:          [10]           >>> OUTSIDE the window, still pushed <<<
background_unprocessed1:  [14,15,17]     (region past the caret)
theory_status: finished=13 failed=0 unprocessed=6   (error past caret NOT processed)
```

The `sorry`@8 and warning@10 are 4–6 lines from the caret, **well outside** the
±1 window, yet are pushed. The genuine error past the caret is **not** processed
(`failed=0`), so it neither decorates as bad nor produces a diagnostic.

**Conclusions (re-verified):**
1. `cp = 1` does **not** restrict decoration to the caret window. Decoration's real
   markup covers the whole **processed** region `[1..caret]`, independent of window size.
2. Processing extent is bounded by the caret (the "evaluate only up to caret"
   semantics is intact).
3. **Do NOT raise `vscode_caret_perspective`** to widen decoration — it widens
   *processing* too (window upper bound `caret + cp + 1`), pushing evaluation to
   EOF and defeating "evaluate only up to caret". (Confirmed: a large `cp` left the
   whole theory `consolidated`.)

→ For the MCP this is ideal: a normal `evaluate_to(destination)` already yields
real `sorry`/`warning` markup for all of `[1..destination]`, with `cp = 1`, no
sweeping, no perspective change, no boundary violation. The MCP only needs to
**stop discarding** these decorations (`parse_decoration_ranges`, `processing.py:14`,
keeps only `background_unprocessed1`/`background_running1`).

### 3.1 Categorization scheme — verified reliable

A theory with 2 sorries, 2 errors (one **failed proof** = `Bad` markup, one
**type error** = `ERROR` markup), and 2 plain warnings was processed; decoration
and diagnostic channels were captured and classified:

```
text_overview_error   = [9, 11]      (= diagnostics error lines, exact MATCH)
text_overview_warning = [17, 19]
background_bad         = [6, 9, 15]   (sorryA, failed-proof, sorryB)
=> errors   = text_overview_error            = [9, 11]   ✔
=> warnings = text_overview_warning          = [17, 19]  ✔
=> sorry    = background_bad − errors         = [6, 15]   ✔
```

Findings:
- `text_overview_error` matches the diagnostics error set **line for line** — so
  either may be used for "error lines"; using diagnostics makes the snapshot's
  error lines identical to what `isabelle_diagnostics` later shows.
- A **type error** (`ERROR` markup) appears in `text_overview_error` but **not** in
  `background_bad`; a **failed proof** (`Bad`) appears in **both**. Subtracting
  errors from `background_bad` therefore yields exactly the `sorry` lines.
- Residual untested edge: a bare "malformed input" `Bad` that is neither a sorry
  nor a failed proof — if it is absent from `text_overview_error` it would be
  miscounted as `sorry`. In practice such input also raises a parse error. Low risk.

### 3.2 Where warning *messages* live

`publishDiagnostics` does not carry plain warnings (§1.1), but the warning **text**
is a command-output message. Verified: `isabelle_command_output` at the warning
line returns `[warning] a plain ML warning`. So:
- error detail → `isabelle_diagnostics` (diagnostics channel)
- warning detail → `isabelle_command_output` at that line (output channel)
- sorry → no further detail (command_output at a `sorry` shows only the resulting
  `[normal] theorem …`)

`theory_status.warned` also counts plain-warning commands (`liberal_elements ∋
WARNING`), so a non-zero `warned` is a cheap "there are warnings" signal (count
only, no locations — locations come from `text_overview_warning`/`dotted_warning`).

---

## 4. The full set of decoration types (empirical)

Non-empty types seen for a realistic theory, split by usefulness:

**Semantic (proof / message status) — worth consuming:**

| type | meaning |
|---|---|
| `background_bad` | errors **and** `sorry`/skipped proofs (red background) |
| `dotted_warning` | **plain warnings** (the ones absent from diagnostics) |
| `dotted_information` | info-level messages |
| `dotted_writeln` | commands that emitted normal/`writeln` output |
| `text_overview_error` / `_warning` / `_running` / `_unprocessed` | overview-ruler markers |
| `background_unprocessed1` / `background_running1` | processing status (already tracked) |
| `background_canceled` / `background_intensify` | cancelled / emphasised |

**Cosmetic (syntax highlighting) — ignore:** `text_main`, `text_keyword1/2/3`,
`text_operator`, `foreground_quoted/antiquoted`, `text_inner_*`, `text_free/bound/
var/skolem/tfree/tvar`, `text_comment*`, `text_class_parameter`, `spell_checker`, …

Headline: **plain warnings live ONLY on the decoration channel**
(`dotted_warning` + `text_overview_warning`), never in diagnostics. If
`EvaluationResult` is to mention warnings at all, decoration is the only source.

---

## 5. The current EvaluationResult is wrong in two ways

1. **Incremental "new errors only".** `EvaluationState.reported_errors`
   (`evaluation.py:172-191`, `:216-228`) deduplicates by `(path, line, msg)` across
   the `evaluate_to → status → status …` session and returns only the *delta*. This
   makes "fixed?" undecidable: once an error has been reported, a later `status`
   returns `errors: []` whether the error was **fixed** or merely **already
   reported**. The field name `errors` is also a misnomer — it holds `DiagnosticMessage`
   with severity error *and* warning (`models.py:160-165`).

2. **Completion races ahead of the diagnostic push.** `evaluate_to` can return
   `status: complete, errors: []` while the same result's `theory_status` already
   shows `failed: 1` — the error's `publishDiagnostics` (push) lagged the completion
   signal (pull). Verified: the genuine error only appeared on a later
   `isabelle_diagnostics` call. Completion must be gated on diagnostics having
   **settled** (`diagnostics_settled`, `lsp_client.py:924`), not just on the
   processing tracker reaching the line.

> **Update (superseded — completion stays on `line_reached`).** The redesign did
> *not* gate completion on `diagnostics_settled`; that requirement was dropped.
> Completion still gates only on the processing tracker reaching the destination line
> (`line_reached`, `evaluation.py:_is_evaluation_complete`). The race in item 2 no
> longer applies because the snapshot is built from **decoration channels, not the
> diagnostics push**: a still-running forked proof is surfaced in the new `running`
> column instead of being silently treated as "clean". Verified: a pending fork is
> always covered by an `unprocessed`/`background_running1` decoration until it
> atomically flips to `background_bad` — there is no limbo window where it is neither
> running nor failed, so reading decoration at `line_reached` never reports a false
> "clean". The historical investigation above is retained for context.

---

## 6. EvaluationResult redesign — spec

### 6.1 Principles

- **Full current state, never a cross-call delta.** Each call reports the complete
  current set so "fixed?" is decidable: fixed = absent from a *settled, complete*
  set. Drop `reported_errors`.
- **Gate `complete` on settled diagnostics** (§5.2), else `errors: []` is a lie.
- **`isabelle_diagnostics` stays the full-detail escape hatch.** After context
  compaction an agent can re-fetch everything there, so EvaluationResult may stay lean.
- **Line + command, never column.** LLMs mishandle columns. Map each
  diagnostic/decoration to the enclosing Isar command via the existing command-span
  resolver (as in `command_output`/`goal`) and show `line N: <command source>`.
- **Manual string rendering, no output schema** (like `format_command_output`);
  build with an `io.StringIO` buffer, not raw `+` concatenation. (Also refactor the
  existing `format_command_output` to use a buffer.)

### 6.2 Sources per section (with dedup)

| section | source | shown |
|---|---|---|
| **Errors** | diagnostics (`ERROR` + `Bad`) | line + command + **full message** |
| **Sorry / skipped** | `background_bad` ranges with **no** error diagnostic on that line | line + command |
| **Warnings** | `dotted_warning` (+ legacy warnings from diagnostics) | line + command, **no detail** |

`oops` is intentionally not reported (it introduces nothing).

### 6.3 Per-file cache + "unchanged"

- Cache, **per file**, the signature `(error set + sorry set + warning set)`.
- If a file's signature is unchanged since last report, print
  `Foo.thy: unchanged (still N errors, K sorry, M warnings)` — **with counts** —
  **regardless of `in_progress`**.
- A fresh `evaluate_to` **resets the target file's** cache (forces a full reprint
  as a clean baseline).
- This is an ETag-style change-detector, *not* the old incremental: "unchanged" is
  an unambiguous statement about the complete set the agent already has.

### 6.4 Decoration plumbing required

- Stop discarding decorations: track at least `background_bad` and `dotted_warning`
  per file, accumulating per type (the `ProcessingTracker` pattern), in addition to
  the existing `unprocessed1`/`running1`.
- **No** change to `vscode_caret_perspective` (keep 1) and **no** caret sweeping —
  §3 shows the processed region is already fully decorated.

### 6.5 Rendering format (agreed)

Compact decoration snapshot — counts + line ranges only, **no** diagnostic detail
(detail is fetched from `isabelle_diagnostics` / `isabelle_command_output`). One
listed line-span per decoration marker; count = number of markers (no cross-marker
merging). Categories on separate lines (`sorry` is its own category, not folded
into errors). Example:

```
Evaluation complete — reached line 18.

Test.thy
    1 error:   line 16
    1 sorry:   line 9
    1 warning: line 11
```

`Foo.thy — unchanged (1 error, 1 sorry, 1 warning)` when a file's
`(errors + sorry + warnings)` signature is unchanged (per §6.3); `Bar.thy — clean`
when none. Paths shown **relative** to the project root (the per-agent stdio server
has a stable CWD); resolved to absolute internally for `file://` URIs and all
path-keyed state (file watcher, trackers, caches). `oops` is not reported.

### 6.6 Resolved / open decisions

Resolved: sorry is its own category (✔); count = #markers, one span each (✔);
sources = `text_overview_error` (errors) / `text_overview_warning` (warnings) /
`background_bad − errors` (sorry), **verified reliable** (§3.1); relative paths,
absolute internally (✔). `vscode_caret_perspective` widening **rejected** (§3).

**`isabelle_diagnostics` will be deleted.** Verified: `isabelle_command_output`
returns the same error message as diagnostics (`[error] Failed to finish proof…`)
*plus* every other message kind (warning/normal/writeln/state) per command. So
`command_output` is a superset of `diagnostics` in message content; `diagnostics`'
only unique value was the bulk range scan — and that "discovery" role is now served
by the EvaluationResult snapshot (which lists every error/warning/sorry line). The
tool surface converges to: **EvaluationResult = discovery (where), `command_output`
= detail (what, all kinds)**. (The `diagnostic_cache` / `publishDiagnostics` handler
stays — EvaluationResult still uses it for error lines; only the public *tool* is
removed. `DiagnosticMessage` stays where other tools embed it.)

Still open:
1. Warnings: `text_overview_warning`/`dotted_warning` only, or also
   `dotted_information` (info messages)?

> **Update (what actually shipped).** The plan in §6–§7 below is the historical
> design; two of its decisions were reversed at implementation time:
> 1. **No separate `sorry` category.** `errors` is the line-deduped **union** of
>    `text_overview_error` and `background_bad`, so a `sorry`, a failed proof, and a
>    killed command all count as errors. `warnings = text_overview_warning`; the new
>    `running = background_running1` column surfaces still-executing forked proofs.
> 2. **Completion stays on `line_reached`, not `diagnostics_settled`** (see the §5.2
>    update). The snapshot is decoration-only — no diagnostics channel is read for it.
> Also implemented: the fallback for a file with no decoration (e.g. an unopened
> dependency) reports `theory_status` **counts** (failed→errors, warned→warnings, no
> line numbers), using `unprocessed`/`consolidated` for "in progress" vs "clean".
> The internal structured result is the `EvaluationView`/`FileSnapshot` dataclass pair
> rendered by `format_evaluation_result` (`evaluation.py`); the `isabelle_diagnostics`
> tool and `tools/diagnostics.py` are deleted, while the diagnostic cache /
> `publishDiagnostics` handler / `DiagnosticMessage` remain (now used only by hover).

---

## 7. Consolidated implementation plan

### 7.1 Tool surface

- **Delete** the `isabelle_diagnostics` MCP tool (registration in `server.py`,
  `tools/diagnostics.py`). Keep `diagnostic_cache`, the `publishDiagnostics`
  handler, and `DiagnosticMessage`.
- **Discovery** → EvaluationResult snapshot. **Detail** → `isabelle_command_output`.

### 7.2 Decoration plumbing (`lsp_client.py` / `processing.py`)

- Stop discarding decorations. In addition to `background_unprocessed1`/`running1`,
  accumulate per file (per-type full-replace, the `ProcessingTracker` pattern):
  `background_bad`, `text_overview_error`, `text_overview_warning` (and
  `dotted_warning` if not using overview).
- Keep `vscode_caret_perspective = 1`; **no** sweep, **no** widening (§3).

### 7.3 EvaluationResult (`evaluation.py`)

- **Drop** `reported_errors` / the incremental delta. Build the **full** current
  per-file snapshot each call.
- Categorize (§3.1): `errors = text_overview_error` (≡ diagnostic error lines),
  `warnings = text_overview_warning`, `sorry = background_bad − errors`.
- Gate `complete` on diagnostics/decoration having **settled** (`diagnostics_settled`),
  not just the processing tracker reaching the line (§5.2 race).
- Per-file cache of the `(errors, sorry, warnings)` signature → print
  `unchanged (N errors, K sorry, M warnings)` when unchanged (regardless of
  `in_progress`); a fresh `evaluate_to` resets the **target file's** cache.

### 7.4 Rendering

- Manual string, `output_schema=None` for `evaluate_to`/`evaluation_status`/
  `cancel_evaluation`; build with `io.StringIO`. Format per §6.5 (counts + line
  spans, one span per marker, sorry its own line, paths relative to project root).
- Refactor `format_command_output` to use a buffer too.

### 7.5 Paths

- Accept + display **relative** paths; normalize to absolute internally for
  `file://` URIs and all path-keyed state. **Resolved:** the project root is the
  per-agent stdio server's CWD (`os.path.realpath(os.getcwd())`), set in the
  lifespan; a path that escapes the root falls back to absolute.

### 7.6 Adjacent / out-of-scope work

- **Done:** Shared HTTP server → **stdio-per-agent** (each bound to its agent's
  process). `--http`/`--host`/`--port` removed; stdio is the only transport. The
  session is chosen at run time via `isabelle_launch(session, session_dirs=None)`
  (+ `isabelle_terminate()`), not a CLI/config arg.
- `instructions.py` — owned by the user; should gain an "`isabelle_launch` first"
  step in its workflow (left to the user).
- After implementing the above, re-sync `SPECIFICATION.md`, `API_DESIGN.md`,
  `ARCHITECTURE.md`, `README.md` (tool list loses `isabelle_diagnostics`; the
  evaluation tools' output is now a plain string).
