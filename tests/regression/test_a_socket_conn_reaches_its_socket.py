import json
import os
import shutil
import subprocess
import sys

import pytest

from hammer.vlsi import pd_store
from tests.utils import fake_postgres
from tests.utils.postgres_switch import Sandbox

SOCKET = "/var/run/postgresql"
CONN = f"postgresql+psycopg2:///airflow_ab?host={SOCKET}"

# Loads the launcher with Airflow stubbed out and runs the scheduler guard against a psycopg2 that records each login.
DRIVER = """
import datetime, importlib.util, json, os, sys, types

for name in ("airflow", "airflow.cli", "airflow.cli.commands", "airflow.cli.commands.standalone_command",
             "airflow.executors", "airflow.executors.executor_loader", "airflow.executors.executor_constants"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["airflow.cli.commands.standalone_command"].StandaloneCommand = object
sys.modules["airflow.executors.executor_loader"].ExecutorLoader = object


class Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return [("other-host", datetime.datetime(2026, 10, 14, 9, 30))]


class Connection:
    def cursor(self):
        return Cursor()

    def close(self):
        pass


def connect(*args, **kwargs):
    with open(os.environ["CALLS"], "a") as f:
        f.write(json.dumps(kwargs) + "\\n")
    return Connection()


driver = types.ModuleType("psycopg2")
driver.connect = connect
sys.modules["psycopg2"] = driver
spec = importlib.util.spec_from_file_location("launcher", sys.argv[1])
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
launcher._refuse_if_scheduler_running()
"""


@pytest.mark.parametrize("conn, expected", [
    (CONN, {"host": SOCKET, "dbname": "airflow_ab"}),
    ("postgresql+psycopg2:///airflow_ab?host=%2FVar%2Frun%2Fpostgresql", {"host": "/Var/run/postgresql",
                                                                         "dbname": "airflow_ab"}),
    ("postgresql://ab@/airflow_ab?host=/tmp&port=5433", {"host": "/tmp", "port": 5433, "user": "ab",
                                                         "dbname": "airflow_ab"}),
    ("postgresql://ab:pw@db.example.edu:5432/airflow?host=DB2.example.edu&port=6543&sslmode=require",
     {"host": "db2.example.edu", "port": 6543, "user": "ab", "password": "pw", "dbname": "airflow"}),
    ("postgresql://ab:pw?host=/evil@db.example.edu/airflow", {"host": "db.example.edu", "user": "ab",
                                                              "password": "pw?host=/evil", "dbname": "airflow"}),
    ("postgresql://ab:pw@db.example.edu/airflow?host=&port=0", {"host": "db.example.edu", "user": "ab",
                                                                "password": "pw", "dbname": "airflow"}),
], ids=["socket", "encoded-socket", "socket-port-user", "query-wins", "raw-password-not-a-query", "blank-ignored"])
def test_the_conn_parser_reads_the_query_as_sqlalchemy_hands_it_to_psycopg2(conn, expected) -> None:
    assert pd_store._parse_conn_uri(conn) == expected


def test_the_metadata_lookup_and_the_cache_follow_the_socket(monkeypatch) -> None:
    for var in ("HAMMER_PG_HOST", "HAMMER_PG_PORT", "HAMMER_PG_USER", "SLEDGE_METADATA_CONN", "PGPORT"):
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", CONN)
    assert pd_store.airflow_metadata_conn_settings() == {"host": SOCKET, "dbname": "airflow_ab"}
    fields = pd_store.pg_fields(target={})
    assert (fields["host"], fields["port"]) == ((SOCKET, "conn"), (5432, "libpq"))
    monkeypatch.setenv("PGPORT", "6543")
    assert pd_store.pg_fields(target={})["port"] == (6543, "libpq")
    monkeypatch.delenv("PGPORT")
    pd_store.export_conn_defaults()
    assert (os.environ["HAMMER_PG_HOST"], os.environ["HAMMER_PG_PORT"]) == (SOCKET, "5432")


@pytest.mark.parametrize("pghost", [None, SOCKET, "/elsewhere"], ids=["no-PGHOST", "PGHOST-same", "PGHOST-other"])
def test_the_scheduler_guard_reaches_the_socket_with_or_without_pghost(tmp_path, pghost) -> None:
    script = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py") / "scripts" / \
        "airflow-standalone-ldap.py"
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER)
    calls = tmp_path / "calls.jsonl"
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "USER": "ab", "PATH": os.environ.get("PATH", ""), "CALLS": str(calls),
           "PYTHONPATH": str(fake_postgres.REPO), "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": CONN,
           "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    if pghost:
        env["PGHOST"] = pghost
    res = subprocess.run([sys.executable, str(driver), str(script)], capture_output=True, text=True, timeout=120,
                         cwd=str(home), env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    logins = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [{k: v for k, v in login.items() if k != "connect_timeout"} for login in logins] == \
        [{"host": SOCKET, "dbname": "airflow_ab"}]
    assert "another scheduler is already heartbeating on this database" in res.stderr


COURSE_ENV = {"AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": CONN, "PGHOST": SOCKET, "HAMMER_PG_HOST": SOCKET,
              "HAMMER_PG_PORT": "5432", "HAMMER_PG_DB": "sledgehammer_studio", "HAMMER_PG_PASSWORD": "unused"}
needs_bash = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def box(tmp_path, monkeypatch, capsys):
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, fake_postgres.system_tools(tmp_path / "tools"), secrets=None)
    yield sandbox
    sandbox.restore()


@needs_bash
@pytest.mark.parametrize("pgport, note", [(None, None), ("6543", "/var/run/postgresql:6543")], ids=["libpq-default",
                                                                                                    "PGPORT"])
def test_postgres_show_sees_one_server_for_the_course_socket_conn(box, monkeypatch, pgport, note) -> None:
    for var, value in COURSE_ENV.items():
        monkeypatch.setenv(var, value)
    if pgport:
        monkeypatch.setenv("PGPORT", pgport)
    else:
        monkeypatch.setenv("PGPORT", "")
        monkeypatch.delenv("PGPORT")
    rc, out, err = box.run("show", tty=False)
    assert rc == 0, err
    if note is None:
        assert "different servers" not in out
    else:
        assert (f"NOTE: Airflow and the cache point at different servers: Airflow at {note}, the cache at "
                f"{SOCKET}:5432.") in out


@needs_bash
def test_postgres_configure_offers_a_host_it_accepts_for_a_socket_conn(box, monkeypatch) -> None:
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", CONN)
    rc, out, err = box.run("configure", answers=["", "", "", "", "", "pw", "pw", "n"])
    assert box.prompts[:2] == [("input", "  host [localhost]: "), ("input", "  port [5432]: ")]
    assert "a host name or IP address" not in out
    assert rc == 1 and "Nothing changed." in out


@needs_bash
def test_postgres_show_says_the_cache_port_is_libpqs_default_for_a_portless_conn(box, monkeypatch) -> None:
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", CONN)
    rc, out, err = box.run("show", tty=False)
    assert rc == 0, err
    assert "5432" in out and "libpq's default for a connection without a port (PGPORT, else 5432)" in out
