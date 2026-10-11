import os
import shutil
import subprocess
import sys

import pytest

from hammer.vlsi import pd_store

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                      "scripts", "sledge-secrets-create.sh")
FAKE_GPG = '#!/bin/sh\nwhile [ "$#" -gt 1 ]; do\n    [ "$1" = -o ] && out="$2"\n    shift\ndone\ncat "$1" > "$out"\n'
FAKE_PYTHON = "#!/bin/sh\necho stub\n"
PG_VARS = ("HAMMER_PG_HOST", "HAMMER_PG_PORT", "HAMMER_PG_DB", "HAMMER_PG_USER", "HAMMER_PG_PASSWORD",
           "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN")

needs_bash = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="sledge-secrets-create.sh needs bash")


def _executable(path, text) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _create_secrets(tmp_path, user="alice", password="pw") -> dict:
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / ".venv" / "bin").mkdir(parents=True)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    with open(SCRIPT) as src:
        _executable(checkout / "scripts" / "sledge-secrets-create.sh", src.read().replace("\r\n", "\n"))
    _executable(checkout / ".venv" / "bin" / "python", FAKE_PYTHON)
    _executable(fake_bin / "gpg", FAKE_GPG)
    secrets = tmp_path / "secrets.env.gpg"
    subprocess.run(["bash", str(checkout / "scripts" / "sledge-secrets-create.sh")],
                   input=f"\n\n\n\n{password}\n{password}\n", text=True, capture_output=True,
                   check=True, timeout=60,
                   env={"HOME": str(tmp_path), "USER": user, "TMPDIR": str(tmp_path),
                        "PATH": os.pathsep.join((str(fake_bin), "/usr/bin", "/bin")),
                        "SLEDGE_SECRETS_FILE": str(secrets)})
    return dict(line.split("=", 1) for line in secrets.read_text().splitlines())


@pytest.fixture
def nothing_configured(tmp_path, monkeypatch):
    for var in PG_VARS:
        monkeypatch.delenv(var, raising=False)
    airflow_home = tmp_path / "airflow_home"
    airflow_home.mkdir()
    (airflow_home / "airflow.cfg").write_text("[database]\nsql_alchemy_conn = \n")
    monkeypatch.setenv("AIRFLOW_HOME", str(airflow_home))
    monkeypatch.setenv("PGPASSWORD", "pw")


@needs_bash
def test_fresh_install_points_airflow_at_the_local_postgres(tmp_path) -> None:
    written = _create_secrets(tmp_path)
    assert written["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] == \
        "postgresql+psycopg2://alice:pw@localhost:5544/airflow_alice"


def test_cache_with_nothing_configured_uses_the_local_postgres(nothing_configured) -> None:
    settings = pd_store._pg_settings()
    assert (settings["host"], settings["port"], settings["dbname"]) == ("localhost", 5544, "sledgehammer_studio")


@needs_bash
def test_fresh_install_puts_the_cache_and_airflow_on_one_postgres(tmp_path, nothing_configured, monkeypatch) -> None:
    for key, value in _create_secrets(tmp_path).items():
        monkeypatch.setenv(key, value)
    airflow = pd_store._parse_conn_uri(os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"])
    cache = pd_store._pg_settings()
    assert (cache["host"], cache["port"]) == (airflow["host"], airflow["port"]) == ("localhost", 5544)
