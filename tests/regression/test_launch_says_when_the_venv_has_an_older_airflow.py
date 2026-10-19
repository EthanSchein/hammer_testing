import importlib.metadata
import os
import shlex

import pytest

from hammer.shell import sledgehammer_cli


def _launch(tmp_path, monkeypatch, capsys, installed):
    def version(name):
        assert name == "apache-airflow"
        if installed is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(importlib.metadata, "version", version)
    monkeypatch.setenv("SLEDGE_LITE", "")
    monkeypatch.delenv("SLEDGE_LITE")
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path))
    monkeypatch.setenv("SLEDGE_DRYRUN", "1")
    monkeypatch.setattr(sledgehammer_cli.sys, "argv", ["sledgehammer", "lite"])
    assert sledgehammer_cli.main() == 0
    return capsys.readouterr().out


def test_an_older_airflow_is_named_with_the_command_that_upgrades_it(tmp_path, monkeypatch, capsys) -> None:
    out = _launch(tmp_path, monkeypatch, capsys, "3.1.0")
    setup = shlex.quote(os.path.join(sledgehammer_cli.REPO, "scripts", "uv_setup.sh"))
    assert out.startswith("[sledgehammer] this checkout uses Airflow 3.1.8, but your venv has 3.1.0.\n"
                          f"  Stop sledgehammer (Ctrl-C), run {setup}, then start sledgehammer again.\n")
    assert "[dryrun] launch" in out


@pytest.mark.parametrize("installed", ["3.1.8", "3.2.0", None], ids=["same", "newer", "not-installed"])
def test_the_airflow_setup_installs_or_a_newer_one_starts_without_a_note(tmp_path, monkeypatch, capsys,
                                                                          installed) -> None:
    out = _launch(tmp_path, monkeypatch, capsys, installed)
    assert "this checkout uses Airflow" not in out and "[dryrun] launch" in out
