import os
import shutil
import subprocess
import sys
import time

import pytest

from hammer.shell import sledgehammer_cli
from hammer.vlsi import pd_store
from tests.utils import fake_postgres
from tests.utils.postgres_switch import PASSPHRASE, FakePsycopg2, Sandbox

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths and shell quoting")

CLEARED = ("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "HAMMER_PG_HOST", "HAMMER_PG_PORT", "HAMMER_PG_DB",
           "HAMMER_PG_USER", "HAMMER_PG_PASSWORD", "PGPASSWORD", "PGPASSFILE", "PGHOSTADDR", "PGSERVICE",
           "SLEDGE_PG_DIR", "SLEDGE_ENV_FILE")
# conftest swaps the lookup out for every test; these tests need the real one.
REAL_FIND = sledgehammer_cli._find_stack_env
VENV = "Run \"source venv.sh\" to load HAMMER_PG_PASSWORD from the encrypted secrets"
TAIL = ", export HAMMER_PG_PASSWORD, or add a ~/.pgpass line for this server; \"sledgehammer postgres show\" " \
       "says where each setting comes from."


@pytest.fixture
def shell(tmp_path, monkeypatch):
    """A shell with no cache password anywhere and no encrypted secrets, on a cache server that is not a local Postgres."""
    for var in CLEARED:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    (home / ".sledgehammer").mkdir(parents=True)
    airflow_home = tmp_path / "airflow_home"
    airflow_home.mkdir()
    (airflow_home / "airflow.cfg").write_text("[database]\nsql_alchemy_conn = \n")
    work = tmp_path / "work" / "vlsi"
    work.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AIRFLOW_HOME", str(airflow_home))
    monkeypatch.setenv("SLEDGE_SECRETS_FILE", str(tmp_path / "private" / "airflow-secrets.env.gpg"))
    monkeypatch.setenv("HAMMER_PG_HOST", "db.example.edu")
    monkeypatch.chdir(work)
    monkeypatch.setattr(pd_store, "psycopg2", FakePsycopg2())
    _real_lookup(monkeypatch, home)
    return home


def _real_lookup(monkeypatch, home) -> None:
    monkeypatch.setattr(sledgehammer_cli, "_find_stack_env", REAL_FIND)
    monkeypatch.setattr(sledgehammer_cli, "STACK_ENV_HOME", str(home / ".sledgehammer" / "env.sh"))


def _message() -> str:
    with pytest.raises(pd_store.DatabaseUnavailable) as raised:
        pd_store._connect()
    return str(raised.value)


def test_the_home_env_file_is_named_instead_of_venv_sh(shell) -> None:
    (shell / ".sledgehammer" / "env.sh").write_text("export HAMMER_PG_HOST=db.example.edu\n")
    assert _message() == ("No Postgres password found. Run \"source ~/.sledgehammer/env.sh\" if it sets "
                          "HAMMER_PG_PASSWORD" + TAIL)


def test_sledge_env_file_is_named_first(shell, tmp_path, monkeypatch) -> None:
    (shell / ".sledgehammer" / "env.sh").write_text("")
    chosen = tmp_path / "stacks" / "course_env.sh"
    chosen.parent.mkdir()
    chosen.write_text("")
    monkeypatch.setenv("SLEDGE_ENV_FILE", str(chosen))
    assert _message().startswith(f"No Postgres password found. Run \"source {chosen}\" if it sets HAMMER_PG_PASSWORD")


def test_a_stack_env_sh_above_the_current_directory_is_named(shell, tmp_path) -> None:
    (tmp_path / "work" / "stack_env.sh").write_text("")
    assert _message().startswith(f"No Postgres password found. Run \"source {tmp_path / 'work' / 'stack_env.sh'}\"")


def test_without_an_env_file_the_hint_stays_venv_sh(shell) -> None:
    assert _message() == "No Postgres password found. " + VENV + TAIL


def test_with_encrypted_secrets_the_hint_stays_venv_sh_even_beside_an_env_file(shell, tmp_path, monkeypatch) -> None:
    (shell / ".sledgehammer" / "env.sh").write_text("export HAMMER_PD_CACHE=1\n")
    secrets = tmp_path / "private" / "airflow-secrets.env.gpg"
    secrets.parent.mkdir()
    secrets.write_text("encrypted\n")
    assert _message() == "No Postgres password found. " + VENV + TAIL


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_path_with_a_space_is_quoted_so_the_hint_runs_as_printed(shell, monkeypatch) -> None:
    env_file = shell / "My Stack" / "env.sh"
    env_file.parent.mkdir()
    env_file.write_text("echo loaded\n")
    monkeypatch.setenv("SLEDGE_ENV_FILE", str(env_file))
    message = _message()
    assert message.startswith("No Postgres password found. Run \"source ~/'My Stack/env.sh'\" if it sets")
    command = message.split("Run \"", 1)[1].split("\" if it sets", 1)[0]
    res = subprocess.run(["bash", "-c", command], capture_output=True, text=True, env={**os.environ, "HOME": str(shell)})
    assert res.stdout == "loaded\n", res.stderr


def test_a_home_of_slash_does_not_shorten_paths(shell, tmp_path, monkeypatch) -> None:
    env_file = tmp_path / "stacks" / "env.sh"
    env_file.parent.mkdir()
    env_file.write_text("")
    monkeypatch.setenv("SLEDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("HOME", "/")
    assert _message().startswith(f"No Postgres password found. Run \"source {env_file}\" if it sets")


def _local_with_unusable_password_file(box) -> None:
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE, "y", "local"])
    assert rc == 0, out + err
    (box.pg_root / "data" / "postmaster.pid").write_text(f"{os.getpid()}\n")
    box.db.serve("localhost", box.port, box.local_password(), "airflow_alice", "sledgehammer_studio")
    (box.pg_root / "password").chmod(0o644)
    (box.home / ".sledgehammer" / "env.sh").write_text("# course settings\n")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_postgres_show_names_the_env_file_when_there_are_no_encrypted_secrets(tmp_path, monkeypatch, capsys) -> None:
    box = Sandbox(tmp_path, monkeypatch, capsys, fake_postgres.system_tools(tmp_path / "tools"))
    _real_lookup(monkeypatch, box.home)
    try:
        _local_with_unusable_password_file(box)
        box.secrets.unlink()
        rc, out, err = box.run("show", answers=[PASSPHRASE])
        assert rc == 0, err
        assert ("would take one from HAMMER_PG_PASSWORD, which \"source ~/.sledgehammer/env.sh\" loads if that file "
                f"sets it, or from a line for localhost:{box.port} in ~/.pgpass") in out
        assert "source venv.sh" not in out
    finally:
        box.restore()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_postgres_show_keeps_venv_sh_when_the_secrets_hold_the_password(tmp_path, monkeypatch, capsys) -> None:
    box = Sandbox(tmp_path, monkeypatch, capsys, fake_postgres.system_tools(tmp_path / "tools"))
    _real_lookup(monkeypatch, box.home)
    try:
        _local_with_unusable_password_file(box)
        rc, out, err = box.run("show", answers=[PASSPHRASE])
        assert rc == 0, err
        assert "which \"source venv.sh\" loads from the encrypted secrets" in out
        assert "\"source ~/.sledgehammer/env.sh\"" not in out
    finally:
        box.restore()


@pytest.mark.parametrize("line", ["export HAMMER_PG_PASSWORD=unused",
                                  "  AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=postgresql:///x"])
def test_an_env_file_that_sets_the_login_is_named_even_beside_encrypted_secrets(shell, tmp_path, line) -> None:
    (shell / ".sledgehammer" / "env.sh").write_text(f"{line}\n")
    secrets = tmp_path / "private" / "airflow-secrets.env.gpg"
    secrets.parent.mkdir()
    secrets.write_text("encrypted\n")
    assert _message().startswith("No Postgres password found. Run \"source ~/.sledgehammer/env.sh\" if it sets")


def test_the_checkouts_own_secrets_keep_venv_sh_when_sledge_secrets_file_names_a_missing_file(shell, tmp_path,
                                                                                              monkeypatch) -> None:
    (shell / ".sledgehammer" / "env.sh").write_text("export HAMMER_PD_CACHE=1\n")
    checkout_secrets = tmp_path / "checkout" / ".sledgehammer" / "airflow-secrets.env.gpg"
    checkout_secrets.parent.mkdir(parents=True)
    checkout_secrets.write_text("encrypted\n")
    monkeypatch.setattr(pd_store, "SECRETS_FILE", str(checkout_secrets))
    assert _message() == "No Postgres password found. " + VENV + TAIL


def test_a_long_run_of_blank_lines_in_the_env_file_does_not_stall_the_error(shell, tmp_path) -> None:
    (shell / ".sledgehammer" / "env.sh").write_text("export HAMMER_PG_HOST=db.example.edu\n" + "\n" * 50000)
    secrets = tmp_path / "private" / "airflow-secrets.env.gpg"
    secrets.parent.mkdir()
    secrets.write_text("encrypted\n")
    start = time.monotonic()
    assert _message() == "No Postgres password found. " + VENV + TAIL
    assert time.monotonic() - start < 5
