#!/usr/bin/env bash
# Register the isabelle-mcp server with Claude Code and/or Codex.
#
# Usage:
#   scripts/install.sh [--name NAME] [--isabelle-bin BIN] [--claude] [--codex]
#                      [--skip-patch-check]
#
#   --name NAME          MCP server name to register (default: isabelle-lsp)
#   --isabelle-bin BIN   the isabelle binary to pin into the server's PATH
#                        (e.g. .../Isabelle2024/bin/isabelle — same convention
#                        as my-better-isabelle; its directory is accepted too)
#   --claude / --codex   target only that client; with neither, registers into
#                        whichever of `claude` / `codex` is on PATH.
#   --skip-patch-check   register without verifying the my-better-isabelle-prover
#                        patches, and pass --skip-patch-check to the server so its
#                        own launch-time check is skipped too (for setups the
#                        patch manager cannot recognize)
#
# Prerequisites:
#   - the `isabelle-mcp` command must already be installed, e.g.
#       uv tool install isabelle-mcp      # or: pipx install isabelle-mcp
#   - Isabelle must carry the my-better-isabelle-prover patches; this script
#     verifies that (when `isabelle` is reachable and --skip-patch-check is not
#     given) and refuses to register otherwise:
#       pip install my-better-isabelle-prover
#       my-better-isabelle patch
set -euo pipefail

NAME=isabelle-lsp
ISABELLE_BIN=""
DO_CLAUDE=0
DO_CODEX=0
EXPLICIT=0
SKIP_PATCH_CHECK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --name)         NAME="$2"; shift 2;;
    --isabelle-bin) ISABELLE_BIN="$2"; shift 2;;
    --claude)       DO_CLAUDE=1; EXPLICIT=1; shift;;
    --codex)        DO_CODEX=1;  EXPLICIT=1; shift;;
    --skip-patch-check) SKIP_PATCH_CHECK=1; shift;;
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
# --isabelle-bin takes the isabelle binary itself (the same convention as
# my-better-isabelle); a directory containing one is accepted too.
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
    echo "       (pass the isabelle binary, e.g. /path/to/Isabelle2024/bin/isabelle)" >&2
    exit 1
  fi
  PATH="$ISA_DIR:$PATH"   # also lets the patch check below find `isabelle`
  CLAUDE_ENV=(-e "PATH=$PATH")
  CODEX_ENV=(--env "PATH=$PATH")
fi

# The server only works on an Isabelle carrying the my-better-isabelle-prover
# patches: it drives `isabelle vscode_server` through PIDE requests
# (PIDE/output_at_position, PIDE/cancel_execution, ...) that the stock build
# does not expose. Refuse to register against an unpatched Isabelle; the check
# is skipped only when `isabelle` itself is unreachable.
SERVER_ARGS=()
if [ "$SKIP_PATCH_CHECK" -eq 1 ]; then
  echo "warn: --skip-patch-check given; the my-better-isabelle-prover patch check" >&2
  echo "      was skipped here and will be skipped at every session launch too." >&2
  echo "      The server only works on a patched Isabelle." >&2
  SERVER_ARGS+=(--skip-patch-check)
elif ! command -v isabelle >/dev/null 2>&1; then
  echo "warn: 'isabelle' is not on PATH; the server will fail to launch a session," >&2
  echo "      and the my-better-isabelle-prover patch check was skipped." >&2
  echo "      Re-run with --isabelle-bin /path/to/Isabelle/bin/isabelle to pin it." >&2
elif ! command -v my-better-isabelle >/dev/null 2>&1; then
  echo "error: 'my-better-isabelle' not found on PATH." >&2
  echo "  Isabelle-MCP requires the my-better-isabelle-prover patches. Install the" >&2
  echo "  patch manager and apply them first:" >&2
  echo "    pip install my-better-isabelle-prover   # or: uv tool / pipx install" >&2
  echo "    my-better-isabelle patch" >&2
  exit 1
else
  STATUS_OUT="$(my-better-isabelle -q status 2>&1)" && STATUS_RC=0 || STATUS_RC=$?
  if printf '%s\n' "$STATUS_OUT" | grep -q 'no patches available'; then
    echo "error: this Isabelle version is not supported by my-better-isabelle-prover:" >&2
    printf '%s\n' "$STATUS_OUT" | sed 's/^/  /' >&2
    echo "  Isabelle-MCP needs its patches; point --isabelle-bin at a supported Isabelle." >&2
    exit 1
  fi
  if [ "$STATUS_RC" -ne 0 ] \
     || printf '%s\n' "$STATUS_OUT" | grep -qF -e '[not-applied]' -e 'No patches found'; then
    echo "error: this Isabelle is missing the required my-better-isabelle-prover patches:" >&2
    printf '%s\n' "$STATUS_OUT" | sed 's/^/  /' >&2
    echo "  Apply them with: my-better-isabelle patch" >&2
    exit 1
  fi
  echo "✓ my-better-isabelle-prover patches applied"
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
    claude mcp add -s user "${CLAUDE_ENV[@]+"${CLAUDE_ENV[@]}"}" "$NAME" -- "$CMD" "${SERVER_ARGS[@]+"${SERVER_ARGS[@]}"}"
    echo "✓ registered '$NAME' into Claude Code (user scope)"
    did=1
  else
    echo "warn: --claude given but 'claude' is not on PATH" >&2
  fi
fi
if [ "$DO_CODEX" -eq 1 ]; then
  if command -v codex >/dev/null 2>&1; then
    codex mcp remove "$NAME" >/dev/null 2>&1 || true            # idempotent
    codex mcp add "$NAME" "${CODEX_ENV[@]+"${CODEX_ENV[@]}"}" -- "$CMD" "${SERVER_ARGS[@]+"${SERVER_ARGS[@]}"}"
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
echo "done. In your agent, call isabelle_launch(\"HOL\") before any other tool."
