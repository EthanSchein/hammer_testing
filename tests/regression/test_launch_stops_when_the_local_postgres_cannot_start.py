import contextlib
import os
import shutil
import socket
import subprocess
import sys

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="sledge-local-postgres.sh needs bash")

# Stands in for Airflow and ends the launch at its import, after the launcher's environment setup has run.
STANDALONE_STUB = "print('[stub] airflow imported')\nraise SystemExit(0)\n"


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@contextlib.contextmanager
def held(port):
    with socket.socket() as other:
        other.bind(("127.0.0.1", port))
        other.listen()
        yield


class LocalPostgres:
    def __init__(self, tmp_path, tools, made=True):
        self.log = tmp_path / "calls.log"
        self.repo = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py",
                                           "sledge-local-postgres.sh")
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.root = self.home / ".sledgehammer" / "postgres"
        self.port = fake_postgres.free_port()
        self.tools = tools
        self.pg_path = os.pathsep.join((fake_postgres.postgres(tmp_path / "pgbin", self.log), tools))
        if made:
            for step in ("setup", "stop"):
                res = self.local_postgres(step)
                assert res.returncode == 0, res.stdout + res.stderr
        self.stubs = tmp_path / "stubs"
        for pkg in ("airflow", "airflow/cli", "airflow/cli/commands"):
            (self.stubs / pkg).mkdir(parents=True)
            (self.stubs / pkg / "__init__.py").write_text("")
        (self.stubs / "airflow" / "cli" / "commands" / "standalone_command.py").write_text(STANDALONE_STUB)

    def local_postgres(self, step):
        return subprocess.run(["bash", str(self.repo / "scripts" / "sledge-local-postgres.sh"), step],
                              capture_output=True, text=True, timeout=60,
                              env={"HOME": str(self.home), "USER": "alice", "SLEDGE_PG_PORT": str(self.port),
                                   "PATH": self.pg_path})

    def launch(self, host="localhost", port=None):
        env = {"HOME": str(self.home), "USER": "alice", "PATH": self.tools, "SLEDGE_NO_PGADMIN": "1",
               "PYTHONPATH": os.pathsep.join((str(self.stubs), str(fake_postgres.REPO))),
               "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN":
                   f"postgresql+psycopg2://alice:pw@{host}:{port or self.port}/airflow_alice"}
        return subprocess.run([sys.executable, str(self.repo / "scripts" / "airflow-standalone-ldap.py")],
                              capture_output=True, text=True, timeout=120, cwd=str(self.home), env=env)

    def starts(self):
        return [c for c in fake_postgres.calls(self.log, "pg_ctl") if c.endswith(" start")]

    def ended_with(self, res, reason):
        assert "[stub] airflow imported" not in res.stdout, res.stdout + res.stderr
        assert res.returncode == 1 and res.stdout == ""
        assert res.stderr.splitlines() == [f"[postgres] ERROR: could not start your local Postgres in {self.root}: "
                                           f"{reason}"]

    def went_on(self, res, reason):
        assert res.returncode == 0 and "[stub] airflow imported" in res.stdout, res.stdout + res.stderr
        assert [line for line in res.stdout.splitlines() if line.startswith("[postgres]")] == \
            [f"[postgres] WARNING: could not start your local Postgres in {self.root}: {reason}"]
        assert res.stderr == ""


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_a_local_postgres_whose_port_is_taken_ends_the_launch_with_one_line(tmp_path, tools, host) -> None:
    pg = LocalPostgres(tmp_path, tools)
    with held(pg.port):
        res = pg.launch(host)
    pg.ended_with(res, f"sledge-local-postgres: port {pg.port} is in use by another program, so this Postgres "
                       f"cannot start on it. Stop that program, or change port in {pg.root}/data/postgresql.conf "
                       f"and in your secrets.")
    assert len(pg.starts()) == 1


def test_a_local_postgres_that_fails_to_start_ends_the_launch_with_one_line(tmp_path, tools) -> None:
    pg = LocalPostgres(tmp_path, tools)
    (pg.root / "data" / "postmaster.pid").mkdir()
    res = pg.launch()
    pg.ended_with(res, f"sledge-local-postgres: Postgres did not start; log: {pg.root}/postgres.log")
    assert len(pg.starts()) == 2


def test_a_local_postgres_left_running_on_another_host_ends_the_launch_with_one_line(tmp_path, tools) -> None:
    pg = LocalPostgres(tmp_path, tools)
    (pg.root / "data" / "postmaster.pid").write_text("4242\n")
    (pg.root / "host").write_text("other-host\n")
    res = pg.launch()
    pg.ended_with(res, f"sledge-local-postgres: this Postgres was started on other-host and not stopped there. "
                       f"Two servers on one data directory corrupt it, so stop it on other-host first "
                       f"({pg.repo}/scripts/sledge-local-postgres.sh stop). If other-host is gone for good, "
                       f"remove {pg.root}/host and start again.")
    assert len(pg.starts()) == 1


def test_the_last_port_in_postgresql_conf_is_the_one_the_conn_must_match(tmp_path, tools) -> None:
    pg = LocalPostgres(tmp_path, tools)
    port = fake_postgres.free_port()
    with open(pg.root / "data" / "postgresql.conf", "a") as conf:
        conf.write(f"port = {port}\n")
    with held(port):
        res = pg.launch(port=port)
    pg.ended_with(res, f"sledge-local-postgres: port {port} is in use by another program, so this Postgres "
                       f"cannot start on it. Stop that program, or change port in {pg.root}/data/postgresql.conf "
                       f"and in your secrets.")


def test_a_conn_on_another_local_port_warns_of_a_taken_port_and_goes_on_to_airflow(tmp_path, tools) -> None:
    pg = LocalPostgres(tmp_path, tools)
    with held(pg.port):
        res = pg.launch(port=fake_postgres.free_port())
    pg.went_on(res, f"sledge-local-postgres: port {pg.port} is in use by another program, so this Postgres "
                    f"cannot start on it. Stop that program, or change port in {pg.root}/data/postgresql.conf "
                    f"and in your secrets.")
    assert len(pg.starts()) == 1


def test_a_conn_on_another_local_port_warns_of_missing_tools_and_goes_on_to_airflow(tmp_path, tools) -> None:
    pg = LocalPostgres(tmp_path, tools)
    shutil.rmtree(tmp_path / "pgbin")
    res = pg.launch(port=fake_postgres.free_port())
    pg.went_on(res, f"sledge-local-postgres: no initdb, pg_ctl and psql found for {pg.root}; "
                    f"put the directory holding them on PATH")


def test_a_remote_conn_goes_on_to_airflow_whatever_the_local_postgres_does(tmp_path, tools) -> None:
    pg = LocalPostgres(tmp_path, tools)
    calls = fake_postgres.calls(pg.log)
    with held(pg.port):
        res = pg.launch(host="db.example.edu")
    assert res.returncode == 0 and "[stub] airflow imported" in res.stdout, res.stdout + res.stderr
    assert "[postgres]" not in res.stdout + res.stderr
    assert fake_postgres.calls(pg.log) == calls


def test_a_local_conn_without_a_local_postgres_goes_on_to_airflow(tmp_path, tools) -> None:
    pg = LocalPostgres(tmp_path, tools, made=False)
    with held(pg.port):
        res = pg.launch()
    assert res.returncode == 0 and "[stub] airflow imported" in res.stdout, res.stdout + res.stderr
    assert "[postgres]" not in res.stdout + res.stderr
    assert fake_postgres.calls(pg.log) == []
