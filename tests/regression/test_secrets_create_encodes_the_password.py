import os
import shutil
import subprocess
import sys

import pytest

from hammer.vlsi import pd_store
from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="sledge-secrets-create.sh needs bash")


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


def _loaded(text: str) -> dict:
    """The secrets as the launcher loads them."""
    out = {}
    for line in text.splitlines():
        key, value = line.strip().split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


@pytest.mark.parametrize("password", ["p@ss/w:rd%#? it's café", ' "quoted" '])
def test_any_password_survives_the_conn_and_the_loaders(tmp_path, tools, password) -> None:
    checkout = fake_postgres.checkout(tmp_path / "checkout", "sledge-secrets-create.sh", venv=True,
                                      log=tmp_path / "calls.log")
    gpg = fake_postgres.executable(tmp_path / "gpgbin" / "gpg", fake_postgres.GPG)
    pw_file = tmp_path / "pw"
    pw_file.write_text(password + "\n", encoding="utf-8")
    secrets = tmp_path / "secrets.env.gpg"
    res = subprocess.run(["bash", str(checkout / "scripts" / "sledge-secrets-create.sh")],
                         text=True, capture_output=True, timeout=60,
                         env={"HOME": str(tmp_path), "USER": "alice", "TMPDIR": str(tmp_path), "LANG": "C.UTF-8",
                              "PATH": os.pathsep.join((str(gpg.parent), tools)),
                              "SLEDGE_SECRETS_FILE": str(secrets), "SLEDGE_PG_USER": "alice",
                              "SLEDGE_PG_DB": "airflow_alice", "SLEDGE_PG_HOST": "db.example.edu",
                              "SLEDGE_PG_PORT": "5544", "SLEDGE_PG_PASSWORD_FILE": str(pw_file)})
    assert res.returncode == 0, res.stderr
    loaded = _loaded(secrets.read_text(encoding="utf-8"))
    conn = pd_store._parse_conn_uri(loaded["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"])
    assert (conn["host"], conn["port"], conn["user"], conn["dbname"]) == \
        ("db.example.edu", 5544, "alice", "airflow_alice")
    assert conn["password"] == password
    assert loaded["HAMMER_PG_PASSWORD"] == password
