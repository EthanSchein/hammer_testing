import os
import shutil
import subprocess
import sys

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

# Airflow and psycopg2 stand-ins: the full stack prints its ready lines, lite hands off to a no-op supervisor.
STUBS = {
    "airflow/__init__.py": "",
    "airflow/cli/__init__.py": "",
    "airflow/cli/commands/__init__.py": "",
    "airflow/cli/commands/standalone_command.py":
        "class StandaloneCommand:\n"
        "    def print_output(self, name, text):\n"
        "        print(f'{name} | {text}')\n"
        "    def print_ready(self):\n"
        "        print('standalone | Airflow is ready')\n"
        "    def run(self):\n"
        "        self.print_ready()\n",
    "airflow/executors/__init__.py": "",
    "airflow/executors/executor_constants.py": "LOCAL_EXECUTOR = 'LocalExecutor'\n",
    "airflow/executors/executor_loader.py":
        "class ExecutorLoader:\n"
        "    @staticmethod\n"
        "    def import_default_executor_cls():\n"
        "        return type('E', (), {'is_local': True}), None\n",
    "airflow/utils/__init__.py": "",
    "airflow/utils/db.py": "def initdb():\n    pass\n",
    "psycopg2/__init__.py":
        "class OperationalError(Exception):\n"
        "    pass\n"
        "def connect(*a, **k):\n"
        "    raise OperationalError('no server here')\n",
    "psycopg2/extras.py": "Json = object\n",
}


def _launch(tmp_path, port, **env) -> list:
    repo = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py")
    fake_postgres.executable(repo / "scripts" / "sledge-lite-supervisor.sh", "#!/usr/bin/env bash\n")
    stubs = tmp_path / "stubs"
    for rel, text in STUBS.items():
        (stubs / rel).parent.mkdir(parents=True, exist_ok=True)
        (stubs / rel).write_text(text)
    home = tmp_path / "home"
    home.mkdir()
    full_env = {"HOME": str(home), "USER": "alice", "PATH": os.environ["PATH"], "SLEDGE_NO_PGADMIN": "1",
                "SLEDGE_PORT": str(port), "AIRFLOW__API__PORT": str(port),
                "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": f"sqlite:///{tmp_path}/airflow.db",
                "PYTHONPATH": os.pathsep.join((str(stubs), str(fake_postgres.REPO))), **env}
    res = subprocess.run([sys.executable, str(repo / "scripts" / "airflow-standalone-ldap.py")],
                         capture_output=True, text=True, timeout=180, cwd=str(home), env=full_env)
    assert res.returncode == 0, res.stdout + res.stderr
    return [line for line in res.stdout.splitlines() if "Tunnel" in line]


@pytest.mark.parametrize("lite, prefix", [("1", "[lite] "), ("0", "standalone | ")], ids=["lite", "full"])
def test_the_tunnel_hint_names_no_guessed_host(tmp_path, lite, prefix) -> None:
    port = fake_postgres.free_port()
    lines = _launch(tmp_path, port, SLEDGE_LITE=lite)
    assert lines == [f"{prefix}Tunnel from your laptop: ssh -L {port}:localhost:{port} <the host you ssh to>"]
    assert "alice@" not in lines[0]


@pytest.mark.parametrize("lite, prefix", [("1", "[lite] "), ("0", "standalone | ")], ids=["lite", "full"])
def test_sledge_ssh_host_makes_the_tunnel_hint_a_command_that_works(tmp_path, lite, prefix) -> None:
    port = fake_postgres.free_port()
    lines = _launch(tmp_path, port, SLEDGE_LITE=lite, SLEDGE_SSH_HOST=" eecs151-vm ")
    assert lines == [f"{prefix}Tunnel from your laptop: ssh -L {port}:localhost:{port} eecs151-vm"]
