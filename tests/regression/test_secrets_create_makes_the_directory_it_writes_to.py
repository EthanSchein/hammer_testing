import os
import shutil
import stat
import subprocess
import sys

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="sledge-secrets-create.sh needs bash")


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


def _create_secrets(tmp_path, tools, secrets=None, cwd=None):
    log = tmp_path / "calls.log"
    checkout = fake_postgres.checkout(tmp_path / "checkout", "sledge-secrets-create.sh", venv=True, log=log)
    logged = fake_postgres.GPG.replace("#!/bin/sh\n", f'#!/bin/sh\necho "gpg $*" >> "{log}"\n', 1)
    gpg = fake_postgres.executable(tmp_path / "gpgbin" / "gpg", logged)
    password = tmp_path / "pw"
    password.write_text("s3cret\n")
    env = {"HOME": str(tmp_path), "USER": "alice", "TMPDIR": str(tmp_path),
           "PATH": os.pathsep.join((str(gpg.parent), tools)), "SLEDGE_PG_USER": "alice",
           "SLEDGE_PG_DB": "airflow_alice", "SLEDGE_PG_HOST": "localhost", "SLEDGE_PG_PORT": "5544",
           "SLEDGE_PG_PASSWORD_FILE": str(password)}
    if secrets is not None:
        env["SLEDGE_SECRETS_FILE"] = str(secrets)
    res = subprocess.run(["bash", str(checkout / "scripts" / "sledge-secrets-create.sh")], stdin=subprocess.DEVNULL,
                         text=True, capture_output=True, timeout=60, env=env, cwd=cwd)
    return checkout, res


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_a_secrets_file_in_a_new_directory_is_written_there(tmp_path, tools) -> None:
    secrets = tmp_path / "elsewhere" / "nested" / "secrets.env.gpg"
    checkout, res = _create_secrets(tmp_path, tools, secrets)
    assert res.returncode == 0, res.stderr
    assert "HAMMER_PG_PASSWORD=s3cret" in secrets.read_text().splitlines()
    assert _mode(secrets.parent) == 0o700
    assert _mode(secrets) == 0o600
    assert not (checkout / ".sledgehammer").exists()


def test_an_existing_directory_you_chose_keeps_its_mode(tmp_path, tools) -> None:
    directory = tmp_path / "shared"
    directory.mkdir()
    directory.chmod(0o755)
    checkout, res = _create_secrets(tmp_path, tools, directory / "secrets.env.gpg")
    assert res.returncode == 0, res.stderr
    assert (directory / "secrets.env.gpg").is_file()
    assert _mode(directory) == 0o755
    assert not (checkout / ".sledgehammer").exists()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("named", [False, True])
def test_the_default_secrets_directory_is_made_private(tmp_path, tools, existing, named) -> None:
    default = tmp_path / "checkout" / ".sledgehammer"
    if existing:
        default.mkdir(parents=True)
        default.chmod(0o755)
    secrets = None
    if named:
        (tmp_path / "link").symlink_to(tmp_path / "checkout", target_is_directory=True)
        secrets = tmp_path / "link" / ".sledgehammer" / "airflow-secrets.env.gpg"
    _, res = _create_secrets(tmp_path, tools, secrets)
    assert res.returncode == 0, res.stderr
    assert (default / "airflow-secrets.env.gpg").is_file()
    assert _mode(default) == 0o700


def test_a_secrets_file_under_tilde_goes_in_home(tmp_path, tools) -> None:
    work = tmp_path / "work"
    work.mkdir()
    _, res = _create_secrets(tmp_path, tools, "~/tilde/secrets.env.gpg", cwd=work)
    assert res.returncode == 0, res.stderr
    secrets = tmp_path / "tilde" / "secrets.env.gpg"
    assert secrets.is_file()
    assert f"wrote {secrets}" in res.stdout
    assert os.listdir(work) == []
    _, again = _create_secrets(tmp_path, tools, "~/tilde/secrets.env.gpg", cwd=work)
    assert f"already present: {secrets}" in again.stdout


def test_an_unusable_secrets_directory_stops_before_any_question(tmp_path, tools) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    checkout, res = _create_secrets(tmp_path, tools, blocker / "secrets.env.gpg")
    assert res.returncode != 0
    assert "Postgres user" not in res.stdout
    assert fake_postgres.calls(tmp_path / "calls.log", "gpg") == []
    assert not (checkout / ".sledgehammer").exists()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can write to any directory")
def test_a_directory_you_cannot_write_stops_before_any_question(tmp_path, tools) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        _, res = _create_secrets(tmp_path, tools, locked / "secrets.env.gpg")
    finally:
        mode = _mode(locked)
        locked.chmod(0o700)
    assert res.returncode != 0
    assert f"cannot write to {locked}" in res.stderr
    assert "Postgres user" not in res.stdout
    assert fake_postgres.calls(tmp_path / "calls.log", "gpg") == []
    assert mode == 0o500
