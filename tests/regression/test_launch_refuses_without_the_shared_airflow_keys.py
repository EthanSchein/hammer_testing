import os
import shutil
import subprocess
import sys

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

CONN = "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"
JWT = "AIRFLOW__API_AUTH__JWT_SECRET"
SECRET_KEY = "AIRFLOW__API__SECRET_KEY"
# The keys as the checkout's airflow.cfg ships them: present but blank.
BLANK_CFG = "[core]\nfernet_key = \n[api]\nsecret_key = \n[api_auth]\njwt_secret = \n"
# Airflow and psycopg2 stand-ins: each stack says when it would start its processes.
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
    "airflow/utils/db.py": "def initdb():\n    print('[stub] initdb')\n",
    "psycopg2/__init__.py":
        "class OperationalError(Exception):\n"
        "    pass\n"
        "def connect(*a, **k):\n"
        "    raise OperationalError('no server here')\n",
    "psycopg2/extras.py": "Json = object\n",
}
STARTED = ("[stub] lite supervisor", "[stub] full standalone ran", "[stub] initdb", "[port] serving")


def _launch(tmp_path, lite: str, cfg: str = BLANK_CFG, secrets=None, **env):
    """Run a copy of the launcher whose environment holds no Airflow keys but env and, through gpg, secrets."""
    repo = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py")
    fake_postgres.executable(repo / "scripts" / "sledge-lite-supervisor.sh",
                             '#!/usr/bin/env bash\necho "[stub] lite supervisor"\n')
    (repo / "airflow.cfg").write_text(cfg)
    stubs = tmp_path / "stubs"
    for rel, text in STUBS.items():
        (stubs / rel).parent.mkdir(parents=True, exist_ok=True)
        (stubs / rel).write_text(text)
    home = tmp_path / "home"
    home.mkdir()
    full_env = {"HOME": str(home), "USER": "alice", "PATH": os.environ["PATH"], "SLEDGE_NO_PGADMIN": "1",
                "SLEDGE_LITE": lite, "SLEDGE_PORT": str(fake_postgres.free_port()),
                CONN: f"sqlite:///{tmp_path}/airflow.db",
                "PYTHONPATH": os.pathsep.join((str(stubs), str(fake_postgres.REPO)))}
    if secrets is not None:
        gpg = fake_postgres.executable(tmp_path / "gpgbin" / "gpg", fake_postgres.GPG)
        secrets_file = tmp_path / "secrets.env.gpg"
        secrets_file.write_text("".join(f"{k}={v}\n" for k, v in {CONN: full_env.pop(CONN), **secrets}.items()))
        full_env.update(PATH=os.pathsep.join((str(gpg.parent), full_env["PATH"])),
                        SLEDGE_SECRETS_FILE=str(secrets_file))
    full_env.update(env)
    return subprocess.run([sys.executable, str(repo / "scripts" / "airflow-standalone-ldap.py")],
                          capture_output=True, text=True, timeout=180, cwd=str(home), env=full_env)


def _refusal(res) -> str:
    assert res.returncode != 0, res.stdout + res.stderr
    assert not [marker for marker in STARTED if marker in res.stdout], res.stdout
    lines = [line for line in res.stderr.splitlines() if line.startswith("[secrets] ERROR:")]
    assert len(lines) == 1, res.stderr
    assert " Run \"source ~/.sledgehammer/env.sh\", or add " in lines[0]
    assert lines[0].endswith(" to the encrypted secrets that venv.sh loads, then start sledgehammer again.")
    return lines[0]


def test_lite_without_the_jwt_secret_starts_nothing(tmp_path) -> None:
    line = _refusal(_launch(tmp_path, "1"))
    assert line.startswith(f"[secrets] ERROR: {JWT} is not set in the environment or airflow.cfg, so each Airflow "
                           "process would make up its own and they would reject each other's requests with 403.")
    assert SECRET_KEY not in line


@pytest.mark.parametrize("env, named", [
    ({}, f"{JWT} and {SECRET_KEY} are not set"),
    ({JWT: "jwt"}, f"{SECRET_KEY} is not set"),
    ({SECRET_KEY: "key"}, f"{JWT} is not set"),
], ids=["neither", "no-secret-key", "no-jwt-secret"])
def test_the_full_stack_also_needs_the_secret_key_its_log_servers_check(tmp_path, env, named) -> None:
    assert _refusal(_launch(tmp_path, "0", **env)).startswith(f"[secrets] ERROR: {named} in the environment")


@pytest.mark.parametrize("lite, cfg, env", [
    ("1", BLANK_CFG, {JWT: "jwt"}),
    ("0", BLANK_CFG, {JWT: "jwt", SECRET_KEY: "key"}),
    ("1", "[api_auth]\njwt_secret = jwt\n", {}),
    ("0", "[api]\nsecret_key = key\n[api_auth]\njwt_secret = jwt\n", {}),
    ("0", "[api]\nsecret_key_secret = key\n[api_auth]\njwt_private_key_path = /keys/jwt.pem\n", {}),
    ("0", BLANK_CFG, {JWT + "_CMD": "echo jwt", "AIRFLOW__WEBSERVER__SECRET_KEY": "key"}),
], ids=["lite-env", "full-env", "lite-cfg", "full-cfg", "cfg-private-key-and-backend", "env-cmd-and-webserver"])
def test_keys_from_the_environment_or_airflow_cfg_let_it_start(tmp_path, lite, cfg, env) -> None:
    res = _launch(tmp_path, lite, cfg, **env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert ("[stub] lite supervisor" if lite == "1" else "[stub] full standalone ran") in res.stdout
    assert "[secrets] ERROR" not in res.stderr


def test_keys_from_the_encrypted_secrets_let_it_start(tmp_path) -> None:
    res = _launch(tmp_path, "0", secrets={JWT: "jwt", SECRET_KEY: "key"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "[secrets] loaded 3 secret(s)" in res.stdout and "[stub] full standalone ran" in res.stdout
