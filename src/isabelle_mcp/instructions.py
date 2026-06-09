INSTRUCTIONS = """\
# Isabelle LSP MCP Server

You work by editing `.thy` or `.ML` files on disk and calling the MCP tools to
evaluate them and query the proof states. Changes to the files are synced and
re-evaluated automatically.

Before any other tool, call `isabelle_launch(session)` to start a session
(e.g. "Main"); ask the user which session/logic to use if unsure.

When your call starts an evaluation, either by `isabelle_evaluate_to` or other query commands,
the call may not wait for the evaluation to finish, but may return earlier with the current
progress. You must keep polling `isabelle_evaluation_status` to watch it through:
it reports progress (per-theory percentage and command counts), any new errors,
and which commands are still running and for how long.

Watch for a stuck evaluation — a bad edit can make a command loop forever.
A stuck command burns large amounts of CPU and can bog down the whole system, so
cancel it promptly: cancel with `isabelle_cancel_evaluation`, fix the command,
and evaluate again. Cancelling keeps everything already checked.

**Errors do not stop the checking.** Isabelle checks every command up to your
target even when an earlier one fails, unless some command gets stuck. A failed
command reports a diagnostic at its location. So `isabelle_evaluate_to` still
reaches your target line when there are errors before it — you get those errors
back as diagnostics, not a halt.

## Conventions

- Positions are **1-indexed**; file paths must be **absolute**.
"""


def get_instructions() -> str:
    return INSTRUCTIONS
