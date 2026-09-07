# Review context: "⌂ → file:line" and roots-based project root in Isabelle-MCP

Repository: /home/qiyuan/Current/MLML/contrib/Isabelle-MCP (uncommitted working-tree changes).
Full diff (without the binary jar): ai-artifacts/here-review/change.diff

## What the user asked for (the spec)

1. In the output of the MCP tool `isabelle_command_output`, positions that Isabelle prints as the
   placeholder `⌂` should instead read `<path relative to Isabelle-MCP's working directory>:<line>`.
   Background: Isabelle's `Position.here` (Pure/General/position.ML) prints `⌂` (`\<^here>`) when a
   position carries only a PIDE command id + offset and no line/file. Every position from a theory
   in the live document is like that, because PIDE sends commands to the prover without line/file
   (Pure/PIDE/document.ML `define_command` starts each command at `Position.id id`, line 0). Only the
   front end (Scala) can turn id+offset into file:line, via `Document.Snapshot.find_command_position`.
   The user explicitly does NOT want positions from heap-precompiled theories (which already print
   as `(line N of "~~/src/HOL/Foo.thy")`) rewritten into the same format. Leave them as is.
2. The "project root" used to relativize agent-facing paths should be taken from the MCP client's
   declared roots (`roots/list`) when the client has the roots capability, falling back to the
   server process cwd otherwise. Measured facts: Claude Code 2.1.263 declares `roots: {listChanged:
   true}` and `roots/list` returns its project directory; Codex 0.153.2 declares no roots capability
   and returns an empty list; both start the stdio server with cwd = the agent's working directory.

Division of labour that was agreed: Scala resolves id+offset → absolute path + 1-based line (it is
the only side that can); Python relativizes with the pre-existing `relativize` helper (it already
relativizes every other agent-facing path, with the rule "absolute if outside the root").

## Constraints from the project (CLAUDE.md of the umbrella repo)
- Reuse code, never reinvent; elegance is a review criterion ("a shape that makes an invariant
  impossible to violate beats one that asks people to remember it"); no dirty hacks.
- Consistent terminology; no coined words.
- The Scala jar is a committed build artefact with `no_build = true`; it was rebuilt from a copy of
  the component per docs/COMPONENT_INSTALL_PLAN.md §7 and `scripts/check_component.py` passes.

## Verification already done
- 942 unit tests pass (12 new).
- End-to-end on a real HOL prover (script ai-artifacts/here-check/e2e_check.py, run from a
  different cwd with only ai-artifacts/here-check declared as the MCP root): output line is
  `[error] Undefined fact: "nonexistent_fact" Here_Check.thy:7`. Before the change the agent saw
  `[error] Undefined fact: "nonexistent_fact"` (the ⌂ was silently deleted by
  `_normalize_command_output_text`).
- Discovered during debugging: prover output reaches Scala already symbol-decoded
  (Pure/PIDE/prover.scala:256 `Symbol.decode_yxml_failsafe`), so the placeholder arrives as the
  literal `⌂`, not `\<^here>`; the comparison decodes both sides.

## Files changed
Scala: src/isabelle_mcp/scala/Isabelle2025-2/src/vscode_rendering.scala (is_here_placeholder,
resolve_here_positions), language_server.scala (call site in output_at_position).
Python: utils/core.py (relativize moved here from evaluation.py; new project_root_from_roots),
utils/__init__.py, utils/formatters.py (_display_position, position-span tracking in
_CommandOutputHTMLParser, parse_command_output_html gains project_root), tools/command_output.py,
evaluation.py, debugger.py, server.py (_adopt_client_roots, ProjectRootMiddleware,
_default_session_dirs now takes project_root).
Tests: tests/test_utils.py, tests/test_server.py. Docs: CHANGELOG.md, docs/TECH_NOTE.md,
docs/SPECIFICATION.md.

## Things a reviewer may want to know
- Other agent-facing channels still show/drop `⌂` unchanged: hover diagnostics (tools/hover.py uses
  LSP publishDiagnostics text, a plain-text channel) — out of scope by agreement.
- `Position.here` puts a single space before a printed location (" (line N of ...)") but none before
  the glued `\<^here>`. The resolver emits " <file>:<line>" with a leading space; the Python side
  preserves leading whitespace via the regex group.
- `_normalize_command_output_text` still deletes any remaining `⌂` (unresolvable placeholder) and
  collapses whitespace.
- The roots request is made once, at the first tool call, inside a FastMCP middleware
  (ProjectRootMiddleware), because roots can only be requested inside a session and the lifespan
  predates the session; isabelle_launch (normally the first call) derives its default -d dirs from
  project_root, so the adoption must precede it.

---

# ROUND 2 (re-review after the fix round)

Round 1's judge withheld acceptance on three small items and ruled seven concerns
"fix-autonomously" (see ROUND1_RULINGS.md in this directory; 20 concerns were DISMISSED there
with reasons — do not re-raise a dismissed concern unless you have NEW evidence the judge did
not consider). All fixes are now applied; the diff in change.diff is the CUMULATIVE working-tree
diff (round 1 + round 2). What round 2 changed:

1. ProjectRootMiddleware (server.py): `asyncio.Lock` + double-checked flag; the flag
   (`_adopted`) is set AFTER the roots/list round trip completes, so concurrent first calls all
   wait for the one request. Docstring rewritten with the real reason roots/list_changed is not
   tracked (print/parse round trip of root-relative references).
2. Integration test tests/integration/test_here_positions_e2e.py (real prover, modelled on
   test_blob_query_tools.py): asserts the raw PIDE/output_at_position HTML carries
   `<span class="position"> <abs path>:7</span>` and the tool message reads
   `Undefined fact: "nonexistent_fact" Here_Check.thy:7`. It PASSED on a real HOL prover (17 s).
   The scratch ai-artifacts/here-check/ directory was deleted.
3. tests/test_tools_command_output.py: a tool-level test pins the single production wiring line
   (`parse_command_output_html(content, client.project_root)`); a mutation check confirmed it
   fails when the argument is dropped.
4. tests/test_server.py: `test_adopts_the_clients_first_file_root_asking_once` (counting roots
   handler, asserts exactly one roots/list), `test_concurrent_first_calls_all_wait_for_the_adoption`
   (asyncio.gather of two calls with a 0.2 s roots handler; a mutation check confirmed it fails
   against the old body: ['/tmp', '/cwd']), `test_failing_roots_request_never_fails_the_tool_call`
   (Context.list_roots patched to raise). No timeout test, per the judge.
5. `relativize` move finished: tools/goal.py, tools/find_theorems.py, tools/command_status.py now
   import it from isabelle_mcp.utils; evaluation.py no longer acts as a re-export shim.

USER DECISION on the round-1 proposal (input/output path asymmetry): ACCEPTED, but as SILENT
compatibility only — every tool that takes a `file_path` now also accepts a project-root-relative
path, and the user explicitly does NOT want tool descriptions/docstrings, JSON schemas, or the
spec documents changed to advertise it. Implementation: new `resolve_path(path, root)` in
utils/core.py next to `relativize` (its inverse: relative → joined onto the root → realpath;
absolute → realpath; root None → realpath as before). Every server.py tool that takes file_path
(evaluate_to, hover, definition, local_occurrences, goal, find_theorems, command_output,
set_breakpoint, list_breakable_sites, and the optional-path list/enable_all/disable_all via a
tiny `_optional_path` helper) calls it after `_ensure_lsp_started` (which yields the client and
hence the root). debugger.py's private `_normalize_ref_path` (the one pre-existing place that
already accepted the relative form, for del_breakpoints) was replaced by the shared
`resolve_path`. Tests: TestResolvePath in tests/test_utils.py (incl. the round trip
`resolve_path(relativize(p, root), root) == p`) and a server-level test that isabelle_hover
accepts a bare file name when project_root is the file's directory.

Verification after round 2: 950 unit tests pass (was 942); the new integration test passes on a
real prover; scripts/check_component.py passes (the jar did not change in round 2); ruff reports
no new findings versus HEAD (the tree's pre-existing findings remain).

Open question the implementer wants the reviewers' opinion on (not a spec change): should the
silent relative-path acceptance get a CHANGELOG line? The user excluded "descriptions, JSON
schemas etc."; a changelog is developer-facing, not agent-facing, so it may or may not fall under
that exclusion. Flag it as discuss-with-user if you think it matters.

---

# ROUND 3 (verification of the round-2 fix-autonomously items)

Round 2's judge (ROUND2_RULINGS.md) closed all round-1 items and withheld acceptance on three
fix-autonomously items, now applied:

1. `isabelle_command_status` (server.py): positions are rebuilt after `_ensure_lsp_started` with
   `p.model_copy(update={"file_path": resolve_path(p.file_path, client.project_root)})`, exactly
   the judge's fix plan. Test: tests/test_server.py
   `test_command_status_positions_resolve_against_the_project_root` (patches
   `isabelle_mcp.server.command_status` to capture the positions the tool passes on; asserts the
   bare file name resolved to the realpath under project_root). models.py, docstrings and spec
   documents untouched.
2. tests/test_debugger.py `TestDelBreakpoints.test_relative_ref_resolves_against_the_project_root`,
   the judge's five-line test verbatim (plus `import os`).
3. server.py: `asyncio.wait_for(ctx.list_roots(), timeout=10)`.

Verification: 952 unit tests pass (was 950). Mutation check: a pytest plugin rebinding
`resolve_path` to plain `os.path.realpath` in both isabelle_mcp.server and isabelle_mcp.debugger
turns exactly three tests red — the two new ones above and the pre-existing hover one
(`3 failed, 949 passed`). ruff: 51 tree-wide, unchanged from HEAD. check_component.py: OK (jar
unchanged since round 1). The CHANGELOG was left as is, per the round-2 judge's answer to the
open question.
