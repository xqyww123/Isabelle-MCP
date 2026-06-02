INSTRUCTIONS = """\
# Isabelle LSP MCP Server

## Evaluation model

Isabelle processes theory files incrementally.  Before querying results
(hover, goals, diagnostics, etc.) you must **evaluate** the file up to
the line of interest.

### Evaluation tools (3)

1. **isabelle_evaluate_to(file_path, line)** — start evaluating up to a
   target line.  Returns within ~10 s with errors found so far.  If
   evaluation is still running, call ``evaluation_status`` to poll.
2. **isabelle_evaluation_status()** — check progress of an ongoing
   evaluation.  Returns new errors since the last check and current
   execution position.  Call repeatedly until status is ``complete``.
3. **isabelle_cancel_evaluation()** — cancel an ongoing evaluation.

### Query tools (7)

Query tools return results instantly when the target line has been
evaluated.  If the line has not been evaluated yet and no evaluation is
running, they auto-start evaluation.  If an evaluation is already
running, they fail with a message to call ``evaluation_status``.

4. **isabelle_hover** — type info and documentation for symbol at position
5. **isabelle_definition** — jump to symbol definition
6. **isabelle_local_occurrences** — all in-file occurrences (definition +
   uses) of a locally-defined entity; current file only
7. **isabelle_diagnostics** — errors, warnings for a line range
8. **isabelle_goal** — proof goals at position; omit column for
   before/after diff
9. **isabelle_command_output** — prover messages for a command
10. **isabelle_session_info** — current session name

## Key conventions

- All positions are **1-indexed** (line 1, column 1 = first character).
- Always use **absolute paths**.
- ``evaluate_to`` supports negative line indices: ``-1`` = last line.

## Recommended workflow

1. **isabelle_evaluate_to** — evaluate the file to the region of interest.
2. Poll **isabelle_evaluation_status** until ``status == "complete"``.
3. **isabelle_goal** — use extensively during proof development (omit
   column to see tactic effect).
4. **isabelle_diagnostics** — check for errors in a range.
5. **isabelle_hover** + **isabelle_definition** — understand symbols.
6. Modify files with your editor, then re-evaluate with ``evaluate_to``.

## Session configuration

Default session is **HOL**.  Override via ``ISABELLE_SESSION`` env var
before starting the MCP server.

## Configuration

- ``ISA_LSP_EVAL_POLL_INTERVAL`` env var controls the poll timeout in
  seconds (default ``10``).
"""


def get_instructions() -> str:
    return INSTRUCTIONS
