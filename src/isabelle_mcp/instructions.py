INSTRUCTIONS = """\
# Isabelle LSP MCP Server

You work by editing `.thy` or `.ML` files on disk and calling the MCP tools to
evaluate them and query the proof states. Changes to the files are synced and
re-evaluated automatically.

This tool is not meant to fully replace the `isabelle` command line — you are
still strongly encouraged to use commands like `isabelle getenv ISABELLE_HOME`
and `isabelle getenv AFP` to locate key directories.

Before any other tool, call `isabelle_launch()` to start a session (defaults
to "Main"). The session only determines which theories come **precompiled**
(the session's heap image); Isabelle can still load any other theory
dynamically — it is just slow, because the theory and all its imports are
checked from source. "Main" precompiles rather little, so substantial imports
load slowly under it: pick the session that best fits the actual work (e.g.
"HOL-Analysis" for analysis; a project's own session via `session_dirs`,
building its heap first with `isabelle build -b SESSION` if needed). Ask the
user which session/logic to use if unsure.

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

## Working with the `isabelle` command line

**Locate key directories.** `isabelle getenv NAME` prints `NAME=value` (several
names allowed):
- `ISABELLE_HOME` — the distribution (read-only install).
- `ISABELLE_HOME_USER` — your per-user dir; all config below lives here.
- `AFP` — the AFP `thys` dir (only if AFP is registered as a component).

**Sessions & components.** A session is declared in a `ROOT` file
(`session NAME = parent + theories …`); a `ROOTS` file lists subdirectories to
recurse into. To make a session directory permanently discoverable (no `-d`
needed), register it: `isabelle components -u /abs/dir` appends it to
`$ISABELLE_HOME_USER/etc/components` (one path per line, `#` comments out;
`-x DIR` removes, `isabelle components -l` lists). A registered directory
contributes its `ROOT`/`ROOTS` and its own `etc/settings`.

**Environment variables.** Isabelle does not reliably read environment variables
from the calling shell. Set them persistently in
`$ISABELLE_HOME_USER/etc/settings` (a bash-sourced file: `VAR=value` lines), or
in a component's own `etc/settings`.

**Building.** `isabelle build -b SESSION` builds a session's heap image; `-d DIR`
adds a session directory, `-v` is verbose. For parallelism use `-o threads=N` —
it gives the prover N worker **threads inside** the session (0 = guess from
hardware), e.g. `isabelle build -o threads=8 -b HOL`. Avoid `-j N` (build N
separate **sessions** at once): it multiplies memory use and is rarely what you
want here. `-o NAME=VAL` overrides any system option (`isabelle options -l` to
list).
"""


def get_instructions() -> str:
    return INSTRUCTIONS
