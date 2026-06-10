# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, …) that want to use the
**Isabelle LSP MCP server** in this project.

## Installing the MCP server

### 1. Patch Isabelle first (REQUIRED)

**Isabelle-MCP does not work on a stock Isabelle.** The server drives
`isabelle vscode_server` through PIDE LSP requests (`PIDE/output_at_position`,
`PIDE/cancel_execution`, …) that only exist after applying the
[my-better-isabelle-prover](https://github.com/xqyww123/my_better_isabelle_prover)
patches. You MUST install and apply them before anything else:

```sh
pip install my-better-isabelle-prover
my-better-isabelle patch          # apply patches + rebuild the Scala components
my-better-isabelle status         # verify: every patch reports "applied"
```

Compatibility notes:

- Needs Python ≥ 3.12 (the package has no other dependencies). If `pip` is not
  the right Python, use `python3 -m pip install my-better-isabelle-prover`.
- On externally-managed Pythons (PEP 668: Debian/Ubuntu system Python refuses
  `pip install`), install into a venv, or use `uv tool` instead.
  Unlike the MCP server below, a project venv is fine here: `my-better-isabelle`
  only runs from *your* shell (manually and via `isabelle-mcp install`), so it
  just has to be on your `PATH` at that moment — no globally reachable install
  is required.
- `my-better-isabelle` needs the `isabelle` binary to detect the version: put
  it on `PATH`, or pass `--isabelle-bin /path/to/Isabelle/bin/isabelle` (the
  binary itself; `isabelle-mcp install --isabelle-bin` takes the same form). The
  system `patch` command must also be installed.

`isabelle-mcp install` (step 4 below) checks this (when `isabelle` is reachable;
`--skip-patch-check` overrides) and refuses to register the server against an
unpatched Isabelle.

The server also re-checks **at run time**: every `isabelle_launch` verifies the
patches before spawning `isabelle vscode_server` and refuses to start an
unpatched Isabelle, with instructions to run `my-better-isabelle patch`. This
check uses the server's own bundled copy of the patch manager (a declared
dependency, invoked via `python -m`), so it works regardless of where — or
whether — `my-better-isabelle` is on the server's `PATH`; only `isabelle`
itself must be reachable. For hand-patched setups the patch manager cannot
recognize, start the server with `isabelle-mcp --skip-patch-check`
(`isabelle-mcp install --skip-patch-check` registers it that way automatically).

### 2. Install the package

The server ships as the `isabelle-mcp` command on PyPI; install it with pip or uv:

```sh
pip install isabelle-mcp          # or: uv tool install isabelle-mcp
```

The command must land on a globally reachable `PATH` — the agent launches the
server from *its own* environment, not your project venv. `uv tool install`
guarantees this: it installs the app into its own isolated environment and
exposes `isabelle-mcp` on a stable global `PATH`.

Plain `pip` works only if the command still lands on a globally reachable `PATH`:

```sh
pip install --user isabelle-mcp   # command goes to ~/.local/bin
```

Note: `--user` shares one site-packages (weaker isolation, possible dependency
clashes) and may be blocked on externally-managed Pythons (PEP 668). A bare
`pip install` into a project venv will *not* work — the agent won't find the
command. When in doubt, prefer `uv tool install`.

Confirm it resolves: `command -v isabelle-mcp`.

### 3. Make the `isabelle` binary reachable

At runtime the server spawns `isabelle vscode_server`, so the `isabelle` binary
must be on `PATH` (check with `command -v isabelle`). If you use a non-global
Isabelle (e.g. a vendored `.../Isabelle2024/bin/isabelle`), note that binary —
the registration step below can pin it into the server's environment.

### 4. Register the server with your agent

**One-shot command** (auto-detects whichever of `claude` / `codex` is installed):

```sh
isabelle-mcp install
# pin a non-global Isabelle:
isabelle-mcp install --isabelle-bin /path/to/Isabelle2024/bin/isabelle
# target one client / rename the server:
isabelle-mcp install --claude --name isabelle-lsp
```

It is idempotent (re-running re-registers cleanly) and registers an absolute
path to `isabelle-mcp` so the client need not share your `PATH`. It also
verifies the my-better-isabelle-prover patches (step 1) and aborts with
instructions if they are not applied (skip with `--skip-patch-check`).
`scripts/install.sh` in a repo checkout does the same thing.

**Or register manually** — the two CLIs take the same `add NAME -- COMMAND` form:

```sh
# Claude Code (user scope; options go BEFORE the name)
claude mcp add -s user isabelle-lsp -- isabelle-mcp

# Codex (writes ~/.codex/config.toml)
codex mcp add isabelle-lsp -- isabelle-mcp
```

## Using it

The prover does **not** auto-start. Before any other tool, call
`isabelle_launch("HOL")` (or another session/logic) to start a session. The
server's own instructions (delivered at the MCP handshake) describe the full
workflow and the `isabelle` command-line tips.
