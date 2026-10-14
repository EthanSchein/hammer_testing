import os
import shutil
import subprocess
import sys
from urllib.parse import quote

import pytest

from hammer.shell import sledgehammer_postgres
from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="the gpg stand-in and venv.sh need a POSIX shell")

SCRIPT = fake_postgres.REPO / "scripts" / "sledge-secrets-init.py"
RAW = ["SeKrit/tail", "SeKrit?tail", "SeKrit#tail", "Se/Kr?it#tail"]
ENCODED_ONLY = [" Se'Krit ", '"SeKrit"', "Se@Kr:i%t"]
CFG = """[core]
fernet_key = fern3tKey=
internal_api_secret_key = intern4lKey
[database]
sql_alchemy_conn = {conn}
[api]
secret_key = s3cretKey
[api_auth]
jwt_secret = jw7Key
"""
# Stands in for the real airflow binary that venv.sh's airflow function runs after loading the secrets.
AIRFLOW = '#!/bin/sh\nprintf "%s" "$HAMMER_PG_PASSWORD"\n'


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


def _conn(password: str) -> str:
    return f"postgresql+psycopg2://alice:{password}@localhost:5544/airflow_alice"


def _init(tmp_path, tools, conn, script=SCRIPT, answer="", **env):
    cfg = tmp_path / "airflow.cfg"
    cfg.write_text(CFG.format(conn=conn))
    secrets = tmp_path / "checkout" / ".sledgehammer" / "airflow-secrets.env.gpg"
    gpg = fake_postgres.executable(tmp_path / "gpgbin" / "gpg", fake_postgres.GPG)
    res = subprocess.run([sys.executable, str(script)], input=answer, text=True, capture_output=True, timeout=120,
                         env={"HOME": str(tmp_path), "AIRFLOW_CONFIG": str(cfg), "SLEDGE_SECRETS_FILE": str(secrets),
                              "PATH": os.pathsep.join((str(gpg.parent), tools)), **env})
    return res, cfg, secrets


def _venv_sh_password(tmp_path, tools) -> str:
    checkout = tmp_path / "checkout"
    with open(fake_postgres.REPO / "venv.sh") as src:
        (checkout / "venv.sh").write_text(src.read().replace("\r\n", "\n"))
    venv_bin = checkout / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text(f'export PATH="{venv_bin}:$PATH"\n')
    fake_postgres.executable(venv_bin / "airflow", AIRFLOW)
    res = subprocess.run(["bash", "-c", "source ./venv.sh >/dev/null 2>&1 && airflow"], cwd=str(checkout),
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                         env={"HOME": str(tmp_path), "PATH": os.pathsep.join((str(tmp_path / "gpgbin"), tools))})
    assert res.returncode == 0, res.stderr
    return res.stdout


@pytest.mark.parametrize("in_conn,password",
                         [(p, p) for p in RAW] + [(quote(p, safe=""), p) for p in RAW + ENCODED_ONLY])
def test_the_cache_password_is_the_conn_password_as_both_loaders_read_it(tmp_path, tools, in_conn, password) -> None:
    res, _, secrets = _init(tmp_path, tools, _conn(in_conn))
    assert res.returncode == 0, res.stderr
    assert password not in res.stdout + res.stderr and in_conn not in res.stdout + res.stderr
    assert sledgehammer_postgres._parse_env(secrets.read_text())["HAMMER_PG_PASSWORD"] == password
    assert _venv_sh_password(tmp_path, tools) == password


def test_the_other_secrets_and_the_cfg_blanking_are_unchanged(tmp_path, tools) -> None:
    res, cfg, secrets = _init(tmp_path, tools, _conn("SeKrit"), answer="y\n")
    assert res.returncode == 0, res.stderr
    assert secrets.read_text() == (
        "# Airflow secrets, loaded into the env at launch. Never commit.\n\n"
        "AIRFLOW__CORE__FERNET_KEY=fern3tKey=\n"
        "AIRFLOW__CORE__INTERNAL_API_SECRET_KEY=intern4lKey\n"
        f"AIRFLOW__DATABASE__SQL_ALCHEMY_CONN={_conn('SeKrit')}\n"
        "AIRFLOW__API__SECRET_KEY=s3cretKey\n"
        "AIRFLOW__API_AUTH__JWT_SECRET=jw7Key\n\n"
        "HAMMER_PG_PASSWORD=SeKrit\n")
    assert secrets.stat().st_mode & 0o777 == 0o600
    assert sorted(os.listdir(secrets.parent)) == ["airflow-secrets.env.gpg"]
    assert cfg.read_text() == "".join(line.split(" = ")[0] + " = \n" if " = " in line else line + "\n"
                                      for line in CFG.splitlines())
    assert "Blanked 5 secret(s)" in res.stdout and "Krit" not in res.stdout + res.stderr


def test_a_conn_without_a_password_writes_no_cache_password(tmp_path, tools) -> None:
    res, cfg, secrets = _init(tmp_path, tools, "postgresql+psycopg2://alice@localhost:5544/airflow_alice")
    assert res.returncode == 0, res.stderr
    assert "HAMMER_PG_PASSWORD" not in secrets.read_text()
    assert "Left airflow.cfg untouched" in res.stdout and "airflow_alice" in cfg.read_text()


def test_it_reads_the_conn_with_the_hammer_of_its_own_checkout(tmp_path, tools) -> None:
    other = tmp_path / "other" / "hammer" / "vlsi"
    other.mkdir(parents=True)
    (other / "__init__.py").write_text("raise ImportError('another checkout')\n")
    res, _, secrets = _init(tmp_path, tools, _conn("Se/Kr?it#tail"), PYTHONPATH=str(tmp_path / "other"))
    assert res.returncode == 0, res.stderr
    assert sledgehammer_postgres._parse_env(secrets.read_text())["HAMMER_PG_PASSWORD"] == "Se/Kr?it#tail"


def test_without_hammer_it_stops_before_writing_anything(tmp_path, tools) -> None:
    checkout = fake_postgres.checkout(tmp_path / "bare", "sledge-secrets-init.py")
    (checkout / "hammer").mkdir()
    (checkout / "hammer" / "__init__.py").write_text("raise ImportError('hammer is not installed')\n")
    res, cfg, secrets = _init(tmp_path, tools, _conn("Se/Kr?it#tail"),
                              script=checkout / "scripts" / "sledge-secrets-init.py", answer="y\n")
    assert res.returncode == 1
    assert f"could not import hammer from {checkout} (hammer is not installed)" in res.stderr
    assert "Krit" not in res.stdout + res.stderr
    assert not secrets.parent.exists()
    assert cfg.read_text() == CFG.format(conn=_conn("Se/Kr?it#tail"))
