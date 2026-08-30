INSTRUCTIONS = """\
# Isabelle LSP MCP Server

You work by editing `.thy` or `.ML` files on disk and calling the MCP tools to
evaluate them and query the proof states. Changes to the files are synced and
re-evaluated automatically.

This tool is not meant to replace the `isabelle` command line — you should
still use commands like `isabelle getenv ISABELLE_HOME`
and `isabelle getenv AFP` to locate key directories.

Before any other tool, call `isabelle_launch(session)` to start a session.
The session only determines which theories come **precompiled** (the
session's heap image); Isabelle can still load any other theory dynamically — it is just slow.

You should NEVER check whether the session is built — `isabelle_launch` checks automatically.

When your call starts an evaluation, either by `isabelle_evaluate_to` or other query commands,
the call may not wait for the evaluation to finish, but may return earlier with the current
progress. You should keep polling `isabelle_evaluation_status` to watch it through:
it reports progress (per-theory percentage and command counts), any new errors,
and which commands are still running and for how long.

During an evaluation you can still query any line it has already evaluated.

A result is only reported `clean`/`complete` once the proofs up to your target have fully
checked — including forked proofs running in the background. If a result shows `running:` or
`pending:` line numbers, work is still in flight there: keep polling `isabelle_evaluation_status`
until those clear before trusting a clean verdict (a proof that ultimately fails surfaces its
error only when its fork joins).

Watch for a stuck evaluation — a bad edit can make a command loop forever.
A stuck command burns large amounts of CPU and can bog down the whole system, so
cancel it promptly: cancel with `isabelle_cancel_evaluation`, fix the command,
and evaluate again.
"""

# Claude Code truncates server instructions at 2048 characters. Everything that
# a tool description carries (session choice, positions, ASCII notation, the
# debugger vocabulary and workflow) lives in that tool's docstring in server.py;
# the `isabelle` command-line guidance is the isabelle-command-line skill
# shipped under skills/. tests/test_instructions.py pins the budget.


def get_instructions() -> str:
    return INSTRUCTIONS
