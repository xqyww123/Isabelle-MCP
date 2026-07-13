"""Registration of the bundled Isabelle Scala component that provides ``isabelle mcp_server``.

The component is a package asset — ``isabelle_mcp/scala/<Isabelle identifier>/`` — and it is
registered with Isabelle **where it lies**, inside site-packages (or, for an editable install,
inside the source tree). Nothing is copied, and nothing is ever compiled on the user's machine:
the component ships a prebuilt jar and declares ``no_build = true``, so ``isabelle scala_build``
skips it entirely and its directory never has to be writable.

See ``docs/COMPONENT_INSTALL_PLAN.md`` for why, and for the four rules a component must obey to
avoid invalidating session heaps.

Cost model. :func:`ensure_component` runs before every ``isabelle mcp_server`` spawn, never at
import and never when the MCP server process boots. It splits into

  * a *stable* half — the ``isabelle`` binary, its identifier, ``ISABELLE_HOME_USER`` — which
    cannot change while we run, and is resolved once (~0.6 s of subprocesses);
  * a *volatile* half — the content of ``etc/components``, which any other install can rewrite at
    any moment — which is re-read on every call (one file read, tens of microseconds).

In the steady state that is a single read of a few hundred bytes: no subprocess, no JVM, no write.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path

from isabelle_mcp.utils import IsabelleToolError

logger = logging.getLogger(__name__)

# Lines in etc/components that are ours. The directory may be long gone (a deleted venv), so this
# has to be decided from the path text alone — we cannot look inside it.
_OURS = re.compile(r"/isabelle_mcp/scala/Isabelle[^/]*/?$")

_INSTALL_HINT = (
    "Install Isabelle-MCP against a specific Isabelle with:\n"
    "  isabelle-mcp install --isabelle-bin /path/to/Isabelle/bin/isabelle"
)


@dataclass(frozen=True)
class Component:
    """Everything about the component that cannot change while this process runs."""

    isabelle: str
    """The ``isabelle`` executable, as resolved on PATH."""

    identifier: str
    """``isabelle version``, e.g. ``Isabelle2025-2``. This is the key the asset is stored under."""

    path: Path
    """The component directory: ``isabelle_mcp/scala/<identifier>/``."""

    registry: Path
    """``$ISABELLE_HOME_USER/etc/components`` — the list Isabelle reads at every startup."""

    def line(self) -> str:
        return str(self.path)


def ensure_component() -> Component:
    """Make ``isabelle mcp_server`` resolve, and return the component. Idempotent.

    Raises :class:`IsabelleToolError` if Isabelle is unreachable, this Isabelle is unsupported, or
    the packaged component is incomplete.
    """
    c = _resolve()

    listed = _registered(c.registry)
    ours = [line for line in listed if _OURS.search(line)]
    if ours == [c.line()]:
        return c  # steady state: nothing to do

    for stale in (line for line in ours if line != c.line()):
        logger.info("dropping stale Isabelle-MCP component registration: %s", stale)
        _components(c, "-x", stale)
    _components(c, "-u", c.line())

    # `isabelle components -x` matches by path identity, so a line it cannot match (a symlinked
    # venv, a hand-edited spelling) survives silently — and would then make us take this slow path
    # on every single call. Say so once, loudly enough to be found, but do not fail: such a line is
    # inert to Isabelle beyond a warning on stderr.
    left = [line for line in _registered(c.registry) if _OURS.search(line) and line != c.line()]
    if left:
        logger.warning(
            "these Isabelle-MCP registrations could not be removed and will keep warning on every "
            "`isabelle` command; drop them by hand from %s: %s", c.registry, ", ".join(left)
        )

    logger.info("registered Isabelle-MCP component: %s", c.path)
    return c


def unregister_component() -> None:
    """Remove every Isabelle-MCP registration. The counterpart of ``ensure_component``."""
    c = _resolve()
    for line in _registered(c.registry):
        if _OURS.search(line):
            _components(c, "-x", line)
            print(f"✓ unregistered Isabelle component {line}")


# ── the stable half ─────────────────────────────────────────────────────────────

@cache
def _resolve() -> Component:
    isabelle = shutil.which("isabelle")
    if isabelle is None:
        raise IsabelleToolError(f"'isabelle' is not on PATH.\n{_INSTALL_HINT}")

    # `Path(x) / ""` is `Path(x)`, so an empty identifier would silently resolve to the *parent* of
    # every component and pass the is_dir() check below. Refuse it outright.
    identifier = _isabelle(isabelle, "version").strip()
    if not identifier:
        raise IsabelleToolError("'isabelle version' printed nothing; this Isabelle is not usable.")

    path = Path(str(files("isabelle_mcp"))) / "scala" / identifier
    if not path.is_dir():
        supported = sorted(p.name for p in path.parent.iterdir() if p.is_dir())
        raise IsabelleToolError(
            f"Isabelle-MCP does not support {identifier!r} "
            f"(supported: {', '.join(supported) or 'none'}).\n"
            "If you set ISABELLE_IDENTIFIER yourself, that is why."
        )

    # The component must ship its jar, and must declare no_build: a build-enabled component would
    # be compiled on the user's machine, inside our own `isabelle mcp_server` spawn, under the LSP
    # handshake deadline — and into site-packages, which may be read-only.
    if not (path / "lib" / "isabelle_mcp.jar").is_file():
        raise IsabelleToolError(
            f"The Isabelle-MCP component is incomplete: {path / 'lib' / 'isabelle_mcp.jar'} "
            "is missing. Reinstall the package."
        )
    if "no_build = true" not in (path / "etc" / "build.props").read_text():
        raise IsabelleToolError(
            f"The Isabelle-MCP component at {path} is build-enabled; it must declare "
            "'no_build = true'. This is a packaging bug — please report it."
        )

    home_user = Path(_isabelle(isabelle, "getenv", "-b", "ISABELLE_HOME_USER").strip())
    return Component(isabelle, identifier, path, home_user / "etc" / "components")


# ── the volatile half ───────────────────────────────────────────────────────────

def _registered(registry: Path) -> list[str]:
    """The component paths Isabelle currently knows about. Blank and '#' lines are not paths."""
    if not registry.is_file():
        return []
    lines = (line.strip() for line in registry.read_text().splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _components(c: Component, op: str, path: str) -> None:
    """``isabelle components -u|-x`` — Isabelle owns etc/components; we only ever ask it to edit."""
    proc = subprocess.run(
        [c.isabelle, "components", op, path], capture_output=True, text=True, timeout=300
    )
    if proc.returncode != 0:
        out = (proc.stdout + proc.stderr).strip()
        # `isabelle components` runs scala_build first, so it also fails when some *other* Scala
        # component on this machine is broken. Do not blame ours, and give the manual way out —
        # etc/components is a plain list of directories, one per line.
        raise IsabelleToolError(
            f"'isabelle components {op} {path}' failed:\n{out}\n\n"
            f"If this is an I/O error, {c.registry} is not writable. If it names another "
            f"component, that component is broken, not ours.\n"
            f"Either way you can edit {c.registry} by hand: it is one directory per line."
        )


def _isabelle(isabelle: str, *args: str) -> str:
    proc = subprocess.run(
        [isabelle, *args], capture_output=True, text=True, timeout=300
    )
    if proc.returncode != 0:
        raise IsabelleToolError(
            f"'isabelle {' '.join(args)}' failed:\n{(proc.stdout + proc.stderr).strip()}"
        )
    return proc.stdout
