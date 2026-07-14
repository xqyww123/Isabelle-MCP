"""Tests for isabelle_mcp.component — registration of the bundled Isabelle Scala component.

This is the code that edits the user's Isabelle configuration, so it gets the closest scrutiny:
a stale registration silently *shadows* the right one (`Isabelle_Tool.find` is a `collectFirst`),
and a dangling one makes Isabelle warn on the stderr of every command, forever.

The shape tests are pure. The rest drive a real `isabelle` against a scratch ``USER_HOME``, so
they never touch the user's own ``~/.isabelle``, and they need no prover.
"""

import shutil
import subprocess

import pytest

from isabelle_mcp.component import (
    _OURS,
    _registered,
    _resolve,
    build_props,
    ensure_component,
    unregister_component,
)


class TestBuildProps:
    """build.props must be read the way Isabelle reads it: java.util.Properties.

    Checked against the real JVM's `Properties.load` — these cases all agree with it. The trailing
    whitespace one is why this parser exists at all: `no_build = true ` looks identical to
    `no_build = true`, but `Build.get_bool` matches the exact string and aborts on anything else, so
    a component carrying it makes EVERY isabelle command on the machine exit 2 — `isabelle
    components -x`, the cure, included. A `.strip()` here would wave it through.
    """

    def _props(self, tmp_path, text):
        f = tmp_path / "build.props"
        f.write_text(text)
        return build_props(f)

    def test_trailing_whitespace_is_part_of_the_value(self, tmp_path):
        assert self._props(tmp_path, "no_build = true \n")["no_build"] == "true "
        assert self._props(tmp_path, "no_build = true\t\n")["no_build"] == "true\t"

    def test_leading_whitespace_is_not(self, tmp_path):
        assert self._props(tmp_path, "   no_build =    true\n")["no_build"] == "true"

    @pytest.mark.parametrize("line", ["no_build=true", "no_build:true", "no_build true"])
    def test_every_properties_separator(self, tmp_path, line):
        assert self._props(tmp_path, line + "\n")["no_build"] == "true"

    @pytest.mark.parametrize("hash_", ["#", "!"])
    def test_comments(self, tmp_path, hash_):
        assert "no_build" not in self._props(tmp_path, f"{hash_} no_build = true\n")

    def test_continuation_joins_with_no_separator_of_its_own(self, tmp_path):
        # The space before the backslash is kept; the indent after it is not.
        assert self._props(tmp_path, "sources = a.scala \\\n    b.scala\n")["sources"] == \
            "a.scala b.scala"


class TestShape:
    """Which lines of etc/components are ours.

    It has to be decided from the path text alone: by the time we want to prune a dead venv, the
    directory is gone and there is nothing left to inspect.
    """

    @pytest.mark.parametrize("line", [
        "/home/u/p/.venv/lib/python3.13/site-packages/isabelle_mcp/scala/Isabelle2025-2",
        "/opt/x/isabelle_mcp/scala/Isabelle2024",
        "/home/u/proj/src/isabelle_mcp/scala/Isabelle2025-2/",   # editable install, trailing slash
    ])
    def test_ours(self, line):
        assert _OURS.search(line)

    @pytest.mark.parametrize("line", [
        "/home/u/MLML/contrib/Semantic_Embedding",
        "/home/u/MLML/contrib/afp-2026-05-13/thys",
        "/opt/isabelle_mcp/scala",                    # no identifier
        "/opt/isabelle_mcp/Isabelle2025-2",           # no scala/
        "/mnt/usb/somebody_elses_component",          # an unmounted volume: never touch it
    ])
    def test_not_ours(self, line):
        assert not _OURS.search(line)


class TestRegistryReading:
    def test_blank_and_comment_lines_are_not_paths(self, tmp_path):
        reg = tmp_path / "components"
        reg.write_text("# a comment\n\n/opt/one\n  /opt/two  \n")
        assert _registered(reg) == ["/opt/one", "/opt/two"]

    def test_absent_registry(self, tmp_path):
        assert _registered(tmp_path / "nope") == []


@pytest.mark.integration
class TestRegistration:
    """End to end against a real `isabelle`, in a scratch USER_HOME."""

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("USER_HOME", str(tmp_path))
        _resolve.cache_clear()
        yield tmp_path
        _resolve.cache_clear()

    @staticmethod
    def _isabelle(*args):
        return subprocess.run(["isabelle", *args], capture_output=True, text=True)

    def test_fresh_user(self, home):
        # A brand-new Isabelle user: no etc/components at all.
        assert "Unknown Isabelle tool" in self._isabelle("mcp_server", "-X").stderr

        c = ensure_component()

        # Assert on OUTPUT, not on rc: `isabelle mcp_server -X` exits 1 (illegal option) even
        # though the tool resolved perfectly.
        out = self._isabelle("mcp_server", "-X")
        assert "Usage: isabelle mcp_server" in out.stdout
        assert "Unknown Isabelle tool" not in out.stderr
        assert c.line() in c.registry.read_text()

    def test_fast_path_is_inert(self, home, monkeypatch):
        c = ensure_component()
        before = c.registry.read_bytes()

        # The steady state must cost one file read: another install can evict us at any moment,
        # so we re-check before every spawn — that is only affordable if it spawns nothing.
        def no_subprocess(*a, **k):
            pytest.fail("the fast path must not run a subprocess")

        monkeypatch.setattr(subprocess, "run", no_subprocess)
        assert ensure_component() == c
        assert c.registry.read_bytes() == before

    def test_scala_build_never_touches_the_component(self, home):
        # The load-bearing property of `no_build = true`: nothing is ever compiled on the user's
        # machine, so site-packages may be read-only (sudo pip, Docker, Nix).
        #
        # Deliberately NOT `scala_build -f`: -f rebuilds *every* component, including
        # $ISABELLE_HOME's own isabelle.jar, which a scratch USER_HOME does not isolate. The
        # "-f cannot touch us either" property is proven by Build.java instead (build() returns
        # when module_result() is "" — before `fresh` is read).
        c = ensure_component()
        jar = c.path / "lib" / "isabelle_mcp.jar"
        mtime = jar.stat().st_mtime

        out = self._isabelle("scala_build")

        assert out.returncode == 0
        assert "Isabelle-MCP" not in out.stdout      # no `### Building …` banner
        assert jar.stat().st_mtime == mtime

    def test_prunes_a_dead_venv(self, home):
        dead = home / "old/.venv/lib/python3.13/site-packages/isabelle_mcp/scala/Isabelle2025-2"
        (dead / "etc").mkdir(parents=True)
        (dead / "etc/settings").write_text("")
        assert self._isabelle("components", "-u", str(dead)).returncode == 0

        # The venv is deleted without `isabelle-mcp uninstall` — the ordinary case.
        shutil.rmtree(home / "old")

        c = ensure_component()

        assert str(dead) not in c.registry.read_text()
        # A dangling entry is not silent: Isabelle warns on the stderr of every command.
        assert "Missing Isabelle component" not in self._isabelle("version").stderr

    def test_foreign_entries_survive(self, home):
        c = ensure_component()
        foreign = "/mnt/unmounted/somebody_elses_component"   # not of our shape; may be legitimate
        c.registry.write_text(c.registry.read_text().rstrip("\n") + f"\n{foreign}\n")

        ensure_component()

        assert foreign in c.registry.read_text()

    def test_uninstall_removes_only_ours(self, home):
        c = ensure_component()
        foreign = "/mnt/unmounted/somebody_elses_component"
        c.registry.write_text(c.registry.read_text().rstrip("\n") + f"\n{foreign}\n")

        unregister_component()

        text = c.registry.read_text()
        assert c.line() not in text
        assert foreign in text
