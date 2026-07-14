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

# The properties this gate knows how to check. Every one of them feeds the build, and with
# no_build = true nothing will ever recompile the jar — so a property we do not model is a property
# that can drift away from the jar in silence. An unknown one fails: teach this script about it.
KNOWN = {"title", "module", "no_build", "requirements", "sources", "services"}


# Deliberately duplicated from isabelle_mcp.component.build_props rather than imported: the CI gate
# runs `python scripts/check_component.py` on a bare interpreter with nothing installed, and
# isabelle_mcp/__init__.py pulls in fastmcp.
def props(path: Path) -> dict[str, str]:
    """etc/build.props exactly as Isabelle reads it — `java.util.Properties`, via
    `isabelle.setup.Build.component_context`.

    A value keeps its **trailing** whitespace, and `Build.get_bool` accepts nothing but the exact
    strings "true"/"false". So `no_build = true ` — one invisible space, on the very line §7.1 has
    you delete and retype by hand — is not `true`: it aborts *every* `isabelle` command on the
    user's machine, `isabelle components -x` included. Hence lstrip, never strip.

    Escapes are deliberately not decoded. Java's unescaping only ever *removes* backslashes, so a
    value we read as "true" is a value Java reads as "true": we can be wrong only in the
    fail-loudly direction, never in the wave-it-through one.
    """
    parsed: dict[str, str] = {}
    lines = iter(path.read_text().splitlines())
    for line in lines:
        line = line.lstrip()
        if not line or line[0] in "#!":     # a comment never continues onto the next line
            continue
        while line.endswith("\\"):          # continuation: joined with no separator of its own
            line = line[:-1] + next(lines, "").lstrip()
        i = next((n for n, c in enumerate(line) if c in "=: \t"), len(line))
        key, rest = line[:i], line[i:].lstrip(" \t")
        parsed[key] = rest[1:].lstrip(" \t") if rest[:1] in ("=", ":") else rest
    return parsed


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}")
    _fail.count += 1  # type: ignore[attr-defined]


_fail.count = 0  # type: ignore[attr-defined]


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def check_component(root: Path, name: str) -> None:
    """Everything that must hold of one `scala/<Isabelle identifier>/` directory."""
    print(f"\n{name}")

    p = props(root / "etc" / "build.props")

    unknown = sorted(set(p) - KNOWN)
    if unknown:
        _fail(f"build.props declares properties this gate does not model: {', '.join(unknown)}. "
              "They feed the build, and nothing will ever rebuild the jar — teach this script about "
              "them, or the jar can silently stop matching them.")

    no_build = p.get("no_build")
    if no_build == "true":
        _ok("build.props declares no_build = true (nothing is compiled on the user's machine)")
    elif no_build is None:
        _fail("build.props does NOT declare 'no_build = true' — the user's machine would compile "
              "the component, inside the mcp_server spawn, into a possibly read-only site-packages")
    else:
        _fail(f"build.props declares no_build = {no_build!r}. Isabelle reads this file with "
              "java.util.Properties and Build.get_bool takes nothing but the exact string 'true' — "
              "a stray trailing space is enough to abort EVERY isabelle command on the user's "
              "machine with '*** Bad boolean property', `isabelle components -x` included.")

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

    services = jar.read("META-INF/isabelle/services").decode().split()
    if services == p.get("services", "").split():
        _ok(f"jar declares the services build.props does ({' '.join(services)})")
    else:
        _fail(f"jar declares services {services}, build.props declares "
              f"{p.get('services', '').split()} — the jar's is what `isabelle mcp_server` resolves")

    # Isabelle hashes <meta_info>, every requirement and every source into META-INF/isabelle/shasum.
    # With no_build = true nothing will ever recompile the jar, so a stale one ships in silence —
    # and the jar's own record is the only witness. Compare it to what build.props declares TODAY:
    # checking only the entries the jar already lists would be blind to a source since added.
    recorded: dict[str, str] = {}
    for entry in jar.read("META-INF/isabelle/shasum").decode().splitlines():
        sha, name = entry.split(" ", 1)
        recorded[name] = sha

    sources = p.get("sources", "").split()
    declared = {"<meta_info>", *p.get("requirements", "").split(), *sources}

    absent = [s for s in sources if not (root / s).is_file()]
    if absent:
        _fail(f"build.props declares sources that are not there: {', '.join(absent)}")
    elif declared != set(recorded):
        _fail("the jar was built from a different build.props than the one shipped here.\n"
              f"    only in build.props: {', '.join(sorted(declared - set(recorded))) or '—'}\n"
              f"    only in the jar:     {', '.join(sorted(set(recorded) - declared)) or '—'}\n"
              "    Rebuild per docs/COMPONENT_INSTALL_PLAN.md §7 (from a COPY with no_build removed).")
    elif stale := [s for s in sources
                   if hashlib.sha1((root / s).read_bytes()).hexdigest() != recorded[s]]:
        _fail(f"the jar is stale — sources edited since it was built: {', '.join(sorted(stale))}\n"
              "    Rebuild per docs/COMPONENT_INSTALL_PLAN.md §7 (from a COPY with no_build removed).")
    else:
        _ok(f"jar matches all {len(sources)} declared sources byte for byte")

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
