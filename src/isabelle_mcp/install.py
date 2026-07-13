"""Register the isabelle-mcp server with Claude Code and/or Codex.

Python port of ``scripts/install.sh`` exposed as ``isabelle-mcp install`` so the
registration works from any pip/pipx/uv installation, without a checkout of the
repository.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys


def _eprint(*lines: str) -> None:
    for line in lines:
        print(line, file=sys.stderr)


from isabelle_mcp.component import ensure_component, unregister_component
from isabelle_mcp.utils import IsabelleToolError


def _find_server_command() -> str | None:
    """Locate the installed ``isabelle-mcp`` command as an absolute path.

    The absolute path is registered (rather than the bare name) so the
    registration does not depend on the client inheriting the same PATH.
    """
    cmd = shutil.which("isabelle-mcp")
    if cmd:
        return os.path.abspath(cmd)
    # Fallback: the console script we are running as (covers `pipx run` and
    # venvs whose bin dir is not on the parent shell's PATH).
    argv0 = sys.argv[0] or ""
    if (
        os.path.basename(argv0).startswith("isabelle-mcp")
        and os.path.isfile(argv0)
        and os.access(argv0, os.X_OK)
    ):
        return os.path.abspath(argv0)
    return None


def _run_add(add_cmd: list[str], client: str) -> None:
    proc = subprocess.run(add_cmd)
    if proc.returncode != 0:
        _eprint(f"error: '{client} mcp add' failed with exit code {proc.returncode}")
        raise SystemExit(proc.returncode)


def _register_claude(
    name: str, cmd: str, path_env: str | None, server_args: list[str]
) -> bool:
    if shutil.which("claude") is None:
        _eprint("warn: --claude given but 'claude' is not on PATH")
        return False
    subprocess.run(  # idempotent
        ["claude", "mcp", "remove", name, "-s", "user"], capture_output=True
    )
    env_args = ["-e", f"PATH={path_env}"] if path_env else []
    _run_add(
        ["claude", "mcp", "add", "-s", "user", *env_args, name, "--", cmd, *server_args],
        "claude",
    )
    print(f"✓ registered '{name}' into Claude Code (user scope)")
    return True


def _register_codex(
    name: str, cmd: str, path_env: str | None, server_args: list[str]
) -> bool:
    if shutil.which("codex") is None:
        _eprint("warn: --codex given but 'codex' is not on PATH")
        return False
    subprocess.run(  # idempotent
        ["codex", "mcp", "remove", name], capture_output=True
    )
    env_args = ["--env", f"PATH={path_env}"] if path_env else []
    _run_add(["codex", "mcp", "add", name, *env_args, "--", cmd, *server_args], "codex")
    print(f"✓ registered '{name}' into Codex")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="isabelle-mcp install",
        description="Register the isabelle-mcp server with Claude Code and/or Codex.",
    )
    parser.add_argument(
        "--name",
        default="isabelle-lsp",
        help="MCP server name to register (default: %(default)s)",
    )
    parser.add_argument(
        "--isabelle-bin",
        default="",
        metavar="BIN",
        help="the isabelle binary to pin into the server's PATH (e.g. "
        ".../Isabelle2025-2/bin/isabelle; its directory is accepted too)",
    )
    parser.add_argument(
        "--claude", action="store_true", help="register only into Claude Code"
    )
    parser.add_argument("--codex", action="store_true", help="register only into Codex")
    args = parser.parse_args(argv)

    cmd = _find_server_command()
    if cmd is None:
        _eprint(
            "error: 'isabelle-mcp' not found on PATH. Install it first, e.g.:",
            "  uv tool install isabelle-mcp      # or: pipx install isabelle-mcp",
        )
        return 1

    # Pin the Isabelle binary location into the server's environment when requested.
    # --isabelle-bin takes the isabelle binary itself; a directory containing one is accepted.
    path_env: str | None = None
    if args.isabelle_bin:
        if os.path.isdir(args.isabelle_bin):
            isa_dir = args.isabelle_bin
        else:
            isa_dir = os.path.dirname(args.isabelle_bin)
        isa = os.path.join(isa_dir, "isabelle")
        if not (os.path.isfile(isa) and os.access(isa, os.X_OK)):
            _eprint(
                f"error: --isabelle-bin: no executable 'isabelle' at {isa}",
                "       (pass the isabelle binary, e.g. /path/to/Isabelle2025-2/bin/isabelle)",
            )
            return 1
        path_env = f"{isa_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["PATH"] = path_env  # also lets ensure_component() below find `isabelle`

    # Register the bundled Scala component that provides `isabelle mcp_server`. The server does
    # this itself before every launch too — doing it here as well means an install that cannot
    # work fails now, in front of a human, rather than at the first isabelle_launch.
    try:
        component = ensure_component()
    except IsabelleToolError as exc:
        _eprint(f"error: {exc}")
        return 1
    print(f"✓ Isabelle component registered: {component.path}")

    server_args: list[str] = []

    # Default target: whichever client is installed.
    do_claude, do_codex = args.claude, args.codex
    if not do_claude and not do_codex:
        do_claude = shutil.which("claude") is not None
        do_codex = shutil.which("codex") is not None

    did = False
    if do_claude:
        did = _register_claude(args.name, cmd, path_env, server_args) or did
    if do_codex:
        did = _register_codex(args.name, cmd, path_env, server_args) or did
    if not did:
        _eprint(
            "error: no target client found. Install Claude Code or Codex, "
            "or pass --claude / --codex."
        )
        return 1
    print(
        "done. In your agent, call isabelle_launch(session=...) before any other tool"
        " — pick the session that fits the work."
    )
    return 0


def uninstall_main(argv: list[str] | None = None) -> int:
    """Undo `isabelle-mcp install`: drop the Isabelle component registration.

    `pip uninstall` cannot run hooks, so the registration would otherwise outlive the package —
    harmless (Isabelle ignores a directory that is gone) but noisy: it warns on stderr of every
    `isabelle` command until it is removed.
    """
    parser = argparse.ArgumentParser(
        prog="isabelle-mcp uninstall",
        description="Remove the Isabelle component registration made by `isabelle-mcp install`.",
    )
    parser.parse_args(argv)
    try:
        unregister_component()
    except IsabelleToolError as exc:
        _eprint(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
