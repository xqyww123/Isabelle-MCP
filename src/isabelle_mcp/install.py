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
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path


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


_SKILL_MARKER = "managed-by: isabelle-mcp"


def _bundled_skills() -> list[Path]:
    """The ``SKILL.md`` files shipped as package data, one per skill directory."""
    root = Path(str(files("isabelle_mcp"))) / "skills"
    return sorted(p / "SKILL.md" for p in root.iterdir() if (p / "SKILL.md").is_file())


def _skill_is_managed(target: Path) -> bool:
    """True when the installed copy still carries the managed-by marker.

    The marker is the ownership test: a copy whose marker is gone was edited by
    the user, and neither install nor uninstall touches it again.
    """
    text = target.read_text(encoding="utf-8")
    return any(line.strip() == _SKILL_MARKER for line in text.splitlines())


def _skill_targets(claude: bool, codex: bool) -> Iterator[tuple[Path, Path, str, str]]:
    """Yield (source, target, display_dir, name) per bundled skill × selected client."""
    dirs = []
    if claude:
        dirs.append((Path.home() / ".claude" / "skills", "~/.claude/skills"))
    if codex:
        dirs.append((Path.home() / ".codex" / "skills", "~/.codex/skills"))
    for skills_dir, display in dirs:
        for source in _bundled_skills():
            name = source.parent.name
            yield source, skills_dir / name / "SKILL.md", display, name


def _warn_unmanaged(display: str, name: str) -> None:
    _eprint(
        f"warn: left {display}/{name}/SKILL.md alone: it has been edited "
        f"(its '{_SKILL_MARKER}' marker is gone)"
    )


def _install_skills(claude: bool, codex: bool) -> None:
    """Copy the bundled skills into the selected clients' user-level skill dirs.

    A managed copy is overwritten (an upgrade); a hand-edited one is left alone.
    """
    for source, target, display, name in _skill_targets(claude, codex):
        if target.exists() and not _skill_is_managed(target):
            _warn_unmanaged(display, name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"✓ installed skill '{name}' into {display}")


def _uninstall_skills() -> None:
    """Remove the installed skills from both clients; a hand-edited copy is kept."""
    for _source, target, display, name in _skill_targets(claude=True, codex=True):
        if not target.exists():
            continue
        if not _skill_is_managed(target):
            _warn_unmanaged(display, name)
            continue
        target.unlink()
        try:
            target.parent.rmdir()  # keep the dir if the user put other files in it
        except OSError:
            pass
        print(f"✓ removed skill '{name}' from {display}")


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
        description="Register the isabelle-mcp server with Claude Code and/or Codex, "
        "and install its bundled skills.",
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
    parser.add_argument(
        "--no-skills",
        action="store_true",
        help="do not install the bundled skills into ~/.claude/skills / ~/.codex/skills",
    )
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

    did_claude = do_claude and _register_claude(args.name, cmd, path_env, server_args)
    did_codex = do_codex and _register_codex(args.name, cmd, path_env, server_args)
    if not (did_claude or did_codex):
        _eprint(
            "error: no target client found. Install Claude Code or Codex, "
            "or pass --claude / --codex."
        )
        return 1
    # The bundled skills follow the same client selection as the registration.
    if not args.no_skills:
        _install_skills(did_claude, did_codex)
    print(
        "done. In your agent, call isabelle_launch(session=...) before any other tool"
        " — pick the session that fits the work."
    )
    return 0


def uninstall_main(argv: list[str] | None = None) -> int:
    """Undo `isabelle-mcp install`: remove the skills, drop the component registration.

    `pip uninstall` cannot run hooks, so the registration would otherwise outlive the package —
    harmless (Isabelle ignores a directory that is gone) but noisy: it warns on stderr of every
    `isabelle` command until it is removed.
    """
    parser = argparse.ArgumentParser(
        prog="isabelle-mcp uninstall",
        description="Remove the skills and the Isabelle component registration "
        "installed by `isabelle-mcp install`.",
    )
    parser.parse_args(argv)
    # Skills first: the local file removal must not be blocked by a failing
    # component unregistration (which needs a working `isabelle` on PATH).
    _uninstall_skills()
    try:
        unregister_component()
    except IsabelleToolError as exc:
        _eprint(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
