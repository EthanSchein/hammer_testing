import configparser
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

from hammer.shell import sledgehammer_cli
from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

SIMPLE = "airflow.api_fastapi.auth.managers.simple.simple_auth_manager.SimpleAuthManager"

# Airflow and psycopg2 stand-ins: the full stack dumps its environment and prints the login line, lite hands off.
STUBS = {
    "airflow/__init__.py": "",
    "airflow/cli/__init__.py": "",
    "airflow/cli/commands/__init__.py": "",
    "airflow/cli/commands/standalone_command.py":
        "import json, os\n"
        "class StandaloneCommand:\n"
        "    def print_output(self, name, text):\n"
        "        print(f'{name} | {text}')\n"
        "    def run(self):\n"
        "        json.dump(dict(os.environ), open(os.environ['ENV_DUMP'], 'w'))\n"
        "        self.find_user_info()\n",
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
SUPERVISOR = '#!/usr/bin/env bash\nenv > "$LITE_ENV_DUMP"\n'


def _launch(tmp_path, **env):
    repo = tmp_path / "checkout"
    if not repo.exists():
        fake_postgres.checkout(repo, "airflow-standalone-ldap.py")
        fake_postgres.executable(repo / "scripts" / "sledge-lite-supervisor.sh", SUPERVISOR)
    stubs = tmp_path / "stubs"
    for rel, text in STUBS.items():
        (stubs / rel).parent.mkdir(parents=True, exist_ok=True)
        (stubs / rel).write_text(text)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    dump = tmp_path / "env.json"
    full_env = {"HOME": str(home), "USER": "alice", "PATH": os.environ["PATH"], "SLEDGE_NO_PGADMIN": "1",
                "ENV_DUMP": str(dump), "LITE_ENV_DUMP": str(tmp_path / "lite.env"),
                "SLEDGE_PORT": str(fake_postgres.free_port()),
                "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": f"sqlite:///{tmp_path}/airflow.db",
                "PYTHONPATH": os.pathsep.join((str(stubs), str(fake_postgres.REPO))), **env}
    res = subprocess.run([sys.executable, str(repo / "scripts" / "airflow-standalone-ldap.py")],
                         capture_output=True, text=True, timeout=180, cwd=str(home), env=full_env)
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout, repo / ".sledgehammer" / "airflow-ui-password.json"


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_the_launching_user_is_the_only_login_with_a_private_password(tmp_path) -> None:
    out, passwords = _launch(tmp_path)
    seen = json.loads((tmp_path / "env.json").read_text())
    assert seen["AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS"] == "alice:admin"
    assert seen["AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE"] == str(passwords)
    stored = json.loads(passwords.read_text())
    assert list(stored) == ["alice"] and len(stored["alice"]) >= 20
    assert _mode(passwords) == 0o600 and _mode(passwords.parent) == 0o700
    assert f"standalone | Log in as alice; your password is in {passwords} (edit that file to change it)." in out
    assert stored["alice"] not in out


def test_the_password_stays_across_launches_and_edits(tmp_path) -> None:
    _, passwords = _launch(tmp_path)
    first = json.loads(passwords.read_text())["alice"]
    _launch(tmp_path)
    assert json.loads(passwords.read_text()) == {"alice": first}
    passwords.write_text(json.dumps({"alice": "chosen by alice"}))
    passwords.chmod(0o644)
    _launch(tmp_path)
    assert json.loads(passwords.read_text()) == {"alice": "chosen by alice"}
    assert _mode(passwords) == 0o600


@pytest.mark.parametrize("damaged", ["not json", "[]", json.dumps({"bob": "x"}), json.dumps({"alice": ""})])
def test_a_password_file_without_one_for_the_user_gets_one(tmp_path, damaged) -> None:
    (tmp_path / "checkout" / ".sledgehammer").mkdir(parents=True)
    fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py")
    fake_postgres.executable(tmp_path / "checkout" / "scripts" / "sledge-lite-supervisor.sh", SUPERVISOR)
    passwords = tmp_path / "checkout" / ".sledgehammer" / "airflow-ui-password.json"
    passwords.write_text(damaged)
    _launch(tmp_path)
    stored = json.loads(passwords.read_text())
    assert stored["alice"] and _mode(passwords) == 0o600


def test_lite_names_the_same_login(tmp_path) -> None:
    out, passwords = _launch(tmp_path, SLEDGE_LITE="1")
    seen = dict(line.split("=", 1) for line in (tmp_path / "lite.env").read_text().splitlines() if "=" in line)
    assert seen["AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS"] == "alice:admin"
    assert f"[lite] Log in as alice; your password is in {passwords} (edit that file to change it)." in out
    assert list(json.loads(passwords.read_text())) == ["alice"]


def test_the_checkout_config_uses_the_simple_login_on_this_host_only() -> None:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(fake_postgres.REPO / "airflow.cfg")
    assert cfg["core"]["auth_manager"] == SIMPLE
    assert cfg["core"]["simple_auth_manager_users"] == "${USER}:admin"
    assert cfg["core"]["simple_auth_manager_all_admins"] == "False"
    assert cfg["core"]["simple_auth_manager_passwords_file"] == \
        "${AIRFLOW_HOME}/.sledgehammer/airflow-ui-password.json"
    assert cfg["api"]["host"] == "127.0.0.1"


def test_a_dry_run_launch_needs_nothing_set(tmp_path, monkeypatch, capsys) -> None:
    for name in ("SLEDGE_2FA", "SLEDGE_LITE"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path))
    monkeypatch.setenv("SLEDGE_DRYRUN", "1")
    monkeypatch.setattr(sledgehammer_cli.sys, "argv", ["sledgehammer"])
    assert sledgehammer_cli.main() == 0
    assert "[dryrun] launch" in capsys.readouterr().out
