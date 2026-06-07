INSTRUCTIONS = """\
# Isabelle LSP MCP Server

This server drives **one shared Isabelle session** through Isabelle's LSP
backend. It is meant to be driven by a **single agent, sequentially** —
concurrent or interleaved calls corrupt this shared state.

## Evaluation model

You work by editing `.thy` files on disk and calling the MCP tools to
evaluate them and query the proof states. Changes to the files are synced and
re-evaluated automatically.

When your call starts an evaluation, either by `isabelle_evaluate_to` or other query commands,
the call may not wait for the evaluation to finish, but may return earlier with the current
progress. You should poll `isabelle_evaluation_status` to monitor the status.
It reports any new errors since your last check, how far each theory is
checked (a percentage and command counts), which commands are still running and
for how long, and whether it has finished. The evaluation does not always reach
the desired destination — it can get stuck (a bad edit can make a command loop
forever; the sign is a command whose elapsed time keeps climbing while the
percentage stops moving). If so, terminate it with `isabelle_cancel_evaluation`,
fix the command, and evaluate again. Cancelling keeps everything already checked.

**Errors do not stop the checking.** Isabelle checks every command up to your
target even when an earlier one fails. A failed command reports a diagnostic at
its location; commands that use its result usually fail too, each with its own
diagnostic, while independent commands are still checked normally. So
`isabelle_evaluate_to` still reaches your target line when there are errors before
it — you get those errors back as diagnostics, not a halt.

**Edits take effect on your next call.** After you change a file, nothing happens
on its own. Your next tool call re-reads the file from disk and re-checks the
affected part; it is not re-checked in the background between calls.

## Conventions

- Positions are **1-indexed**; file paths must be **absolute**.

## Recommended workflow

1. `isabelle_evaluate_to` — evaluate the file to the region of interest.
2. Follow progress with `isabelle_evaluation_status`; if a command gets stuck,
   cancel and fix it (see above) rather than waiting for `complete`.
3. `isabelle_goal` — use extensively during proof development (omit
   `after_text` to see a tactic's before/after effect).
4. `isabelle_diagnostics` — check for errors in a range.
5. `isabelle_hover` + `isabelle_definition` — understand symbols.
6. Modify files with your editor, then re-evaluate with
   `isabelle_evaluate_to`.
"""


def get_instructions() -> str:
    return INSTRUCTIONS
