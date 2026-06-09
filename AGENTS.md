# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, …) that want to use the
**Isabelle LSP MCP server** in this project.

## Installing the MCP server

### 1. Install the package

The server ships as the `isabelle-mcp` command:

```sh
uv tool install isabelle-mcp      # or: pipx install isabelle-mcp
```

`uv tool` / `pipx` install the app into its own isolated environment and expose
the `isabelle-mcp` command on a stable global `PATH` — which matters because the
agent launches the server from *its own* environment, not your project venv.

Plain `pip` works only if the command still lands on a globally reachable `PATH`:

```sh
pip install --user isabelle-mcp   # command goes to ~/.local/bin
```

Note: `--user` shares one site-packages (weaker isolation, possible dependency
clashes) and may be blocked on externally-managed Pythons (PEP 668). A bare
`pip install` into a project venv will *not* work — the agent won't find the
command. Prefer `uv tool` / `pipx`.

Confirm it resolves: `command -v isabelle-mcp`.

### 2. Make the `isabelle` binary reachable

At runtime the server spawns `isabelle vscode_server`, so the `isabelle` binary
must be on `PATH` (check with `command -v isabelle`). If you use a non-global
Isabelle (e.g. a vendored `.../Isabelle2024/bin`), note that directory — the
registration step below can pin it into the server's environment.

### 3. Register the server with your agent

**One-shot script** (auto-detects whichever of `claude` / `codex` is installed):

```sh
scripts/install.sh
# pin a non-global Isabelle:
scripts/install.sh --isabelle-bin /path/to/Isabelle2024/bin
# target one client / rename the server:
scripts/install.sh --claude --name isabelle-lsp
```

It is idempotent (re-running re-registers cleanly) and uses an absolute path to
`isabelle-mcp` so the client need not share your `PATH`.

**Or register manually** — the two CLIs take the same `add NAME -- COMMAND` form:

```sh
# Claude Code (user scope; options go BEFORE the name)
claude mcp add -s user isabelle-lsp -- isabelle-mcp

# Codex (writes ~/.codex/config.toml)
codex mcp add isabelle-lsp -- isabelle-mcp
```

To pin a non-global Isabelle, add a `PATH` env var to the registration:

```sh
claude mcp add -s user -e PATH="/path/to/Isabelle2024/bin:$PATH" isabelle-lsp -- isabelle-mcp
codex  mcp add isabelle-lsp --env PATH="/path/to/Isabelle2024/bin:$PATH" -- isabelle-mcp
```

Verify with `claude mcp list` / `codex mcp` (or `/mcp` inside the agent), then
restart or reconnect the agent so it picks up the new server.

## Using it

The prover does **not** auto-start. Before any other tool, call
`isabelle_launch("HOL")` (or another session/logic) to start a session. The
server's own instructions (delivered at the MCP handshake) describe the full
workflow and the `isabelle` command-line tips.
