import contextlib
import getpass
import io
import json
import os
import subprocess
import sys
import types
from types import SimpleNamespace

import pytest

from hammer.shell import sledgehammer_cli
from hammer.vlsi import run_liveness
from hammer.vlsi.run_liveness import Activity, Task


def _no_processes():
    class Error(Exception):
        pass

    def process(pid):
        raise Error(pid)

    psutil = types.ModuleType("psutil")
    psutil.__dict__.update(Error=Error, STATUS_ZOMBIE="zombie", Process=process, process_iter=lambda: [])
    return psutil


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """`sledgehammer run par -t Top` against a DAG whose OBJ_DIR may hold another run's lock."""
    dags = tmp_path / "dags"
    dags.mkdir()
    obj = tmp_path / "Top"
    obj.mkdir()
    (dags / f"sledgehammer_Top_{getpass.getuser()}.py").write_text(f'OBJ_DIR = "{obj}"\n\'forceall\': Param()\n')
    w = SimpleNamespace(obj=obj, calls=[], checks=[], activity=None)

    def fake_airflow(*a, capture=True):
        w.calls.append(a)
        return 0, "", ""

    def fake_run(cmd, **kw):
        w.checks.append((cmd, kw))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run_liveness.main(cmd[3:])
        return subprocess.CompletedProcess(cmd, 0, "airflow says hello\n" + out.getvalue(), "")

    monkeypatch.setattr(sledgehammer_cli, "_airflow", fake_airflow)
    monkeypatch.setattr(sledgehammer_cli, "_dags_folder", lambda: (str(dags), "test"))
    monkeypatch.setattr(sledgehammer_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(run_liveness, "fetch_run_activity", lambda dag_id, run_id: w.activity)
    monkeypatch.setattr(run_liveness, "this_host", lambda: "here.example")
    monkeypatch.setitem(sys.modules, "psutil", _no_processes())
    w.lock = lambda: (obj / ".sledgehammer-run.lock").write_text(json.dumps(
        {"dag_id": f"sledgehammer_Top_{getpass.getuser()}", "run_id": "old", "user": getpass.getuser(),
         "host": "here.example", "pid": 4242, "claimed": "2026-10-18T04:00:00"}))
    w.run = lambda *extra: sledgehammer_cli._cmd_run(["par", "-t", "Top", "--no-wait", *extra])
    w.triggered = lambda: any(c[:2] == ("dags", "trigger") for c in w.calls)
    return w


def test_run_refuses_to_trigger_into_a_folder_a_live_run_holds(cli, monkeypatch) -> None:
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/conda/lib")
    cli.lock()
    cli.activity = Activity("running", 40, [Task("module_Top.par", "running", "here.example", 4242, "ti", 3)])
    with pytest.raises(SystemExit) as err:
        cli.run()
    assert str(err.value.code).startswith(f"[sledgehammer] run already in progress in {cli.obj}.\n  held by run 'old'")
    assert "  last sign of life: task module_Top.par (running) on here.example, 3 s ago\n" in err.value.code
    assert err.value.code.endswith("To stop that run and free this directory:  sledgehammer cancel old")
    assert not cli.triggered()
    cmd, kw = cli.checks[0]
    assert cmd == [sys.executable, "-m", "hammer.vlsi.run_liveness", str(cli.obj)]
    assert "LD_LIBRARY_PATH" not in kw["env"]


def test_run_takes_over_a_stale_lock_and_says_so(cli, capsys) -> None:
    cli.lock()
    assert cli.run() == 0
    assert cli.triggered()
    assert ("[sledgehammer] holder 'old' is not in the Airflow database and no process of it runs on this host; "
            "taking over the stale lock.") in capsys.readouterr().out


def test_run_after_a_finished_run_triggers_quietly(cli, capsys) -> None:
    cli.lock()
    cli.activity = Activity("success", 5, [])
    assert cli.run() == 0
    assert cli.triggered() and "holder" not in capsys.readouterr().out


def test_run_with_no_lock_skips_the_check(cli) -> None:
    assert cli.run() == 0
    assert cli.triggered() and cli.checks == []


def test_a_named_workspace_skips_the_check(cli) -> None:
    cli.lock()
    cli.activity = Activity("running", 40, [])
    assert cli.run("--workspace", "iter2") == 0
    assert cli.triggered() and cli.checks == []


def test_a_check_that_cannot_run_leaves_it_to_the_run(cli, monkeypatch, capsys) -> None:
    cli.lock()
    monkeypatch.setattr(sledgehammer_cli.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "ModuleNotFoundError"))
    assert cli.run() == 0
    assert cli.triggered()
    assert f"could not check whether another run holds {cli.obj}; the run checks again when it starts." in \
        capsys.readouterr().err


def test_the_check_runs_as_a_module_and_refuses_when_the_database_is_down(tmp_path) -> None:
    (tmp_path / ".sledgehammer-run.lock").write_text(json.dumps({"dag_id": "d", "run_id": "old", "user": "u"}))
    env = dict(os.environ, AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="postgresql://u:p@127.0.0.1:1/airflow",
               HAMMER_PG_CONNECT_TIMEOUT="5")
    r = subprocess.run([sys.executable, "-m", "hammer.vlsi.run_liveness", str(tmp_path)],
                       capture_output=True, text=True, env=env, timeout=120)
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert got["kind"] == "unknown"
    assert "  could not check whether it is still alive: " in got["text"]
