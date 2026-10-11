import shutil
import subprocess
import sys

import pytest

from tests.utils import fake_postgres
from tests.utils.postgres_switch import PASSPHRASE, SECRETS, Sandbox

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    """Secrets that already name the local Postgres, as the install-time setup leaves them, and no postgres.env."""
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    script = sandbox.repo / "scripts" / "sledge-local-postgres.sh"
    assert subprocess.run(["bash", str(script), "setup"], capture_output=True, timeout=60).returncode == 0
    password = sandbox.local_password()
    local = SECRETS.replace("@barney.example.edu:5544", f"@localhost:{sandbox.port}")
    sandbox.secrets.write_text(local.replace("oldpw-1234", password))
    sandbox.before = sandbox.secrets.read_text()
    yield sandbox
    sandbox.restore()


def test_one_question_then_only_postgres_env_is_written(box) -> None:
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE, "y"])
    assert rc == 0, out + err
    assert [p for _, p in box.prompts] == [f"GPG passphrase for {box.secrets}: ", f"Write {box.target}? [y/N]: "]
    assert f"Your encrypted secrets already point at your local Postgres in {box.pg_root}" in out
    assert "DAG run history" not in out
    assert box.secrets.read_text() == box.before and not box.prev.exists()
    assert box.target_lines() == {"HAMMER_PG_HOST": "localhost", "HAMMER_PG_PORT": str(box.port),
                                  "HAMMER_PG_USER": "alice", "HAMMER_PG_DB": "sledgehammer_studio",
                                  "SLEDGE_PG_DIR": str(box.pg_root)}
    assert box.local_password() not in out + err


def test_no_writes_nothing(box) -> None:
    rc, out, _ = box.run("use", "local", answers=[PASSPHRASE, "n"])
    assert rc == 1
    assert "Nothing changed." in out
    assert box.secrets.read_text() == box.before and not box.prev.exists() and not box.target.exists()
