import shutil
import subprocess
import sys

import pytest

from hammer.shell import sledgehammer_cli


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")
def test_launch_does_not_pass_positionals_to_venv_sh(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "hammer"
    repo.mkdir()
    (repo / "venv.sh").write_text("export VENV_SH_ARGC=$#\n")
    launcher = repo / "launcher.py"
    launcher.write_text("import os, sys\nprint(os.environ['VENV_SH_ARGC'], sys.argv[1:])\n")
    monkeypatch.setattr(sledgehammer_cli, "REPO", str(repo))
    monkeypatch.setattr(sledgehammer_cli, "LAUNCHER", str(launcher))
    monkeypatch.setattr(sledgehammer_cli.sys, "argv", ["sledgehammer", "up", "--port", "a b"])
    monkeypatch.delenv("SLEDGE_DRYRUN", raising=False)
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path))
    captured = []

    def fake_execvp(file, args):
        captured.append(args)
        raise SystemExit(0)

    monkeypatch.setattr(sledgehammer_cli.os, "execvp", fake_execvp)
    with pytest.raises(SystemExit):
        sledgehammer_cli.main()
    out = subprocess.run(captured[0], capture_output=True, text=True, check=True).stdout
    assert out.strip().splitlines()[-1] == "0 ['--port', 'a b']"
