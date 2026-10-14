import os
import shutil
import subprocess
import sys
from urllib.parse import quote

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="sledge-local-postgres.sh needs bash")

# Stands in for Airflow and ends the launch at its import, after the launcher's environment setup has run.
STANDALONE_STUB = "print('[stub] airflow imported')\nraise SystemExit(0)\n"
RAW = ["SeKrit/tail", "SeKrit?tail", "SeKrit#tail", "Se/Kr?it#tail"]


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


class StoppedLocalPostgres:
    def __init__(self, tmp_path, tools):
        self.log = tmp_path / "calls.log"
        self.repo = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py",
                                           "sledge-local-postgres.sh")
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.root = self.home / ".sledgehammer" / "postgres"
        self.port = fake_postgres.free_port()
        self.tools = tools
        pg_path = os.pathsep.join((fake_postgres.postgres(tmp_path / "pgbin", self.log), tools))
        for step in ("setup", "stop"):
            res = subprocess.run(["bash", str(self.repo / "scripts" / "sledge-local-postgres.sh"), step],
                                 capture_output=True, text=True, timeout=60,
                                 env={"HOME": str(self.home), "USER": "alice", "SLEDGE_PG_PORT": str(self.port),
                                      "PATH": pg_path})
            assert res.returncode == 0, res.stdout + res.stderr
        self.stubs = tmp_path / "stubs"
        for pkg in ("airflow", "airflow/cli", "airflow/cli/commands"):
            (self.stubs / pkg).mkdir(parents=True)
            (self.stubs / pkg / "__init__.py").write_text("")
        (self.stubs / "airflow" / "cli" / "commands" / "standalone_command.py").write_text(STANDALONE_STUB)

    def launch(self, password, host="localhost"):
        env = {"HOME": str(self.home), "USER": "alice", "PATH": self.tools, "SLEDGE_NO_PGADMIN": "1",
               "PYTHONPATH": os.pathsep.join((str(self.stubs), str(fake_postgres.REPO))),
               "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN":
                   f"postgresql+psycopg2://alice:{password}@{host}:{self.port}/airflow_alice"}
        res = subprocess.run([sys.executable, str(self.repo / "scripts" / "airflow-standalone-ldap.py")],
                             capture_output=True, text=True, timeout=120, cwd=str(self.home), env=env)
        assert "[stub] airflow imported" in res.stdout, res.stdout + res.stderr
        return [line for line in res.stdout.splitlines() if line.startswith("[postgres]")]

    def starts(self):
        return [c for c in fake_postgres.calls(self.log, "pg_ctl") if c.endswith(" start")]


@pytest.mark.parametrize("password", RAW + [quote(p, safe="") for p in RAW])
def test_a_stopped_local_postgres_starts_whether_or_not_the_password_is_encoded(tmp_path, tools, password) -> None:
    pg = StoppedLocalPostgres(tmp_path, tools)
    assert pg.launch(password) == [f"[postgres] started your local Postgres in {pg.root}"]
    assert len(pg.starts()) == 2
    postmaster_env = (pg.root / "data" / "postmaster.env").read_text()
    assert "SQL_ALCHEMY_CONN" not in postmaster_env and "Krit" not in postmaster_env


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]"])
def test_a_raw_password_conn_to_a_loopback_address_starts_it(tmp_path, tools, host) -> None:
    pg = StoppedLocalPostgres(tmp_path, tools)
    assert pg.launch("Se/Kr?it#tail", host) == [f"[postgres] started your local Postgres in {pg.root}"]
    assert len(pg.starts()) == 2


@pytest.mark.parametrize("password", ["Se/Kr?it#t@localhost", "SeKrit@localhost:5544/airflow_alice"])
def test_a_raw_password_naming_localhost_never_starts_it_for_another_host(tmp_path, tools, password) -> None:
    pg = StoppedLocalPostgres(tmp_path, tools)
    calls = fake_postgres.calls(pg.log)
    assert pg.launch(password, host="db.example.edu") == []
    assert fake_postgres.calls(pg.log) == calls


@pytest.mark.parametrize("password", ["SeKrit", "Se/Kr?it#tail"])
def test_a_conn_without_a_host_name_leaves_it_stopped(tmp_path, tools, password) -> None:
    pg = StoppedLocalPostgres(tmp_path, tools)
    calls = fake_postgres.calls(pg.log)
    assert pg.launch(password, host="") == []
    assert fake_postgres.calls(pg.log) == calls


def test_without_hammer_it_warns_and_leaves_it_stopped(tmp_path, tools) -> None:
    pg = StoppedLocalPostgres(tmp_path, tools)
    (pg.stubs / "hammer").mkdir()
    (pg.stubs / "hammer" / "__init__.py").write_text("raise ImportError('hammer is not installed')\n")
    calls = fake_postgres.calls(pg.log)
    assert pg.launch("Se/Kr?it#tail") == [
        "[postgres] WARNING: could not check for your local Postgres: hammer is not installed"]
    assert fake_postgres.calls(pg.log) == calls
