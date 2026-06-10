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


def _check_patches(skip: bool) -> bool:
    """Verify the my-better-isabelle-prover patches are applied.

    The server only works on an Isabelle carrying the my-better-isabelle-prover
    patches: it drives ``isabelle vscode_server`` through PIDE requests
    (PIDE/output_at_position, PIDE/cancel_execution, ...) that the stock build
    does not expose. Returns False to refuse the registration; the check is
    skipped only when ``isabelle`` itself is unreachable.
    """
    if skip:
        _eprint(
            "warn: --skip-patch-check given; the my-better-isabelle-prover patch check",
            "      was skipped here and will be skipped at every session launch too.",
            "      The server only works on a patched Isabelle.",
        )
        return True
    if shutil.which("isabelle") is None:
        _eprint(
            "warn: 'isabelle' is not on PATH; the server will fail to launch a session,",
            "      and the my-better-isabelle-prover patch check was skipped.",
            "      Re-run with --isabelle-bin /path/to/Isabelle/bin/isabelle to pin it.",
        )
        return True
    if shutil.which("my-better-isabelle") is None:
        _eprint(
            "error: 'my-better-isabelle' not found on PATH.",
            "  Isabelle-MCP requires the my-better-isabelle-prover patches. Install the",
            "  patch manager and apply them first:",
            "    pip install my-better-isabelle-prover   # or: uv tool / pipx install",
            "    my-better-isabelle patch",
        )
        return False
    proc = subprocess.run(
        ["my-better-isabelle", "-q", "status"],
        capture_output=True,
        text=True,
    )
    out = proc.stdout + proc.stderr
    indented = "\n".join("  " + line for line in out.splitlines())
    if "no patches available" in out:
        _eprint(
            "error: this Isabelle version is not supported by my-better-isabelle-prover:",
            indented,
            "  Isabelle-MCP needs its patches; point --isabelle-bin at a supported Isabelle.",
        )
        return False
    if proc.returncode != 0 or "[not-applied]" in out or "No patches found" in out:
        _eprint(
            "error: this Isabelle is missing the required my-better-isabelle-prover patches:",
            indented,
            "  Apply them with: my-better-isabelle patch",
        )
        return False
    print("✓ my-better-isabelle-prover patches applied")
    return True


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
        ".../Isabelle2024/bin/isabelle — same convention as my-better-isabelle; "
        "its directory is accepted too)",
    )
    parser.add_argument(
        "--claude", action="store_true", help="register only into Claude Code"
    )
    parser.add_argument("--codex", action="store_true", help="register only into Codex")
    parser.add_argument(
        "--skip-patch-check",
        action="store_true",
        help="register without verifying the my-better-isabelle-prover patches "
        "(for setups the patch manager cannot recognize)",
    )
    args = parser.parse_args(argv)

    cmd = _find_server_command()
    if cmd is None:
        _eprint(
            "error: 'isabelle-mcp' not found on PATH. Install it first, e.g.:",
            "  uv tool install isabelle-mcp      # or: pipx install isabelle-mcp",
        )
        return 1

    # Pin the Isabelle binary location into the server's environment when
    # requested. --isabelle-bin takes the isabelle binary itself (the same
    # convention as my-better-isabelle); a directory containing one is accepted.
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
                "       (pass the isabelle binary, e.g. /path/to/Isabelle2024/bin/isabelle)",
            )
            return 1
        path_env = f"{isa_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["PATH"] = path_env  # also lets the patch check below find `isabelle`

    if not _check_patches(args.skip_patch_check):
        return 1
    # With --skip-patch-check, also skip the server's own launch-time check.
    server_args = ["--skip-patch-check"] if args.skip_patch_check else []

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
    print('done. In your agent, call isabelle_launch("HOL") before any other tool.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
