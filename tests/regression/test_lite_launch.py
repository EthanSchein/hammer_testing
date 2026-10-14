import os
import shutil
import socket
import subprocess
import sys

import pytest

from hammer.shell import sledgehammer_cli
from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

# Airflow and psycopg2 stand-ins: just enough for the launcher to reach its hand-off.
STUBS = {
    "airflow/__init__.py": "",
    "airflow/cli/__init__.py": "",
    "airflow/cli/commands/__init__.py": "",
    "airflow/cli/commands/standalone_command.py":
        "import os\n"
        "class StandaloneCommand:\n"
        "    def run(self):\n"
        "        print('[stub] full standalone ran', os.environ.get('AIRFLOW__CORE__PARALLELISM'),\n"
        "              os.environ.get('MALLOC_ARENA_MAX'))\n",
    "airflow/executors/__init__.py": "",
    "airflow/executors/executor_constants.py": "LOCAL_EXECUTOR = 'LocalExecutor'\n",
    "airflow/executors/executor_loader.py":
        "import os\n"
        "class ExecutorLoader:\n"
        "    @staticmethod\n"
        "    def import_default_executor_cls():\n"
        "        return type('E', (), {'is_local': os.environ.get('STUB_EXECUTOR') != 'edge'}), None\n",
    "airflow/utils/__init__.py": "",
    "airflow/utils/db.py": "def initdb():\n    print('[stub] initdb')\n",
    # STUB_HEARTBEAT: other (another host), same-then-gone (this host, stale after two checks), same-advancing.
    "psycopg2/__init__.py":
        "import datetime, os, socket\n"
        "class OperationalError(Exception):\n"
        "    pass\n"
        "class _Cursor:\n"
        "    rowcount = 0\n"
        "    def __enter__(self):\n"
        "        return self\n"
        "    def __exit__(self, *a):\n"
        "        return False\n"
        "    def execute(self, sql, args=None):\n"
        "        self.sql = sql\n"
        "    def fetchall(self):\n"
        "        mode = os.environ.get('STUB_HEARTBEAT')\n"
        "        if 'SchedulerJob' not in self.sql or not mode:\n"
        "            return []\n"
        "        path = os.environ['STUB_COUNT']\n"
        "        n = int(open(path).read()) + 1 if os.path.exists(path) else 1\n"
        "        open(path, 'w').write(str(n))\n"
        "        base = datetime.datetime(2026, 10, 13, 9, 0, 0)\n"
        "        if mode == 'other':\n"
        "            return [('other-host', base)]\n"
        "        if mode == 'same-then-gone':\n"
        "            return [(socket.getfqdn(), base)] if n <= 2 else []\n"
        "        return [(socket.getfqdn(), base + datetime.timedelta(seconds=n))]\n"
        "    def fetchone(self):\n"
        "        return None\n"
        "class _Conn:\n"
        "    def cursor(self):\n"
        "        return _Cursor()\n"
        "    def commit(self):\n"
        "        pass\n"
        "    def close(self):\n"
        "        pass\n"
        "def connect(*a, **k):\n"
        "    return _Conn()\n",
}
SUPERVISOR = '#!/usr/bin/env bash\nenv > "$LITE_ENV_DUMP"\necho "[stub] lite supervisor"\n'
LIMITS = ("AIRFLOW__CORE__PARALLELISM", "AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_SIZE",
          "AIRFLOW__DATABASE__SQL_ALCHEMY_MAX_OVERFLOW", "MALLOC_ARENA_MAX", "SLEDGE_SET_MALLOC_ARENA_MAX")
POSTGRES = "postgresql+psycopg2://alice:pw@localhost:1/airflow_alice"


def _launch(tmp_path, port=None, launch_env=None, **env):
    """Run a copy of the launcher; returns the result and the supervisor's environment, or None if it never got there."""
    repo = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py")
    fake_postgres.executable(repo / "scripts" / "sledge-lite-supervisor.sh", SUPERVISOR)
    if launch_env is not None:
        (repo / ".sledgehammer").mkdir()
        (repo / ".sledgehammer" / "launch.env").write_text(launch_env)
    stubs = tmp_path / "stubs"
    for rel, text in STUBS.items():
        (stubs / rel).parent.mkdir(parents=True, exist_ok=True)
        (stubs / rel).write_text(text)
    home = tmp_path / "home"
    home.mkdir()
    dump = tmp_path / "supervisor.env"
    full_env = {"HOME": str(home), "USER": "alice", "PATH": os.environ["PATH"], "SLEDGE_NO_PGADMIN": "1",
                "LITE_ENV_DUMP": str(dump), "STUB_COUNT": str(tmp_path / "heartbeat-checks"),
                "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": f"sqlite:///{tmp_path}/airflow.db",
                "PYTHONPATH": os.pathsep.join((str(stubs), str(fake_postgres.REPO)))}
    if port is not None:
        full_env["SLEDGE_PORT"] = str(port)
    full_env.update(env)
    res = subprocess.run([sys.executable, str(repo / "scripts" / "airflow-standalone-ldap.py")],
                         capture_output=True, text=True, timeout=180, cwd=str(home), env=full_env)
    seen = None
    if dump.exists():
        seen = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    return res, seen


@pytest.fixture
def busy_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        yield s.getsockname()[1]


def test_lite_initializes_the_database_and_hands_off_with_small_defaults(tmp_path) -> None:
    port = fake_postgres.free_port()
    res, env = _launch(tmp_path, port, SLEDGE_LITE="yes")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[stub] initdb" in res.stdout and "[stub] lite supervisor" in res.stdout
    assert "[stub] full standalone ran" not in res.stdout
    assert {k: env.get(k) for k in LIMITS} == {
        "AIRFLOW__CORE__PARALLELISM": "2", "AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_SIZE": "2",
        "AIRFLOW__DATABASE__SQL_ALCHEMY_MAX_OVERFLOW": "5", "MALLOC_ARENA_MAX": "2",
        "SLEDGE_SET_MALLOC_ARENA_MAX": "1"}
    assert env["SLEDGE_LITE"] == "1"
    assert env["AIRFLOW__API__PORT"] == str(port)
    assert env["AIRFLOW__API__BASE_URL"] == f"http://localhost:{port}"
    assert "AIRFLOW__CORE__EXECUTOR" not in env


def test_settings_already_in_the_environment_win(tmp_path) -> None:
    res, env = _launch(tmp_path, fake_postgres.free_port(), SLEDGE_LITE="1",
                       AIRFLOW__CORE__PARALLELISM="4", MALLOC_ARENA_MAX="8")
    assert res.returncode == 0, res.stdout + res.stderr
    assert env["AIRFLOW__CORE__PARALLELISM"] == "4" and env["MALLOC_ARENA_MAX"] == "8"
    assert "SLEDGE_SET_MALLOC_ARENA_MAX" not in env
    assert env["AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_SIZE"] == "2"


def test_a_checkout_set_up_for_lite_launches_lite(tmp_path) -> None:
    res, env = _launch(tmp_path, fake_postgres.free_port(), launch_env="# comment\nSLEDGE_LITE=1\n")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[stub] lite supervisor" in res.stdout and env["SLEDGE_LITE"] == "1"


def test_sledge_lite_0_overrides_the_checkout_default(tmp_path) -> None:
    res, env = _launch(tmp_path, fake_postgres.free_port(), launch_env="SLEDGE_LITE=1\n", SLEDGE_LITE="0")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[stub] full standalone ran None None" in res.stdout and env is None


def test_without_lite_the_full_launch_is_unchanged(tmp_path) -> None:
    res, env = _launch(tmp_path, fake_postgres.free_port())
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[stub] full standalone ran None None" in res.stdout
    assert env is None and "[stub] initdb" not in res.stdout


def test_a_busy_pinned_port_is_refused(tmp_path, busy_port) -> None:
    res, env = _launch(tmp_path, busy_port, SLEDGE_LITE="1")
    assert res.returncode != 0 and f"port {busy_port} is already in use" in res.stderr
    assert env is None and "[stub] initdb" not in res.stdout


def test_a_busy_configured_port_is_refused_not_moved(tmp_path, busy_port) -> None:
    res, env = _launch(tmp_path, SLEDGE_LITE="1", AIRFLOW__API__PORT=str(busy_port))
    assert res.returncode != 0 and f"port {busy_port} is already in use" in res.stderr
    assert "auto-selected" not in res.stdout and env is None


def test_a_numeric_sledge_port_wins_over_airflow_api_port(tmp_path, busy_port) -> None:
    port = fake_postgres.free_port()
    res, env = _launch(tmp_path, port, SLEDGE_LITE="1", AIRFLOW__API__PORT=str(busy_port))
    assert res.returncode == 0, res.stdout + res.stderr
    assert env["AIRFLOW__API__PORT"] == str(port)


def test_a_live_scheduler_on_another_host_is_refused(tmp_path) -> None:
    res, env = _launch(tmp_path, fake_postgres.free_port(), SLEDGE_LITE="1", STUB_HEARTBEAT="other",
                       AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=POSTGRES)
    assert res.returncode != 0
    assert "another scheduler is already heartbeating" in res.stderr and "other-host" in res.stderr
    assert env is None and "[stub] initdb" not in res.stdout


def test_this_hosts_crashed_scheduler_is_waited_out(tmp_path) -> None:
    res, env = _launch(tmp_path, fake_postgres.free_port(), SLEDGE_LITE="1", STUB_HEARTBEAT="same-then-gone",
                       AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=POSTGRES)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Waiting for that" in res.stdout and "[stub] lite supervisor" in res.stdout


def test_a_scheduler_on_this_host_that_still_beats_is_refused(tmp_path) -> None:
    res, env = _launch(tmp_path, fake_postgres.free_port(), SLEDGE_LITE="1", STUB_HEARTBEAT="same-advancing",
                       AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=POSTGRES)
    assert res.returncode != 0 and "another scheduler is already heartbeating" in res.stderr
    assert env is None


def test_a_non_local_default_executor_becomes_local(tmp_path) -> None:
    res, env = _launch(tmp_path, fake_postgres.free_port(), SLEDGE_LITE="1", STUB_EXECUTOR="edge")
    assert res.returncode == 0, res.stdout + res.stderr
    assert env["AIRFLOW__CORE__EXECUTOR"] == "LocalExecutor"


@pytest.mark.parametrize("word, value", [("lite", "1"), ("full", "0")])
def test_the_launch_words_choose_the_stack(tmp_path, monkeypatch, capsys, word, value) -> None:
    monkeypatch.setenv("SLEDGE_LITE", "")
    monkeypatch.delenv("SLEDGE_LITE")
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path))
    monkeypatch.setenv("SLEDGE_DRYRUN", "1")
    monkeypatch.setattr(sledgehammer_cli.sys, "argv", ["sledgehammer", word])
    assert sledgehammer_cli.main() == 0
    out = capsys.readouterr().out
    assert f"SLEDGE_LITE={value}" in out and sledgehammer_cli.LAUNCHER in out
