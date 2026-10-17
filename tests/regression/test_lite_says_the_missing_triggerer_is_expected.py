import os
import shutil
import subprocess
import sys

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

NOTE = ("The Health panel on the UI's home page shows the Triggerer as down; that is expected, "
        "since lite runs none and SledgeHammer needs none.")
# Airflow and psycopg2 stand-ins: the full stack prints that it ran, lite hands off to a no-op supervisor.
STUBS = {
    "airflow/__init__.py": "",
    "airflow/cli/__init__.py": "",
    "airflow/cli/commands/__init__.py": "",
    "airflow/cli/commands/standalone_command.py":
        "class StandaloneCommand:\n"
        "    def run(self):\n"
        "        print('[stub] full standalone ran')\n",
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


def _launch(tmp_path, lite: str) -> str:
    repo = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py")
    fake_postgres.executable(repo / "scripts" / "sledge-lite-supervisor.sh", "#!/usr/bin/env bash\n")
    stubs = tmp_path / "stubs"
    for rel, text in STUBS.items():
        (stubs / rel).parent.mkdir(parents=True, exist_ok=True)
        (stubs / rel).write_text(text)
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "USER": "alice", "PATH": os.environ["PATH"], "SLEDGE_NO_PGADMIN": "1",
           "SLEDGE_LITE": lite, "SLEDGE_PORT": str(fake_postgres.free_port()),
           "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": f"sqlite:///{tmp_path}/airflow.db",
           "PYTHONPATH": os.pathsep.join((str(stubs), str(fake_postgres.REPO)))}
    res = subprocess.run([sys.executable, str(repo / "scripts" / "airflow-standalone-ldap.py")],
                         capture_output=True, text=True, timeout=180, cwd=str(home), env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout


def test_lite_says_the_triggerer_shown_as_down_is_expected(tmp_path) -> None:
    assert f"[lite] {NOTE}\n" in _launch(tmp_path, "1")


def test_the_full_stack_runs_a_triggerer_and_says_nothing_about_it(tmp_path) -> None:
    out = _launch(tmp_path, "0")
    assert "[stub] full standalone ran" in out and "Triggerer" not in out
