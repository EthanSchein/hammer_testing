import json
import os
import stat
import subprocess
import sys
import types

import pytest

import hammer.shell.hammer_vlsi as hammer_vlsi
from hammer.vlsi import pd_store, run_liveness
from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX file modes")

CONN = "postgresql+psycopg2://alice:Se%2FKr@db.example.edu:5544/airflow_alice"
LOGIN = {"host": "db.example.edu", "port": 5544, "user": "alice", "password": "Se/Kr", "dbname": "airflow_alice"}
DECOY = "airflow-db-not-allowed:///"

# Loads a copy of the launcher with Airflow stubbed out and prints the conn file it exports to the stack.
DRIVER = """
import importlib.util, json, os, sys, types

for name in ("airflow", "airflow.cli", "airflow.cli.commands", "airflow.cli.commands.standalone_command",
             "airflow.executors", "airflow.executors.executor_loader", "airflow.executors.executor_constants"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["airflow.cli.commands.standalone_command"].StandaloneCommand = object
sys.modules["airflow.executors.executor_loader"].ExecutorLoader = object
spec = importlib.util.spec_from_file_location("launcher", sys.argv[1])
spec.loader.exec_module(importlib.util.module_from_spec(spec))
print(json.dumps(os.environ.get("SLEDGE_METADATA_CONN_FILE")))
"""


def _launch(tmp_path, conn=CONN, **env):
    repo = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py")
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    home = tmp_path / "home"
    home.mkdir()
    res = subprocess.run([sys.executable, str(driver), str(repo / "scripts" / "airflow-standalone-ldap.py")],
                         capture_output=True, text=True, timeout=120, cwd=str(home),
                         env={"HOME": str(home), "USER": "alice", "PATH": os.environ.get("PATH", ""),
                              "PYTHONPATH": str(fake_postgres.REPO), "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": conn,
                              **env})
    assert res.returncode == 0, res.stdout + res.stderr
    return repo, json.loads(res.stdout.strip().splitlines()[-1])


@pytest.fixture
def task_env(monkeypatch):
    """The environment Airflow 3 gives a task: a decoy database conn, the stack's SLEDGE_ variables kept."""
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", DECOY)
    monkeypatch.setenv("AIRFLOW__CORE__SQL_ALCHEMY_CONN", DECOY)
    for key in ("SLEDGE_METADATA_CONN", "SLEDGE_METADATA_CONN_FILE", "SLEDGE_SMTP_PASSWORD_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(pd_store, "_parse_airflow_cfg_conn", lambda: {})
    return monkeypatch


def test_the_launcher_stages_the_conn_for_tasks_without_smtp(tmp_path, task_env) -> None:
    repo, staged = _launch(tmp_path)
    assert staged == str(repo / ".sledgehammer" / ".sledge_metadata_conn")
    assert open(staged).read() == CONN + "\n"
    assert stat.S_IMODE(os.stat(staged).st_mode) == 0o600
    task_env.setenv("SLEDGE_METADATA_CONN_FILE", staged)
    assert pd_store.airflow_metadata_conn_settings() == LOGIN


def test_an_smtp_install_keeps_the_conn_beside_its_password_file(tmp_path) -> None:
    smtp = tmp_path / "smtp"
    smtp.mkdir()
    _, staged = _launch(tmp_path, SLEDGE_SMTP_PASSWORD_FILE=str(smtp / "password"))
    assert staged == str(smtp / ".sledge_metadata_conn")
    assert open(staged).read() == CONN + "\n"


def test_a_conn_file_named_by_the_environment_is_used(tmp_path) -> None:
    named = tmp_path / "conns" / "metadata"
    _, staged = _launch(tmp_path, SLEDGE_METADATA_CONN_FILE=str(named))
    assert staged == str(named) and named.read_text() == CONN + "\n"


def test_a_sqlite_stack_stages_nothing(tmp_path) -> None:
    repo, staged = _launch(tmp_path, conn=f"sqlite:///{tmp_path}/airflow.db")
    assert staged is None and not (repo / ".sledgehammer" / ".sledge_metadata_conn").exists()


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0][:1] if self.rows else None


class _Conn(_Cursor):
    def cursor(self):
        return _Cursor(self.rows)

    def close(self):
        pass


def _held_lock(tmp_path):
    obj = tmp_path / "Top"
    obj.mkdir()
    (obj / ".sledgehammer-run.lock").write_text(json.dumps(
        {"dag_id": "sledgehammer_Top_alice", "run_id": "old", "user": "alice", "pid": 4242,
         "claimed": "2026-10-18T04:00:00"}))
    return obj


def test_a_task_takes_over_the_lock_of_a_run_marked_failed(tmp_path, task_env, capsys) -> None:
    repo, staged = _launch(tmp_path)
    task_env.setenv("SLEDGE_METADATA_CONN_FILE", staged)
    logins = []

    def connect(**kw):
        logins.append(kw)
        return _Conn([("failed", 120.0, "module_Top.par", "failed", "here.example", 4242, "ti", 130.0)])

    task_env.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=connect))
    obj = _held_lock(tmp_path)
    hammer_vlsi.claim_obj_dir(str(obj), "sledgehammer_Top_alice", "new", "alice")
    assert json.loads((obj / ".sledgehammer-run.lock").read_text())["run_id"] == "new"
    assert "holder 'old' is terminal (failed); taking over the stale lock." in capsys.readouterr().out
    assert {k: v for k, v in logins[0].items() if k != "connect_timeout"} == LOGIN


def test_a_task_that_cannot_reach_the_database_refuses_with_the_cancel_command(tmp_path, task_env) -> None:
    task_env.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=lambda **kw: pytest.fail("connected")))
    obj = _held_lock(tmp_path)
    with pytest.raises(hammer_vlsi.RunLockConflict) as err:
        hammer_vlsi.claim_obj_dir(str(obj), "sledgehammer_Top_alice", "new", "alice")
    assert ("  could not check whether it is still alive: "
            "no Postgres connection to the Airflow database is configured\n") in str(err.value)
    assert str(err.value).endswith(f"To stop that run and free this directory:  sledgehammer cancel old "
                                   f"--obj_dir {obj}")
    assert json.loads((obj / ".sledgehammer-run.lock").read_text())["run_id"] == "old"
