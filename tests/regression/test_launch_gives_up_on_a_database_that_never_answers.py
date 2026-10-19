import json
import os
import socket
import subprocess
import sys

import pytest

from tests.utils import fake_postgres

HOOKS = ["_refuse_if_scheduler_running", "_prepare_cache_schema"]

# Runs the launcher to Airflow's start with a psycopg2 that, like libpq, waits connect_timeout seconds or forever.
DRIVER = """
import json, os, runpy, socket, sys, time, types

for name in ("airflow", "airflow.cli", "airflow.cli.commands", "airflow.cli.commands.standalone_command",
             "airflow.executors", "airflow.executors.executor_loader", "airflow.executors.executor_constants"):
    sys.modules[name] = types.ModuleType(name)


class StandaloneCommand:
    def run(self):
        print("[stub] airflow started")


sys.modules["airflow.cli.commands.standalone_command"].StandaloneCommand = StandaloneCommand
sys.modules["airflow.executors.executor_loader"].ExecutorLoader = object


class OperationalError(Exception):
    pass


def hook():
    frame = sys._getframe(2)
    while frame is not None and not frame.f_code.co_name.startswith(("_refuse_if", "_prepare")):
        frame = frame.f_back
    return frame.f_code.co_name if frame is not None else "?"


def connect(**kwargs):
    start = time.monotonic()
    try:
        with socket.create_connection((kwargs["host"], kwargs["port"])) as server:
            server.settimeout(kwargs.get("connect_timeout") or None)
            server.sendall(b"startup")
            server.recv(1)
    except socket.timeout:
        raise OperationalError("timeout expired")
    except OSError:
        raise OperationalError("connection refused")
    finally:
        with open(os.environ["CALLS"], "a") as f:
            f.write(json.dumps([hook(), kwargs.get("connect_timeout"), time.monotonic() - start]) + "\\n")
    raise OperationalError("server closed the connection unexpectedly")


driver = types.ModuleType("psycopg2")
driver.connect = connect
driver.Error = driver.OperationalError = OperationalError
driver.extras = types.ModuleType("psycopg2.extras")
driver.extras.Json = object
sys.modules["psycopg2"] = driver
sys.modules["psycopg2.extras"] = driver.extras
runpy.run_path(sys.argv[1], run_name="__main__")
"""


def launch(tmp_path, port, **env):
    script = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py") / "scripts" / \
        "airflow-standalone-ldap.py"
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    calls = tmp_path / "calls.jsonl"
    home = tmp_path / "home"
    home.mkdir()
    try:
        res = subprocess.run([sys.executable, str(driver), str(script)], capture_output=True, text=True,
                             timeout=60, cwd=str(home),
                             env={"HOME": str(home), "USER": "alice", "PATH": os.environ.get("PATH", ""),
                                  "PYTHONPATH": str(fake_postgres.REPO), "CALLS": str(calls),
                                  "SLEDGE_NO_PGADMIN": "1", "SLEDGE_PORT": str(fake_postgres.free_port()),
                                  "AIRFLOW__API_AUTH__JWT_SECRET": "jwt", "AIRFLOW__API__SECRET_KEY": "key",
                                  "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN":
                                      f"postgresql+psycopg2://alice:pw@127.0.0.1:{port}/airflow_alice", **env})
    except subprocess.TimeoutExpired:
        pytest.fail("the launch still waits on a database that never answers")
    assert res.returncode == 0 and "[stub] airflow started" in res.stdout, res.stdout + res.stderr
    return [json.loads(line) for line in calls.read_text().splitlines()], res.stdout


def test_each_launch_connect_gives_up_on_a_server_that_never_answers(tmp_path) -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(16)
        calls, out = launch(tmp_path, server.getsockname()[1], HAMMER_PG_CONNECT_TIMEOUT="1")
    assert [call[:2] for call in calls] == [[hook, 1] for hook in HOOKS]
    assert all(0.9 < call[2] < 2.5 for call in calls), calls
    assert "[cache] WARNING: could not prepare the cache database (timeout expired)." in out


@pytest.mark.parametrize("setting, seconds", [(None, 10), ("3", 3), ("2.5", 3), ("0", 0)])
def test_each_launch_connect_waits_as_long_as_the_cache_does(tmp_path, setting, seconds) -> None:
    env = {"HAMMER_PG_CONNECT_TIMEOUT": setting} if setting is not None else {}
    calls, _ = launch(tmp_path, fake_postgres.free_port(), **env)
    assert [call[:2] for call in calls] == [[hook, seconds] for hook in HOOKS]
