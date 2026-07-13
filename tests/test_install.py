"""Tests for the `isabelle-mcp install` subcommand (isabelle_mcp.install)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from isabelle_mcp import install


def _fake_which(available):
    """A shutil.which stand-in resolving only the given names."""

    def which(name):
        return available.get(name)

    return which


class RecordingRun:
    """A subprocess.run stand-in recording calls and replaying canned results."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, cmd, **_kwargs):
        self.calls.append(cmd)
        key = tuple(cmd[:3])
        return self.results.get(key, SimpleNamespace(returncode=0, stdout="", stderr=""))


@pytest.fixture
def server_cmd(monkeypatch):
    """Make the isabelle-mcp command and claude resolvable on the fake PATH."""
    monkeypatch.setattr(
        shutil,
        "which",
        _fake_which({"isabelle-mcp": "/opt/tools/isabelle-mcp", "claude": "/usr/bin/claude"}),
    )


class TestFindServerCommand:
    def test_resolves_via_which(self, monkeypatch):
        monkeypatch.setattr(
            shutil, "which", _fake_which({"isabelle-mcp": "/opt/tools/isabelle-mcp"})
        )
        assert install._find_server_command() == "/opt/tools/isabelle-mcp"

    def test_missing_everywhere(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", _fake_which({}))
        monkeypatch.setattr(sys, "argv", ["isabelle-mcp"])
        assert install._find_server_command() is None


@pytest.fixture(autouse=True)
def _no_isabelle(monkeypatch):
    """`isabelle-mcp install` registers the Scala component; unit tests must not need Isabelle."""
    monkeypatch.setattr(
        install, "ensure_component",
        lambda: SimpleNamespace(path=Path("/fake/isabelle_mcp/scala/Isabelle2025-2")),
    )


@pytest.mark.usefixtures("server_cmd")
class TestMain:
    def test_registers_into_claude_with_absolute_path(self, monkeypatch, capsys):
        run = RecordingRun()
        monkeypatch.setattr(subprocess, "run", run)
        assert install.main(["--claude"]) == 0
        assert ["claude", "mcp", "remove", "isabelle-lsp", "-s", "user"] in run.calls
        assert [
            "claude", "mcp", "add", "-s", "user", "isabelle-lsp",
            "--", "/opt/tools/isabelle-mcp",
        ] in run.calls
        assert "registered 'isabelle-lsp' into Claude Code" in capsys.readouterr().out

    def test_auto_detects_clients(self, monkeypatch):
        monkeypatch.setattr(
            shutil,
            "which",
            _fake_which(
                {
                    "isabelle-mcp": "/opt/tools/isabelle-mcp",
                    "claude": "/usr/bin/claude",
                    "codex": "/usr/bin/codex",
                }
            ),
        )
        run = RecordingRun()
        monkeypatch.setattr(subprocess, "run", run)
        assert install.main([]) == 0
        adds = [c for c in run.calls if c[1:3] == ["mcp", "add"]]
        assert {c[0] for c in adds} == {"claude", "codex"}

    def test_registered_command_carries_no_extra_args(self, monkeypatch):
        run = RecordingRun()
        monkeypatch.setattr(subprocess, "run", run)
        assert install.main(["--claude"]) == 0
        add = next(c for c in run.calls if c[1:3] == ["mcp", "add"])
        assert add[-2:] == ["--", "/opt/tools/isabelle-mcp"]

    def test_custom_name(self, monkeypatch):
        run = RecordingRun()
        monkeypatch.setattr(subprocess, "run", run)
        assert install.main(["--claude", "--name", "my-isa"]) == 0
        assert ["claude", "mcp", "remove", "my-isa", "-s", "user"] in run.calls

    def test_isabelle_bin_pins_path(self, monkeypatch, tmp_path):
        # install() pins the Isabelle bin dir into this process's PATH so that everything after it
        # (ensure_component, and the registration itself) sees the requested Isabelle. That is a
        # real, wanted side effect — so the *test* has to contain it, or the stub `isabelle` below
        # leaks into every later test that resolves a real one.
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        isa = tmp_path / "isabelle"
        isa.write_text("#!/bin/sh\n")
        isa.chmod(0o755)
        run = RecordingRun()
        monkeypatch.setattr(subprocess, "run", run)
        assert install.main(["--claude", "--isabelle-bin", str(isa)]) == 0
        add = next(c for c in run.calls if c[1:3] == ["mcp", "add"])
        env_arg = add[add.index("-e") + 1]
        assert env_arg.startswith(f"PATH={tmp_path}")

    def test_isabelle_bin_rejects_bad_path(self, tmp_path, capsys):
        assert (
            install.main(
                ["--claude", "--isabelle-bin", str(tmp_path / "isabelle")]
            )
            == 1
        )
        assert "no executable 'isabelle'" in capsys.readouterr().err

    def test_failed_add_propagates_exit_code(self, monkeypatch):
        run = RecordingRun(
            {("claude", "mcp", "add"): SimpleNamespace(returncode=3, stdout="", stderr="")}
        )
        monkeypatch.setattr(subprocess, "run", run)
        with pytest.raises(SystemExit) as exc:
            install.main(["--claude"])
        assert exc.value.code == 3


class TestMainNoServer:
    def test_no_server_command(self, monkeypatch, capsys):
        monkeypatch.setattr(shutil, "which", _fake_which({}))
        monkeypatch.setattr(sys, "argv", ["isabelle-mcp"])
        assert install.main([]) == 1
        assert "'isabelle-mcp' not found on PATH" in capsys.readouterr().err

    def test_no_client_found(self, monkeypatch, capsys):
        monkeypatch.setattr(
            shutil, "which", _fake_which({"isabelle-mcp": "/opt/tools/isabelle-mcp"})
        )
        run = RecordingRun()
        monkeypatch.setattr(subprocess, "run", run)
        assert install.main([]) == 1
        assert "no target client found" in capsys.readouterr().err


class TestServerDispatch:
    def test_install_subcommand_dispatches(self, monkeypatch):
        from isabelle_mcp import server

        received = {}

        def fake_install_main(argv):
            received["argv"] = argv
            return 0

        def fail_run(*_args, **_kwargs):
            pytest.fail("must not spawn processes")

        monkeypatch.setattr(install, "main", fake_install_main)
        monkeypatch.setattr(subprocess, "run", fail_run)
        monkeypatch.setattr(sys, "argv", ["isabelle-mcp", "install", "--claude", "--name", "x"])
        with pytest.raises(SystemExit) as exc:
            server.main()
        assert exc.value.code == 0
        assert received["argv"] == ["--claude", "--name", "x"]
