import json
import os
import subprocess
import sys
from urllib.parse import quote, unquote

import pytest

from tests.utils import fake_postgres

HOOKS = ["_refuse_if_scheduler_running"]
RAW = ["SeKrit/tail", "SeKrit?tail", "SeKrit#tail", "Se/Kr?it#tail", "Qx7/Zv9?Wm4#Rt2"]
SECRET = "Qx7/Zv9?Wm4#Rt2"

# Loads the launcher with Airflow stubbed out and runs the named hooks against a psycopg2 that only accepts LOGIN.
DRIVER = """
import datetime, importlib.util, json, os, sys, types

for name in ("airflow", "airflow.cli", "airflow.cli.commands", "airflow.cli.commands.standalone_command",
             "airflow.executors", "airflow.executors.executor_loader", "airflow.executors.executor_constants"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["airflow.cli.commands.standalone_command"].StandaloneCommand = object
sys.modules["airflow.executors.executor_loader"].ExecutorLoader = object


class Cursor:
    rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return (1,)

    def fetchall(self):
        return [("other-host", datetime.datetime(2026, 10, 12, 9, 30))]


class Connection:
    def cursor(self):
        return Cursor()

    def commit(self):
        pass

    def close(self):
        pass


def connect(*args, **kwargs):
    with open(os.environ["CALLS"], "a") as f:
        f.write(json.dumps([hook, args, kwargs]) + "\\n")
    login = {key: value for key, value in kwargs.items() if key != "connect_timeout"}
    if args or login != json.loads(os.environ["LOGIN"]):
        raise Exception(f"connection to server failed: {args} {kwargs}")
    return Connection()


driver = types.ModuleType("psycopg2")
driver.connect = connect
sys.modules["psycopg2"] = None if os.environ.get("NO_DRIVER") else driver
spec = importlib.util.spec_from_file_location("launcher", sys.argv[1])
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
for hook in sys.argv[2:]:
    getattr(launcher, hook)()
"""


def conn(password: str) -> str:
    return f"postgresql+psycopg2://alice:{password}@localhost:5544/airflow_alice"


def login(password: str) -> dict:
    return {"host": "localhost", "port": 5544, "user": "alice", "dbname": "airflow_alice", "password": password}


class Launcher:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.script = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py") / "scripts" / \
            "airflow-standalone-ldap.py"
        self.driver = tmp_path / "driver.py"
        self.driver.write_text(DRIVER)
        self.calls = tmp_path / "calls.jsonl"
        self.home = tmp_path / "home"
        self.home.mkdir()

    def run(self, conn_uri: str, accepted: dict, *hooks: str, **env: str):
        if self.calls.exists():
            self.calls.unlink()
        res = subprocess.run([sys.executable, str(self.driver), str(self.script), *hooks],
                             capture_output=True, text=True, timeout=120, cwd=str(self.home),
                             env={"HOME": str(self.home), "USER": "alice", "PATH": os.environ.get("PATH", ""),
                                  "PYTHONPATH": str(fake_postgres.REPO), "CALLS": str(self.calls),
                                  "LOGIN": json.dumps(accepted), "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": conn_uri,
                                  **env})
        assert res.returncode == 0, res.stdout + res.stderr
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return calls, res.stdout, res.stderr


@pytest.fixture
def launcher(tmp_path):
    return Launcher(tmp_path)


@pytest.mark.parametrize("password", RAW + [quote(p, safe="") for p in RAW])
def test_every_hook_logs_in_with_the_fields_of_the_conn(launcher, password) -> None:
    raw = unquote(password)
    calls, _, _ = launcher.run(conn(password), login(raw), *HOOKS)
    assert calls == [[hook, [], {**login(raw), "connect_timeout": 10}] for hook in HOOKS]


@pytest.mark.parametrize("password", ["SeKrit/tail", "Se/Kr?it#tail", SECRET])
def test_the_scheduler_guard_runs_with_a_raw_password_conn(launcher, password) -> None:
    _, out, err = launcher.run(conn(password), login(password), *HOOKS)
    assert "[guard] WARNING: another scheduler is already heartbeating on this database:" in err
    assert "other-host (heartbeat 2026-10-12 09:30:00)" in err
    assert out == ""


@pytest.mark.parametrize("password", [SECRET, quote(SECRET, safe="")])
def test_a_failed_login_prints_no_part_of_the_password(launcher, password) -> None:
    calls, out, err = launcher.run(conn(password), login("something else"), *HOOKS)
    assert len(calls) == 1
    for secret in (SECRET, quote(SECRET, safe="")):
        assert not [secret[i:i + 3] for i in range(len(secret) - 2) if secret[i:i + 3] in out + err]


def test_a_conn_with_no_fields_is_never_used_to_log_in(launcher) -> None:
    calls, out, _ = launcher.run("postgresql+psycopg2://", login(SECRET), *HOOKS)
    assert calls == []
    assert out == ""


def test_without_psycopg2_the_scheduler_guard_steps_aside(launcher) -> None:
    calls, out, err = launcher.run(conn(SECRET), login(SECRET), *HOOKS, NO_DRIVER="1")
    assert calls == []
    assert out == "" and "[guard]" not in err
