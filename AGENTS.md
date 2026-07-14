# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, …) that want to use the
**Isabelle LSP MCP server** in this project.

## Installing the MCP server

### 1. Isabelle (REQUIRED — but no patching)

**Isabelle-MCP needs Isabelle2025-2, and nothing else.** It used to require a patched
Isabelle: the server drove the stock `isabelle vscode_server`, which lacks the PIDE
requests it needs (`PIDE/output_at_position`, `PIDE/cancel_execution`, …), so the
distribution had to be patched by
[my-better-isabelle-prover](https://github.com/xqyww123/my_better_isabelle_prover).

**That is no longer true.** Isabelle-MCP ships its own Isabelle Scala component,
`isabelle mcp_server`, as a package asset, and registers it with Isabelle before the
first session launch. The component carries a prebuilt jar and declares
`no_build = true`, so:

- nothing is compiled on the user's machine — `site-packages` may be read-only
  (`sudo pip install`, Docker, Nix all work);
- **no session heap is invalidated** — patching `src/Pure/**.ML` used to force a rebuild
  of Pure, HOL and every AFP session on the machine;
- there is no second package to install.

Requirements:

- **Isabelle2025-2.** Isabelle2024 is no longer supported (the fork is cut from 2025-2's
  VSCode sources; three of its files do not exist in 2024). The last supporting commit is
  tagged `last-isabelle2024-support` in both repositories.
- `isabelle` on `PATH`, or pinned with
  `isabelle-mcp install --isabelle-bin /path/to/Isabelle/bin/isabelle` (the binary itself);
  that pins the directory into the registered server's `PATH`.

`isabelle-mcp uninstall` removes the component registration. Removing the package without
it leaves a dangling entry: Isabelle then prints `### Missing Isabelle component: …` on the
stderr of every `isabelle` command (exit code stays 0) until you run
`isabelle components -x <the path it names>`.

Design and rationale: `docs/COMPONENT_INSTALL_PLAN.md`; the cancellation mechanism (an ML
prelude injected at prover startup, built from the public `EXECUTION` API) is in
`src/isabelle_mcp/scala/Isabelle2025-2/docs/CANCELLATION.md`.

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

If both clients are installed, it registers into both; pass `--claude` /
`--codex` to target just one.
It is idempotent (re-running re-registers cleanly) and registers an absolute
path to `isabelle-mcp` so the client need not share your `PATH`. It also registers the Isabelle
Scala component up front, so an install that cannot work fails there, in front of a
human, rather than at the first `isabelle_launch`.
`scripts/install.sh` in a repo checkout does the same thing.

**Or register manually** — the two CLIs take the same `add NAME -- COMMAND` form:

```sh
# Claude Code (user scope; options go BEFORE the name)
claude mcp add -s user isabelle-lsp -- isabelle-mcp

# Codex (writes ~/.codex/config.toml)
codex mcp add isabelle-lsp -- isabelle-mcp
```

Verify with `claude mcp list` / `codex mcp list`, then restart or reconnect the
agent so it picks up the new server.

## Using it

The prover does **not** auto-start. Before any other tool, call
`isabelle_launch(...)` with the session/logic that fits the work (bare
`Main` is only a minimal fallback) to start a session. The
server's own instructions (delivered at the MCP handshake) describe the full
workflow and the `isabelle` command-line tips.

## Releasing it

**Do not `uv publish`.** A release is a pushed `vX.Y.Z` tag — see
[CONTRIBUTING.md § Releasing](CONTRIBUTING.md#releasing).
