#!/usr/bin/env python3
"""Release gate for the bundled Isabelle Scala component.

Three things can silently break a release, and none of them shows up on the author's machine:

  1. **`no_build = true` goes missing.** It is one line, and the release recipe temporarily removes
     it to rebuild the jar. Forget to keep it out of the shipped copy and Isabelle compiles the
     component on the *user's* machine — inside our own `isabelle mcp_server` spawn, under the LSP
     handshake deadline, and into `site-packages`, which may be read-only. Every property of the
     design rests on this line.

  2. **The jar goes stale.** With `no_build = true`, `isabelle scala_build` is a no-op: editing a
     `.scala` file and shipping changes *nothing*. The jar records a SHA1 of every source it was
     compiled from, so "is it current?" is a comparison — this script does it.

  3. **A distribution does not carry the component.** Two independent declarations put it there —
     `package-data` for the wheel, `MANIFEST.in` for the sdist — and both fail *silently*: you get
     a perfectly valid package with no component in it.

Run it before releasing, or let CI do it:

    python scripts/check_component.py             # the source tree
    python scripts/check_component.py dist/*      # and every artifact that ships
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENTS = ROOT / "src" / "isabelle_mcp" / "scala"
SERVICES = "isabelle.mcp.Tools"


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}")
    _fail.count += 1  # type: ignore[attr-defined]


_fail.count = 0  # type: ignore[attr-defined]


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def check_component(root: Path, name: str) -> None:
    """Everything that must hold of one `scala/<Isabelle identifier>/` directory."""
    print(f"\n{name}")

    props = (root / "etc" / "build.props").read_text()
    if "no_build = true" in props:
        _ok("build.props declares no_build = true (nothing is compiled on the user's machine)")
    else:
        _fail("build.props does NOT declare 'no_build = true' — the user's machine would compile "
              "the component, inside the mcp_server spawn, into a possibly read-only site-packages")

    jar_path = root / "lib" / "isabelle_mcp.jar"
    if not jar_path.is_file():
        _fail(f"missing {jar_path.relative_to(root)}")
        return

    try:
        jar = zipfile.ZipFile(jar_path)
    except zipfile.BadZipFile as exc:
        # A corrupt jar is the one way this design can still hurt the user: Classpath.services opens
        # every jar eagerly, so *every* Isabelle Scala tool would then exit non-zero.
        _fail(f"{jar_path.name} is not a readable zip ({exc})")
        return

    services = jar.read("META-INF/isabelle/services").decode().strip()
    if services == SERVICES:
        _ok(f"jar declares the service {SERVICES}")
    else:
        _fail(f"jar declares services {services!r}, expected {SERVICES!r} — `isabelle mcp_server` "
              "would not resolve")

    # The jar records a SHA1 of every source it was built from. With no_build = true nothing on the
    # user's machine will ever recompile it, so a stale jar ships silently.
    recorded = {
        line.split(" ", 1)[1]: line.split(" ", 1)[0]
        for line in jar.read("META-INF/isabelle/shasum").decode().splitlines()
        if line.split(" ", 1)[1].startswith("src/")
    }
    stale = [
        src for src, sha in recorded.items()
        if hashlib.sha1((root / src).read_bytes()).hexdigest() != sha
    ]
    if stale:
        _fail(f"the jar is stale — rebuilt sources not in it: {', '.join(sorted(stale))}\n"
              "    Rebuild per docs/COMPONENT_INSTALL_PLAN.md §7 (from a COPY with no_build removed).")
    else:
        _ok(f"jar matches all {len(recorded)} sources byte for byte")

    if not (root / "ML" / "mcp_prelude.ML").is_file():
        _fail("missing ML/mcp_prelude.ML — cancellation would silently do nothing")
    else:
        _ok("ML/mcp_prelude.ML present")


def check_dist(dist: Path) -> None:
    """The gates again, against what actually ships.

    A wheel and an sdist carry the component by two independent declarations — `package-data` and
    `MANIFEST.in` — so each is checked against the artifact itself, not against the one it was
    built beside. The sdist nests everything under `<name>-<version>/`, so the component is
    located rather than assumed.
    """
    print(f"\n{dist.name}")
    with tempfile.TemporaryDirectory() as tmp:
        if dist.suffix == ".whl":
            with zipfile.ZipFile(dist) as z:
                z.extractall(tmp)
        else:
            with tarfile.open(dist) as t:
                t.extractall(tmp, filter="data")

        roots = sorted(p for p in Path(tmp).glob("**/isabelle_mcp/scala/*") if p.is_dir())
        if not roots:
            _fail(f"{dist.name} carries no component at all "
                  "(the wheel gets it from package-data, the sdist from MANIFEST.in)")
            return
        for root in roots:
            check_component(root, f"  in {dist.name}: {root.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dists", nargs="*", type=Path,
                        help="built artifacts to check too (wheels and sdists)")
    args = parser.parse_args()

    print("Isabelle Scala component — release gate")
    for root in sorted(p for p in COMPONENTS.iterdir() if p.is_dir()):
        check_component(root, f"source tree: {root.name}")
    for dist in args.dists:
        check_dist(dist)

    n = _fail.count  # type: ignore[attr-defined]
    print(f"\n{'FAILED — ' + str(n) + ' problem(s)' if n else 'OK — safe to release'}")
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
