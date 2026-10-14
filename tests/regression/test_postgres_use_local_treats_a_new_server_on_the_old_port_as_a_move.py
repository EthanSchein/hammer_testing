import os
import shutil
import socket
import subprocess
import sys

import pytest

from tests.utils import fake_postgres
from tests.utils.postgres_switch import CONN, PASSPHRASE, SECRETS, Sandbox

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

FIRST = "Switch SledgeHammer to your local Postgres? [y/N]: "
SECOND = 'Type "local" to confirm the switch: '


def warning(box, port=None) -> str:
    port = port or box.port
    return (f"WARNING: DAG run history and cached results stay on the Postgres that served localhost:{port} "
            f"before. Your local Postgres in {box.pg_root} starts empty unless it already holds them, and it takes "
            f"over localhost:{port}, so the old one cannot start while yours runs. To merge them later")


def in_use(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            socket.create_connection((host, port), timeout=1).close()
            return True
        except OSError:
            pass
    return False


def set_up_cluster(box, pg_dir) -> None:
    script = str(box.repo / "scripts" / "sledge-local-postgres.sh")
    env = dict(os.environ, SLEDGE_PG_DIR=str(pg_dir))
    assert subprocess.run(["bash", script, "setup"], env=env, capture_output=True, timeout=60).returncode == 0


def assert_secrets_untouched(box) -> None:
    assert box.secrets.read_text() == box.before
    assert not box.prev.exists() and not box.target.exists()
    assert [c for c in box.gpg_calls() if "--symmetric" in c["args"]] == []


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    """Secrets naming a stopped Postgres on SLEDGE_PG_PORT, and no cluster in SLEDGE_PG_DIR yet."""
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    sandbox.before = SECRETS.replace("@barney.example.edu:5544", f"@localhost:{sandbox.port}")
    sandbox.secrets.write_text(sandbox.before)
    yield sandbox
    sandbox.restore()


@pytest.fixture
def other(tmp_path, monkeypatch, capsys, tools):
    """Secrets naming an older, stopped cluster, and another cluster set up on its port in SLEDGE_PG_DIR."""
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    older = tmp_path / "older"
    set_up_cluster(sandbox, older)
    password = (older / "password").read_text().strip()
    sandbox.before = SECRETS.replace("@barney.example.edu:5544", f"@localhost:{sandbox.port}") \
        .replace("oldpw-1234", password)
    sandbox.secrets.write_text(sandbox.before)
    set_up_cluster(sandbox, sandbox.pg_root)
    monkeypatch.setenv("SLEDGE_PG_DIR", str(sandbox.pg_root))
    yield sandbox
    sandbox.restore()


@pytest.fixture
def same(tmp_path, monkeypatch, capsys, tools):
    """A cluster set up in SLEDGE_PG_DIR, with Airflow's connection already on it."""
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    set_up_cluster(sandbox, sandbox.pg_root)
    sandbox.connected = SECRETS.replace("@barney.example.edu:5544", f"@localhost:{sandbox.port}") \
        .replace("oldpw-1234@", f"{sandbox.local_password()}@")
    yield sandbox
    sandbox.restore()


@pytest.fixture
def landing(tmp_path, monkeypatch, capsys, tools):
    """Secrets naming a stopped Postgres on the next port, and SLEDGE_PG_PORT taken after the questions."""
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    if in_use(sandbox.port + 1):
        sandbox.restore()
        pytest.skip("the port after SLEDGE_PG_PORT is in use here")
    sandbox.before = SECRETS.replace("@barney.example.edu:5544", f"@localhost:{sandbox.port + 1}")
    sandbox.secrets.write_text(sandbox.before)
    taken = []
    ask = sandbox._input

    def answer(prompt=""):
        reply = ask(prompt)
        if prompt == SECOND and not taken:
            taken.append(socket.create_server(("127.0.0.1", sandbox.port)))
        return reply

    monkeypatch.setattr("builtins.input", answer)
    yield sandbox
    for s in taken:
        s.close()
    sandbox.restore()


def test_the_plan_warns_that_history_stays_with_the_old_server(box) -> None:
    rc, out, _ = box.run("use", "local", answers=[PASSPHRASE, "n"])
    assert rc == 1
    plan = out[:out.index(FIRST)]
    assert f"  Airflow  alice@localhost:{box.port}/airflow_alice\n" \
           f"       ->  alice@localhost:{box.port}/airflow_alice\n" in plan
    assert warning(box) in plan
    assert f"Your local Postgres is created in {box.pg_root} on port {box.port} and started." in plan
    assert_secrets_untouched(box)


@pytest.mark.parametrize("second", ["no", "y"])
def test_anything_but_local_at_the_second_question_changes_nothing(box, second) -> None:
    rc, out, _ = box.run("use", "local", answers=[PASSPHRASE, "y", second])
    assert rc == 1
    assert "Nothing changed." in out
    assert [p for _, p in box.prompts] == [f"GPG passphrase for {box.secrets}: ", FIRST, SECOND]
    assert_secrets_untouched(box)
    assert box.calls("initdb") == [] and box.calls("pg_ctl") == []


def test_two_yeses_switch_to_the_new_cluster(box) -> None:
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE, "y", "local"])
    assert rc == 0, out + err
    assert [p for _, p in box.prompts] == [f"GPG passphrase for {box.secrets}: ", FIRST, SECOND]
    password = box.local_password()
    assert box.secrets_lines()[CONN] == f"postgresql+psycopg2://alice:{password}@localhost:{box.port}/airflow_alice"
    assert box.prev.read_text() == box.before
    assert password not in out + err


def test_a_dry_run_warns_and_creates_nothing(box, monkeypatch) -> None:
    monkeypatch.setenv("SLEDGE_DRYRUN", "1")
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE])
    assert rc == 0, out + err
    assert warning(box) in out
    assert_secrets_untouched(box)
    assert box.calls("initdb") == [] and box.calls("pg_ctl") == []
    assert not (box.pg_root / "data").exists()


def test_another_cluster_on_the_same_port_gets_the_warning_and_both_questions(other) -> None:
    rc, out, _ = other.run("use", "local", answers=[PASSPHRASE, "y", "no"])
    assert rc == 1
    assert [p for _, p in other.prompts] == [f"GPG passphrase for {other.secrets}: ", FIRST, SECOND]
    assert warning(other) in out
    assert f"Your local Postgres in {other.pg_root} is already set up" in out
    assert_secrets_untouched(other)
    rc, out, err = other.run("use", "local", answers=[PASSPHRASE, "y", "local"])
    assert rc == 0, out + err
    password = other.local_password()
    assert other.secrets_lines()[CONN] == \
        f"postgresql+psycopg2://alice:{password}@localhost:{other.port}/airflow_alice"


def test_a_setup_that_lands_on_the_old_port_shows_the_warning_and_asks_both_again(landing) -> None:
    rc, out, _ = landing.run("use", "local", answers=[PASSPHRASE, "y", "local", "y", "no"])
    assert rc == 1
    assert [p for _, p in landing.prompts] == [f"GPG passphrase for {landing.secrets}: ",
                                               FIRST, SECOND, FIRST, SECOND]
    first, again = out.split("This switch changes:")[1:]
    assert f"stay on localhost:{landing.port + 1}. localhost:{landing.port} starts empty" in first
    assert warning(landing, landing.port + 1) in again
    assert_secrets_untouched(landing)


def test_only_the_password_line_to_refresh_asks_once(same) -> None:
    same.secrets.write_text(same.connected)
    rc, out, err = same.run("use", "local", answers=[PASSPHRASE, "y"])
    assert rc == 0, out + err
    assert [p for _, p in same.prompts] == [f"GPG passphrase for {same.secrets}: ", FIRST]
    assert "WARNING" not in out
    assert same.secrets_lines()["HAMMER_PG_PASSWORD"] == same.local_password()


def test_a_cache_already_on_the_local_postgres_is_not_left_behind(same) -> None:
    same.secrets.write_text(f"{SECRETS}HAMMER_PG_HOST=localhost\nHAMMER_PG_PORT={same.port}\n")
    rc, out, _ = same.run("use", "local", answers=[PASSPHRASE, "y", "no"])
    assert rc == 1
    assert [p for _, p in same.prompts] == [f"GPG passphrase for {same.secrets}: ", FIRST, SECOND]
    assert f"WARNING: DAG run history stays on barney.example.edu:5544. localhost:{same.port} starts empty" in out
    assert "cached results stay" not in out


def test_a_first_setup_with_nothing_configured_asks_once(tmp_path, monkeypatch, capsys, tools) -> None:
    if in_use(5544):
        pytest.skip("port 5544 is in use here")
    box = Sandbox(tmp_path, monkeypatch, capsys, tools, secrets=None)
    monkeypatch.setenv("SLEDGE_PG_PORT", "5544")
    try:
        rc, out, err = box.run("use", "local", answers=["y"])
        assert rc == 0, out + err
        assert box.prompts == [("input", FIRST)]
        assert "WARNING" not in out
        assert box.target_lines()["HAMMER_PG_PORT"] == "5544"
    finally:
        box.restore()
