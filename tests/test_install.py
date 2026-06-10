"""Tests for the `isabelle-mcp install` subcommand (isabelle_mcp.install)."""

import shutil
import subprocess
import sys
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


class TestPatchCheck:
    def test_skip_flag_passes(self, capsys):
        assert install._check_patches(True) is True
        assert "--skip-patch-check" in capsys.readouterr().err

    def test_no_isabelle_warns_and_passes(self, monkeypatch, capsys):
        monkeypatch.setattr(shutil, "which", _fake_which({}))
        assert install._check_patches(False) is True
        assert "'isabelle' is not on PATH" in capsys.readouterr().err

    def test_no_patch_manager_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(
            shutil, "which", _fake_which({"isabelle": "/opt/Isabelle/bin/isabelle"})
        )
        assert install._check_patches(False) is False
        assert "my-better-isabelle" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "returncode,output,ok",
        [
            (0, "patch-a [applied]\npatch-b [applied]\n", True),
            (0, "patch-a [applied]\npatch-b [not-applied]\n", False),
            (1, "boom\n", False),
            (0, "no patches available for Isabelle2023\n", False),
            (0, "No patches found\n", False),
        ],
    )
    def test_status_output(self, monkeypatch, returncode, output, ok):
        monkeypatch.setattr(
            shutil,
            "which",
            _fake_which(
                {
                    "isabelle": "/opt/Isabelle/bin/isabelle",
                    "my-better-isabelle": "/usr/bin/my-better-isabelle",
                }
            ),
        )
        run = RecordingRun(
            {
                ("my-better-isabelle", "-q", "status"): SimpleNamespace(
                    returncode=returncode, stdout=output, stderr=""
                )
            }
        )
        monkeypatch.setattr(subprocess, "run", run)
        assert install._check_patches(False) is ok
        assert run.calls == [["my-better-isabelle", "-q", "status"]]


@pytest.mark.usefixtures("server_cmd")
class TestMain:
    def test_registers_into_claude_with_absolute_path(self, monkeypatch, capsys):
        run = RecordingRun()
        monkeypatch.setattr(subprocess, "run", run)
        assert install.main(["--claude", "--skip-patch-check"]) == 0
        assert ["claude", "mcp", "remove", "isabelle-lsp", "-s", "user"] in run.calls
        # --skip-patch-check is also passed through to the registered server command
        assert [
            "claude", "mcp", "add", "-s", "user", "isabelle-lsp",
            "--", "/opt/tools/isabelle-mcp", "--skip-patch-check",
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
        assert install.main(["--skip-patch-check"]) == 0
        adds = [c for c in run.calls if c[1:3] == ["mcp", "add"]]
        assert {c[0] for c in adds} == {"claude", "codex"}

    def test_no_skip_flag_means_plain_server_command(self, monkeypatch):
        # patch check passes via the "isabelle not on PATH" branch (fake PATH
        # has no isabelle), and the registered command carries no extra args
        run = RecordingRun()
        monkeypatch.setattr(subprocess, "run", run)
        assert install.main(["--claude"]) == 0
        add = next(c for c in run.calls if c[1:3] == ["mcp", "add"])
        assert add[-2:] == ["--", "/opt/tools/isabelle-mcp"]

    def test_custom_name(self, monkeypatch):
        run = RecordingRun()
        monkeypatch.setattr(subprocess, "run", run)
        assert install.main(["--claude", "--skip-patch-check", "--name", "my-isa"]) == 0
        assert ["claude", "mcp", "remove", "my-isa", "-s", "user"] in run.calls

    def test_isabelle_bin_pins_path(self, monkeypatch, tmp_path):
        isa = tmp_path / "isabelle"
        isa.write_text("#!/bin/sh\n")
        isa.chmod(0o755)
        run = RecordingRun()
        monkeypatch.setattr(subprocess, "run", run)
        assert install.main(["--claude", "--skip-patch-check", "--isabelle-bin", str(isa)]) == 0
        add = next(c for c in run.calls if c[1:3] == ["mcp", "add"])
        env_arg = add[add.index("-e") + 1]
        assert env_arg.startswith(f"PATH={tmp_path}")

    def test_isabelle_bin_rejects_bad_path(self, tmp_path, capsys):
        assert (
            install.main(
                ["--claude", "--skip-patch-check", "--isabelle-bin", str(tmp_path / "isabelle")]
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
            install.main(["--claude", "--skip-patch-check"])
        assert exc.value.code == 3


class TestMainNoServer:
    def test_no_server_command(self, monkeypatch, capsys):
        monkeypatch.setattr(shutil, "which", _fake_which({}))
        monkeypatch.setattr(sys, "argv", ["isabelle-mcp"])
        assert install.main(["--skip-patch-check"]) == 1
        assert "'isabelle-mcp' not found on PATH" in capsys.readouterr().err

    def test_no_client_found(self, monkeypatch, capsys):
        monkeypatch.setattr(
            shutil, "which", _fake_which({"isabelle-mcp": "/opt/tools/isabelle-mcp"})
        )
        run = RecordingRun()
        monkeypatch.setattr(subprocess, "run", run)
        assert install.main(["--skip-patch-check"]) == 1
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
