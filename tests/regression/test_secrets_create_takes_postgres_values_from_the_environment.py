import os
import shutil
import subprocess
import sys

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="sledge-secrets-create.sh needs bash")


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


def _create_secrets(tmp_path, tools, stdin="", **given):
    checkout = fake_postgres.checkout(tmp_path / "checkout", "sledge-secrets-create.sh", venv=True,
                                      log=tmp_path / "calls.log")
    gpg_bin = tmp_path / "gpgbin"
    fake_postgres.executable(gpg_bin / "gpg", fake_postgres.GPG)
    secrets = tmp_path / "secrets.env.gpg"
    res = subprocess.run(["bash", str(checkout / "scripts" / "sledge-secrets-create.sh")],
                         input=stdin, text=True, capture_output=True, timeout=60,
                         env={"HOME": str(tmp_path), "USER": "alice", "TMPDIR": str(tmp_path),
                              "PATH": os.pathsep.join((str(gpg_bin), tools)),
                              "SLEDGE_SECRETS_FILE": str(secrets), **given})
    assert res.returncode == 0, res.stderr
    return dict(line.split("=", 1) for line in secrets.read_text().splitlines()), res


def test_given_values_answer_every_connection_prompt(tmp_path, tools) -> None:
    password = tmp_path / "pw"
    password.write_text("s3cret\n")
    written, res = _create_secrets(tmp_path, tools, SLEDGE_PG_USER="alice", SLEDGE_PG_DB="airflow_alice",
                                   SLEDGE_PG_HOST="localhost", SLEDGE_PG_PORT="5601",
                                   SLEDGE_PG_PASSWORD_FILE=str(password))
    assert written["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] == \
        "postgresql+psycopg2://alice:s3cret@localhost:5601/airflow_alice"
    assert written["HAMMER_PG_PASSWORD"] == "s3cret"
    assert "s3cret" not in res.stdout + res.stderr


def test_only_the_missing_values_are_asked_for(tmp_path, tools) -> None:
    written, res = _create_secrets(tmp_path, tools, stdin="\n\n\npw\npw\n", SLEDGE_PG_PORT="5601")
    assert written["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] == \
        "postgresql+psycopg2://alice:pw@localhost:5601/airflow_alice"
    assert "Postgres port: 5601 (from SLEDGE_PG_PORT)" in res.stdout
    assert "SLEDGE_PG_DIR" not in written


def test_the_local_postgres_hands_its_settings_to_the_secrets(tmp_path, tools) -> None:
    log = tmp_path / "calls.log"
    pg = fake_postgres.checkout(tmp_path / "pg-checkout", "sledge-local-postgres.sh")
    home = tmp_path / "home"
    home.mkdir()
    pg_env = {"HOME": str(home), "USER": "alice", "SLEDGE_PG_PORT": str(fake_postgres.free_port()),
              "PATH": os.pathsep.join((fake_postgres.postgres(tmp_path / "pgbin", log), tools))}
    script = str(pg / "scripts" / "sledge-local-postgres.sh")
    assert subprocess.run(["bash", script, "setup"], env=pg_env, capture_output=True, timeout=60).returncode == 0
    settings = dict(line.split("=", 1) for line in subprocess.run(
        ["bash", script, "env"], env=pg_env, capture_output=True, text=True, timeout=60).stdout.splitlines())
    written, res = _create_secrets(tmp_path, tools, **settings)
    password = (home / ".sledgehammer" / "postgres" / "password").read_text().strip()
    assert written["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] == \
        f"postgresql+psycopg2://alice:{password}@localhost:{settings['SLEDGE_PG_PORT']}/airflow_alice"
    assert written["HAMMER_PG_PASSWORD"] == password
    assert written["SLEDGE_PG_DIR"] == str(home / ".sledgehammer" / "postgres")
    assert password not in res.stdout + res.stderr


def test_a_failed_encryption_leaves_no_plaintext_behind(tmp_path, tools) -> None:
    checkout = fake_postgres.checkout(tmp_path / "checkout", "sledge-secrets-create.sh", venv=True,
                                      log=tmp_path / "calls.log")
    plaintext = tmp_path / "gpg-input"
    gpg = fake_postgres.executable(tmp_path / "gpgbin" / "gpg",
                                   f'#!/bin/sh\nfor a; do last="$a"; done\necho "$last" > "{plaintext}"\nexit 2\n')
    res = subprocess.run(["bash", str(checkout / "scripts" / "sledge-secrets-create.sh")],
                         input="\n\n\n\npw\npw\n", text=True, capture_output=True, timeout=60,
                         env={"HOME": str(tmp_path), "USER": "alice", "TMPDIR": str(tmp_path),
                              "PATH": os.pathsep.join((str(gpg.parent), tools)),
                              "SLEDGE_SECRETS_FILE": str(tmp_path / "secrets.env.gpg")})
    assert res.returncode == 2
    left = plaintext.read_text().strip()
    leaked = os.path.exists(left)
    if leaked:
        os.unlink(left)
    assert not leaked
