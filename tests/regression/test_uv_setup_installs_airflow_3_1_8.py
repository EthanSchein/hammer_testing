import os
import shutil
import subprocess
import sys

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="uv_setup.sh needs bash")

CONSTRAINT = "https://raw.githubusercontent.com/apache/airflow/constraints-3.1.8/constraints-3.11.txt"

# Test names stay clear of conda, mamba, miniforge and pixi: uv_setup.sh drops PATH entries containing them.
STUBS = {
    "home/pg_local/usr/bin/pg_config": "#!/bin/sh\necho 'PostgreSQL 16 (stub)'\n",
    "checkout/tools/slang/slang": "#!/bin/sh\necho 'slang version 11.0.0 (stub)'\n",
    "checkout/.venv/bin/airflow": "#!/bin/sh\necho 3.1.8\n",
    "checkout/.venv/bin/sledgehammer": "#!/bin/sh\nexit 0\n",
}


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


def _setup(tmp_path, tools):
    log = tmp_path / "calls.log"
    scripts = [n for n in sorted(os.listdir(fake_postgres.REPO / "scripts"))
               if os.path.isfile(fake_postgres.REPO / "scripts" / n)]
    checkout = fake_postgres.checkout(tmp_path / "checkout", *scripts, venv=True, log=log)
    for rel, text in STUBS.items():
        fake_postgres.executable(tmp_path / rel, text)
    fake_postgres.executable(tmp_path / "bin" / "uv", f'#!/bin/sh\necho "uv $*" >> "{log}"\n'
                                                      '[ "$1" = --version ] && echo "uv 0.0.0 (stub)"\nexit 0\n')
    (checkout / ".venv" / "bin" / "activate").write_text(f'export PATH="{checkout}/.venv/bin:$PATH"\n')
    home = tmp_path / "home"
    (home / "libnsl_local" / "usr" / "lib64").mkdir(parents=True)
    (home / "libnsl_local" / "usr" / "lib64" / "libnsl.so.1").write_text("")
    (home / ".bashrc").write_text("")
    env = {"HOME": str(home), "USER": "alice", "TMPDIR": str(tmp_path),
           "PATH": os.pathsep.join((str(tmp_path / "bin"), tools)),
           "SLEDGE_NO_PLUGINS": "1", "SLEDGE_SECRETS_FILE": str(tmp_path / "secrets.env.gpg")}
    res = subprocess.run(["bash", str(checkout / "scripts" / "uv_setup.sh")], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, env=env, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    return res.stdout + res.stderr, fake_postgres.calls(log, "uv pip install")


def test_airflow_and_edge3_come_from_the_3_1_8_constraints(tmp_path, tools) -> None:
    out, installs = _setup(tmp_path, tools)
    assert f"uv pip install apache-airflow==3.1.8 asyncpg>=0.30.0,<1 --constraint {CONSTRAINT}" in installs
    assert f"uv pip install apache-airflow-providers-edge3==3.1.0 --constraint {CONSTRAINT}" in installs
    assert "=== airflow 3.1.8 ===" in out.splitlines()
