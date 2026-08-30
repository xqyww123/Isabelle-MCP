---
name: isabelle-command-line
description: Working with the `isabelle` command line — locating key directories with
  `isabelle getenv` (ISABELLE_HOME, ISABELLE_HOME_USER, AFP), how sessions are declared
  in ROOT/ROOTS, registering a session directory permanently with `isabelle components -u`,
  and where to set environment variables so Isabelle actually reads them.
managed-by: isabelle-mcp
---

# Working with the `isabelle` command line

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
hardware). Avoid `-j N` (build N separate **sessions** at once).
`-o NAME=VAL` overrides any system option (`isabelle options -l` to list).

**Beware `-n`, `-c` and `-f`.** `-n` is not a dry run: it means *no build — take
existing session build databases*, so it reports on what is already built instead
of building anything. `-c` (clean build) and `-f` (fresh build) both discard heap
images that may have cost hours and rebuild everything selected; to make a single
session re-run, edit one of its source files instead.
