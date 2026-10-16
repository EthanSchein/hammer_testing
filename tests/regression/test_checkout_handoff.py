import os
import sys

import pytest

from hammer.shell import sledgehammer_cli


def _fake_venv(bindir, airflow=True):
    bindir.mkdir(parents=True)
    bindir.chmod(0o755)
    for name in ("sledgehammer", "airflow") if airflow else ("sledgehammer",):
        (bindir / name).write_text("#!/bin/sh\n")
        (bindir / name).chmod(0o755)


def _fake_checkout(root, venv=True, launcher=True, airflow=True):
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    if launcher:
        (root / "scripts").mkdir()
        (root / "scripts" / "airflow-standalone-ldap.py").write_text("")
    if venv:
        _fake_venv(root / ".venv" / "bin", airflow)
    return root


def _confine_lookup(tmp_path, monkeypatch):
    """Keep the checkout lookup blind to real directories above tmp_path."""
    real = sledgehammer_cli._is_checkout
    root = os.path.realpath(tmp_path) + os.sep
    monkeypatch.setattr(sledgehammer_cli, "_is_checkout", lambda p: p.startswith(root) and real(p))


class _Execd(Exception):
    pass


@pytest.fixture
def hand_off(tmp_path, monkeypatch):
    """A cwd outside any checkout, HOME in tmp_path, the lookup confined to tmp_path, os.execv captured instead of run."""
    monkeypatch.setenv(sledgehammer_cli.DELEGATED_ENV, "")
    monkeypatch.delenv(sledgehammer_cli.DELEGATED_ENV)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _confine_lookup(tmp_path, monkeypatch)
    (tmp_path / "outside").mkdir()
    monkeypatch.chdir(tmp_path / "outside")
    monkeypatch.setattr(sledgehammer_cli.sys, "argv", ["sledgehammer", "syn", "-t", "Top"])
    calls = []

    def fake_execv(path, argv):
        calls.append((path, argv, os.environ.get(sledgehammer_cli.DELEGATED_ENV)))
        raise _Execd()

    monkeypatch.setattr(sledgehammer_cli.os, "execv", fake_execv)
    return calls


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executables and execv")
class TestDelegation:
    real_hand_off = True

    def test_reexecs_into_checkout_venv_from_another_python(self, tmp_path, monkeypatch, hand_off) -> None:
        repo = _fake_checkout(tmp_path / "repo")
        monkeypatch.setattr(sledgehammer_cli, "REPO", str(repo))
        monkeypatch.setattr(sledgehammer_cli.sys, "prefix", str(tmp_path / "conda"))
        with pytest.raises(_Execd):
            sledgehammer_cli._delegate_to_checkout_venv()
        exe = os.path.join(str(repo), ".venv", "bin", "sledgehammer")
        assert hand_off == [(exe, [exe, "syn", "-t", "Top"], "1")]

    def test_no_reexec_when_already_on_checkout_venv(self, tmp_path, monkeypatch, hand_off) -> None:
        repo = _fake_checkout(tmp_path / "repo")
        monkeypatch.setattr(sledgehammer_cli, "REPO", str(repo))
        monkeypatch.setattr(sledgehammer_cli.sys, "prefix", str(repo / ".venv"))
        sledgehammer_cli._delegate_to_checkout_venv()
        assert hand_off == []

    def test_no_reexec_when_the_venv_is_a_symlink_to_sys_prefix(self, tmp_path, monkeypatch, hand_off) -> None:
        repo = _fake_checkout(tmp_path / "repo", venv=False)
        _fake_venv(tmp_path / "venvs" / "repo" / "bin")
        (repo / ".venv").symlink_to(tmp_path / "venvs" / "repo")
        monkeypatch.setattr(sledgehammer_cli, "REPO", str(repo))
        monkeypatch.setattr(sledgehammer_cli.sys, "prefix", str(tmp_path / "venvs" / "repo"))
        sledgehammer_cli._delegate_to_checkout_venv()
        assert hand_off == []

    def test_marker_prevents_a_second_reexec_and_is_cleared(self, tmp_path, monkeypatch, hand_off) -> None:
        monkeypatch.setattr(sledgehammer_cli, "REPO", str(_fake_checkout(tmp_path / "repo")))
        monkeypatch.setattr(sledgehammer_cli.sys, "prefix", str(tmp_path / "conda"))
        monkeypatch.setenv(sledgehammer_cli.DELEGATED_ENV, "1")
        sledgehammer_cli._delegate_to_checkout_venv()
        assert hand_off == []
        assert sledgehammer_cli.DELEGATED_ENV not in os.environ

    def test_no_reexec_when_checkout_has_no_venv(self, tmp_path, monkeypatch, hand_off) -> None:
        monkeypatch.setattr(sledgehammer_cli, "REPO", str(_fake_checkout(tmp_path / "repo", venv=False)))
        monkeypatch.setattr(sledgehammer_cli.sys, "prefix", str(tmp_path / "conda"))
        sledgehammer_cli._delegate_to_checkout_venv()
        assert hand_off == []

    def test_execv_failure_falls_back_in_process(self, tmp_path, monkeypatch, hand_off, capsys) -> None:
        monkeypatch.setattr(sledgehammer_cli, "REPO", str(_fake_checkout(tmp_path / "repo")))
        monkeypatch.setattr(sledgehammer_cli.sys, "prefix", str(tmp_path / "conda"))

        def broken_execv(path, argv):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(sledgehammer_cli.os, "execv", broken_execv)
        sledgehammer_cli._delegate_to_checkout_venv()
        assert sledgehammer_cli.DELEGATED_ENV not in os.environ
        assert "could not hand off" in capsys.readouterr().err

    def test_hands_off_to_the_checkout_the_cwd_is_in(self, tmp_path, monkeypatch, hand_off) -> None:
        linked = _fake_checkout(tmp_path / "linked")
        cy = _fake_checkout(tmp_path / "cy" / "vlsi" / "hammer")
        (tmp_path / "cy" / "vlsi" / "build" / "x").mkdir(parents=True)
        monkeypatch.chdir(tmp_path / "cy" / "vlsi" / "build" / "x")
        monkeypatch.setattr(sledgehammer_cli, "REPO", str(linked))
        monkeypatch.setattr(sledgehammer_cli.sys, "prefix", str(linked / ".venv"))
        with pytest.raises(_Execd):
            sledgehammer_cli._delegate_to_checkout_venv()
        assert hand_off[0][0] == os.path.join(os.path.realpath(cy), ".venv", "bin", "sledgehammer")

    def test_main_hands_off_first(self, tmp_path, monkeypatch, hand_off) -> None:
        monkeypatch.setattr(sledgehammer_cli, "REPO", str(_fake_checkout(tmp_path / "repo")))
        monkeypatch.setattr(sledgehammer_cli.sys, "prefix", str(tmp_path / "conda"))
        monkeypatch.setattr(sledgehammer_cli.sys, "argv", ["sledgehammer", "--help"])
        with pytest.raises(_Execd):
            sledgehammer_cli.main()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership, permissions and $HOME")
class TestCheckoutForCwd:
    @pytest.fixture(autouse=True)
    def _layout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir()
        _confine_lookup(tmp_path, monkeypatch)
        self.repo = _fake_checkout(tmp_path / "repo")
        monkeypatch.setattr(sledgehammer_cli, "REPO", str(self.repo))
        self.chdir = lambda p: (p.mkdir(parents=True, exist_ok=True), monkeypatch.chdir(p))

    def _picked(self):
        return os.path.realpath(sledgehammer_cli._checkout_for_cwd())

    def test_chipyard_tree_picks_vlsi_hammer(self, tmp_path) -> None:
        cy = _fake_checkout(tmp_path / "cy" / "vlsi" / "hammer")
        self.chdir(tmp_path / "cy" / "vlsi" / "build" / "x")
        assert self._picked() == os.path.realpath(cy)

    def test_chipyard_tree_under_home_picks_vlsi_hammer(self, tmp_path) -> None:
        cy = _fake_checkout(tmp_path / "home" / "cy" / "vlsi" / "hammer")
        self.chdir(tmp_path / "home" / "cy" / "vlsi" / "build" / "x")
        assert self._picked() == os.path.realpath(cy)

    def test_chipyard_root_picks_vlsi_hammer(self, tmp_path) -> None:
        cy = _fake_checkout(tmp_path / "cy" / "vlsi" / "hammer")
        self.chdir(tmp_path / "cy")
        assert self._picked() == os.path.realpath(cy)

    def test_inside_checkout_picks_it(self, tmp_path) -> None:
        other = _fake_checkout(tmp_path / "other")
        self.chdir(other / "e2e")
        assert self._picked() == os.path.realpath(other)

    def test_workspace_root_picks_hammer_child(self, tmp_path) -> None:
        ws = _fake_checkout(tmp_path / "ws" / "hammer")
        self.chdir(tmp_path / "ws" / "designs")
        assert self._picked() == os.path.realpath(ws)

    def test_outside_any_checkout_uses_repo(self, tmp_path) -> None:
        self.chdir(tmp_path / "elsewhere")
        assert self._picked() == os.path.realpath(self.repo)

    @pytest.mark.parametrize("kind", ["no venv", "no airflow (lab profile)", "upstream hammer"])
    def test_incomplete_or_foreign_checkout_is_skipped(self, tmp_path, kind) -> None:
        _fake_checkout(tmp_path / "cy" / "vlsi" / "hammer", venv=kind != "no venv",
                       airflow=kind != "no airflow (lab profile)", launcher=kind != "upstream hammer")
        self.chdir(tmp_path / "cy" / "vlsi")
        assert self._picked() == os.path.realpath(self.repo)

    def test_someone_elses_checkout_is_skipped(self, tmp_path, monkeypatch) -> None:
        _fake_checkout(tmp_path / "shared" / "hammer")
        self.chdir(tmp_path / "shared" / "x")
        uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: uid + 1)
        assert self._picked() == os.path.realpath(self.repo)

    def test_world_writable_checkout_is_skipped(self, tmp_path) -> None:
        planted = _fake_checkout(tmp_path / "shared" / "hammer")
        (planted / ".venv" / "bin").chmod(0o777)
        self.chdir(tmp_path / "shared" / "x")
        assert self._picked() == os.path.realpath(self.repo)

    def test_hammer_child_of_home_is_not_used(self, tmp_path) -> None:
        _fake_checkout(tmp_path / "home" / "hammer")
        self.chdir(tmp_path / "home" / "projects" / "x")
        assert self._picked() == os.path.realpath(self.repo)

    def test_checkout_directly_in_home_still_counts_from_inside(self, tmp_path) -> None:
        mine = _fake_checkout(tmp_path / "home" / "hammer")
        self.chdir(mine / "e2e")
        assert self._picked() == os.path.realpath(mine)

    def test_deleted_cwd_falls_back_to_repo(self, tmp_path) -> None:
        gone = tmp_path / "gone"
        self.chdir(gone)
        gone.rmdir()
        assert self._picked() == os.path.realpath(self.repo)


class TestCopyInfo:
    def test_help_names_the_copy(self, tmp_path, monkeypatch, capsys) -> None:
        monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path))
        monkeypatch.setattr(sledgehammer_cli.sys, "argv", ["sledgehammer", "--help"])
        assert sledgehammer_cli.main() == 0
        out = capsys.readouterr().out.rstrip()
        assert out.endswith(f"this copy: {sledgehammer_cli.REPO} (python {sys.executable})")

    def test_missing_airflow_exits_with_a_hint(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sledgehammer_cli, "_venv_bin", lambda name: str(tmp_path / "bin" / name))
        with pytest.raises(SystemExit, match="type -a sledgehammer") as e:
            sledgehammer_cli._airflow("version")
        assert sys.executable in str(e.value)

    def test_passthrough_without_airflow_exits_with_a_hint(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sledgehammer_cli, "_venv_bin", lambda name: str(tmp_path / "bin" / name))
        monkeypatch.setattr(sledgehammer_cli, "_load_stack_env", lambda: None)
        monkeypatch.setattr(sledgehammer_cli, "_load_secrets", lambda: None)
        monkeypatch.delenv("SLEDGE_DRYRUN", raising=False)
        monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path))
        monkeypatch.setattr(sledgehammer_cli.sys, "argv", ["sledgehammer", "dags", "list"])
        with pytest.raises(SystemExit, match="type -a sledgehammer"):
            sledgehammer_cli.main()

    @pytest.mark.parametrize("argv", [["status"], ["par-Foo"]])
    @pytest.mark.parametrize("which,noted", [("other", True), ("repo", False), ("plain", False)])
    def test_foreign_airflow_home_is_reported(self, tmp_path, monkeypatch, capsys, argv, which, noted) -> None:
        repo = _fake_checkout(tmp_path / "repo")
        homes = {"other": _fake_checkout(tmp_path / "other"), "repo": repo, "plain": tmp_path / "plain"}
        monkeypatch.setattr(sledgehammer_cli, "REPO", str(repo))
        monkeypatch.setenv("AIRFLOW_HOME", str(homes[which]))
        monkeypatch.setattr(sledgehammer_cli, "_load_stack_env", lambda: None)
        monkeypatch.setattr(sledgehammer_cli, "_load_secrets", lambda: None)
        monkeypatch.setattr(sledgehammer_cli, "_cmd_status", lambda args, list_only=False: 0)
        monkeypatch.setattr(sledgehammer_cli, "_cmd_run", lambda args: 0)
        monkeypatch.setattr(sledgehammer_cli.sys, "argv", ["sledgehammer", *argv])
        assert sledgehammer_cli.main() == 0
        assert ("a different checkout" in capsys.readouterr().err) == noted
