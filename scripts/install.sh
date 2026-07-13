#!/usr/bin/env bash
# Register the isabelle-mcp server with Claude Code and/or Codex.
#
# Usage:
#   scripts/install.sh [--name NAME] [--isabelle-bin BIN] [--claude] [--codex]
#
#   --name NAME          MCP server name to register (default: isabelle-lsp)
#   --isabelle-bin BIN   the isabelle binary to pin into the server's PATH
#                        (e.g. .../Isabelle2025-2/bin/isabelle; its directory is
#                        accepted too)
#   --claude / --codex   target only that client; with neither, registers into
#                        whichever of `claude` / `codex` is on PATH.
#
# Prerequisites:
#   - the `isabelle-mcp` command must already be installed, e.g.
#       uv tool install isabelle-mcp      # or: pipx install isabelle-mcp
#   - Isabelle2025-2. No patches: the server ships its own Isabelle Scala
#     component (`isabelle mcp_server`) and registers it on first launch.
#
# This is the shell equivalent of `isabelle-mcp install`, which also registers
# the Scala component up front. Prefer that; this script exists for setups where
# only the shell is available.

set -euo pipefail

NAME=isabelle-lsp
ISABELLE_BIN=""
DO_CLAUDE=0
DO_CODEX=0
EXPLICIT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --name)         NAME="$2"; shift 2;;
    --isabelle-bin) ISABELLE_BIN="$2"; shift 2;;
    --claude)       DO_CLAUDE=1; EXPLICIT=1; shift;;
    --codex)        DO_CODEX=1;  EXPLICIT=1; shift;;
    -h|--help)      awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "$0"; exit 0;;
    *)              echo "unknown argument: $1 (try --help)" >&2; exit 2;;
  esac
done

# Locate the installed server command (use an absolute path so the registration
# does not depend on the client inheriting the same PATH).
CMD="$(command -v isabelle-mcp || true)"
if [ -z "$CMD" ]; then
  echo "error: 'isabelle-mcp' not found on PATH. Install it first, e.g.:" >&2
  echo "  uv tool install isabelle-mcp      # or: pipx install isabelle-mcp" >&2
  exit 1
fi

# Pin the Isabelle binary location into the server's environment when requested.
# --isabelle-bin takes the isabelle binary itself; a directory containing one is
# accepted too.
CLAUDE_ENV=()
CODEX_ENV=()
if [ -n "$ISABELLE_BIN" ]; then
  if [ -d "$ISABELLE_BIN" ]; then
    ISA_DIR="$ISABELLE_BIN"
  else
    ISA_DIR="$(dirname "$ISABELLE_BIN")"
  fi
  if [ ! -x "$ISA_DIR/isabelle" ]; then
    echo "error: --isabelle-bin: no executable 'isabelle' at $ISA_DIR/isabelle" >&2
    echo "       (pass the isabelle binary, e.g. /path/to/Isabelle2025-2/bin/isabelle)" >&2
    exit 1
  fi
  PATH="$ISA_DIR:$PATH"
  CLAUDE_ENV=(-e "PATH=$PATH")
  CODEX_ENV=(--env "PATH=$PATH")
fi

if ! command -v isabelle >/dev/null 2>&1; then
  echo "warn: 'isabelle' is not on PATH; the server will fail to launch a session." >&2
  echo "      Re-run with --isabelle-bin /path/to/Isabelle/bin/isabelle to pin it." >&2
fi

# Default target: whichever client is installed.
if [ "$EXPLICIT" -eq 0 ]; then
  command -v claude >/dev/null 2>&1 && DO_CLAUDE=1
  command -v codex  >/dev/null 2>&1 && DO_CODEX=1
fi

did=0
if [ "$DO_CLAUDE" -eq 1 ]; then
  if command -v claude >/dev/null 2>&1; then
    claude mcp remove "$NAME" -s user >/dev/null 2>&1 || true   # idempotent
    claude mcp add -s user "${CLAUDE_ENV[@]+"${CLAUDE_ENV[@]}"}" "$NAME" -- "$CMD"
    echo "✓ registered '$NAME' into Claude Code (user scope)"
    did=1
  else
    echo "warn: --claude given but 'claude' is not on PATH" >&2
  fi
fi
if [ "$DO_CODEX" -eq 1 ]; then
  if command -v codex >/dev/null 2>&1; then
    codex mcp remove "$NAME" >/dev/null 2>&1 || true            # idempotent
    codex mcp add "$NAME" "${CODEX_ENV[@]+"${CODEX_ENV[@]}"}" -- "$CMD"
    echo "✓ registered '$NAME' into Codex"
    did=1
  else
    echo "warn: --codex given but 'codex' is not on PATH" >&2
  fi
fi

if [ "$did" -eq 0 ]; then
  echo "error: no target client found. Install Claude Code or Codex, or pass --claude / --codex." >&2
  exit 1
fi
echo "done. In your agent, call isabelle_launch(session=...) before any other tool — pick the session that fits the work."
