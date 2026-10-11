import os
import shutil
import subprocess
import sys

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="sledge-local-postgres.sh needs bash")

# Stands in for Airflow: ends the launch at its import and reports what the DAG tasks would inherit.
STANDALONE_STUB = ("import os\n"
                   "print('[stub] HAMMER_PG_DB=' + os.environ.get('HAMMER_PG_DB', ''))\n"
                   "raise SystemExit(0)\n")


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


def test_the_launch_takes_the_cache_database_and_the_local_postgres_from_postgres_env(tmp_path, tools) -> None:
    log = tmp_path / "calls.log"
    repo = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py", "sledge-local-postgres.sh")
    home = tmp_path / "home"
    home.mkdir()
    pg_dir = tmp_path / "scratch" / "pg"
    port = fake_postgres.free_port()
    pg_path = os.pathsep.join((fake_postgres.postgres(tmp_path / "pgbin", log), tools))
    script = str(repo / "scripts" / "sledge-local-postgres.sh")
    pg_env = {"HOME": str(home), "USER": "alice", "SLEDGE_PG_PORT": str(port), "PATH": pg_path,
              "SLEDGE_PG_DIR": str(pg_dir)}
    for step in ("setup", "stop"):
        assert subprocess.run(["bash", script, step], env=pg_env, capture_output=True, timeout=60).returncode == 0
    stubs = tmp_path / "stubs"
    for pkg in ("airflow", "airflow/cli", "airflow/cli/commands"):
        (stubs / pkg).mkdir(parents=True)
        (stubs / pkg / "__init__.py").write_text("")
    (stubs / "airflow" / "cli" / "commands" / "standalone_command.py").write_text(STANDALONE_STUB)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    secrets = private / "airflow-secrets.env.gpg"
    secrets.write_text(f"AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=postgresql+psycopg2://alice:pw@localhost:{port}/airflow_alice\n"
                       "HAMMER_PG_PASSWORD=pw\n"
                       f"SLEDGE_PG_DIR={home / 'elsewhere'}\n")
    (private / "postgres.env").write_text(f"HAMMER_PG_HOST=localhost\nHAMMER_PG_PORT={port}\nHAMMER_PG_USER=alice\n"
                                          f"HAMMER_PG_DB=cache_x\nSLEDGE_PG_DIR={pg_dir}\n")
    gpg = fake_postgres.executable(tmp_path / "gpgbin" / "gpg", fake_postgres.GPG)
    res = subprocess.run([sys.executable, str(repo / "scripts" / "airflow-standalone-ldap.py")],
                         capture_output=True, text=True, timeout=120, cwd=str(home),
                         env={"HOME": str(home), "USER": "alice", "SLEDGE_NO_PGADMIN": "1",
                              "PATH": os.pathsep.join((str(gpg.parent), pg_path)),
                              "SLEDGE_SECRETS_FILE": str(secrets),
                              "PYTHONPATH": os.pathsep.join((str(stubs), str(fake_postgres.REPO)))})
    assert "[stub] HAMMER_PG_DB=cache_x" in res.stdout, res.stdout + res.stderr
    assert f"[postgres] started your local Postgres in {pg_dir}" in res.stdout
    assert not (home / "elsewhere").exists()
