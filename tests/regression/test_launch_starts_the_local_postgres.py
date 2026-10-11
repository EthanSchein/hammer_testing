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


class Launch:
    def __init__(self, tmp_path, tools, pg_dir=None):
        self.tmp = tmp_path
        self.log = tmp_path / "calls.log"
        self.repo = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py",
                                           "sledge-local-postgres.sh")
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.tools = tools
        self.pg_bin = fake_postgres.postgres(tmp_path / "pgbin", self.log)
        self.port = fake_postgres.free_port()
        self.root = pg_dir or self.home / ".sledgehammer" / "postgres"
        self.pg_env = {"SLEDGE_PG_DIR": str(pg_dir)} if pg_dir else {}
        self.stubs = tmp_path / "stubs"
        for pkg in ("airflow", "airflow/cli", "airflow/cli/commands"):
            (self.stubs / pkg).mkdir(parents=True)
            (self.stubs / pkg / "__init__.py").write_text("")
        (self.stubs / "airflow" / "cli" / "commands" / "standalone_command.py").write_text(STANDALONE_STUB)

    def local_postgres(self, *args):
        return subprocess.run(["bash", str(self.repo / "scripts" / "sledge-local-postgres.sh"), *args],
                              capture_output=True, text=True, timeout=60,
                              env={"HOME": str(self.home), "USER": "alice", "SLEDGE_PG_PORT": str(self.port),
                                   "PATH": os.pathsep.join((self.pg_bin, self.tools)), **self.pg_env})

    def launch(self, host="localhost", secrets=None):
        conn = {"AIRFLOW__DATABASE__SQL_ALCHEMY_CONN":
                f"postgresql+psycopg2://alice:pw@{host}:{self.port}/airflow_alice"}
        env = {"HOME": str(self.home), "USER": "alice", "PATH": self.tools, "SLEDGE_NO_PGADMIN": "1",
               "PYTHONPATH": os.pathsep.join((str(self.stubs), str(fake_postgres.REPO)))}
        if secrets is None:
            env.update(conn)
        else:
            gpg = fake_postgres.executable(self.tmp / "gpgbin" / "gpg", fake_postgres.GPG)
            secrets_file = self.tmp / "secrets.env.gpg"
            secrets_file.write_text("".join(f"{k}={v}\n" for k, v in {**conn, **secrets}.items()))
            env.update(PATH=os.pathsep.join((str(gpg.parent), self.tools)), SLEDGE_SECRETS_FILE=str(secrets_file))
        res = subprocess.run([sys.executable, str(self.repo / "scripts" / "airflow-standalone-ldap.py")],
                             capture_output=True, text=True, timeout=120, cwd=str(self.home), env=env)
        assert "[stub] airflow imported" in res.stdout, res.stdout + res.stderr
        return [line for line in res.stdout.splitlines() if line.startswith("[postgres]")]

    def starts(self):
        return [c for c in fake_postgres.calls(self.log, "pg_ctl") if c.endswith(" start")]


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_a_stopped_local_postgres_starts_before_airflow_and_only_then(tmp_path, tools, host) -> None:
    launch = Launch(tmp_path, tools)
    assert launch.launch(host) == []
    assert fake_postgres.calls(launch.log) == []
    assert launch.local_postgres("setup").returncode == 0
    assert launch.local_postgres("stop").returncode == 0
    assert launch.launch(host) == [f"[postgres] started your local Postgres in {launch.root}"]
    assert len(launch.starts()) == 2
    assert "SQL_ALCHEMY_CONN" not in (launch.root / "data" / "postmaster.env").read_text()
    assert launch.local_postgres("status").returncode == 0
    assert launch.launch(host) == []
    assert len(launch.starts()) == 2


def test_a_local_postgres_dir_named_in_the_secrets_is_started(tmp_path, tools) -> None:
    launch = Launch(tmp_path, tools, pg_dir=tmp_path / "scratch" / "pg")
    assert launch.local_postgres("setup").returncode == 0
    assert launch.local_postgres("stop").returncode == 0
    assert launch.launch(secrets={"SLEDGE_PG_DIR": str(launch.root)}) == \
        [f"[postgres] started your local Postgres in {launch.root}"]
    assert launch.local_postgres("status").returncode == 0


def test_a_conn_to_another_host_never_starts_it(tmp_path, tools) -> None:
    launch = Launch(tmp_path, tools)
    assert launch.local_postgres("setup").returncode == 0
    assert launch.local_postgres("stop").returncode == 0
    calls = fake_postgres.calls(launch.log)
    assert launch.launch(host="db.example.edu") == []
    assert fake_postgres.calls(launch.log) == calls
    assert launch.local_postgres("status").returncode == 3


def test_a_failed_start_says_why_and_the_launch_goes_on(tmp_path, tools) -> None:
    launch = Launch(tmp_path, tools)
    assert launch.local_postgres("setup").returncode == 0
    assert launch.local_postgres("stop").returncode == 0
    with socket.socket() as other:
        other.bind(("127.0.0.1", launch.port))
        other.listen()
        [line] = launch.launch()
    assert line.startswith("[postgres] WARNING: could not start your local Postgres in ")
    assert f"port {launch.port} is in use by another program" in line
    assert len(launch.starts()) == 1
