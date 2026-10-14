import re
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
PLAN = "This switch changes:"
ARROW = re.compile(r"^       ->  alice@localhost:(\d+)/airflow_alice$", re.M)


def listen(port: int, family=socket.AF_INET, host: str = "127.0.0.1") -> socket.socket:
    s = socket.socket(family)
    try:
        s.bind((host, port))
    except OSError:
        s.close()
        raise
    s.listen()
    return s


def planned(text: str) -> int:
    ports = {int(port) for port in ARROW.findall(text)}
    assert len(ports) == 1, text
    return ports.pop()


def assert_secrets_untouched(box, before: str) -> None:
    assert box.secrets.read_text() == before
    assert not box.prev.exists() and not box.target.exists()
    assert [c for c in box.gpg_calls() if "--symmetric" in c["args"]] == []


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    """Secrets naming an older local Postgres that still listens on SLEDGE_PG_PORT, and no cluster in SLEDGE_PG_DIR yet."""
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    sandbox.before = SECRETS.replace("@barney.example.edu:5544", f"@localhost:{sandbox.port}")
    sandbox.secrets.write_text(sandbox.before)
    older = listen(sandbox.port)
    yield sandbox
    older.close()
    sandbox.restore()


@pytest.fixture
def racing(tmp_path, monkeypatch, capsys, tools):
    """Another program takes SLEDGE_PG_PORT after the questions and before the setup."""
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    taken = []
    ask = sandbox._input

    def answer(prompt=""):
        reply = ask(prompt)
        if prompt == SECOND and not taken:
            taken.append(listen(sandbox.port))
        return reply

    monkeypatch.setattr("builtins.input", answer)
    yield sandbox
    for s in taken:
        s.close()
    sandbox.restore()


@pytest.fixture
def running(tmp_path, monkeypatch, capsys, tools):
    """A cluster already set up in SLEDGE_PG_DIR and listening on its port, with the secrets on another server."""
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    script = sandbox.repo / "scripts" / "sledge-local-postgres.sh"
    assert subprocess.run(["bash", str(script), "setup"], capture_output=True, timeout=60).returncode == 0
    up = listen(sandbox.port)
    yield sandbox
    up.close()
    sandbox.restore()


@pytest.fixture
def ipv6(tmp_path, monkeypatch, capsys, tools):
    """Another program listens on SLEDGE_PG_PORT on ::1 only, and no cluster in SLEDGE_PG_DIR yet."""
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    try:
        other = listen(sandbox.port, socket.AF_INET6, "::1")
    except OSError:
        sandbox.restore()
        pytest.skip("no IPv6 loopback here")
    yield sandbox
    other.close()
    sandbox.restore()


def test_the_plan_names_the_free_port_and_warns_before_the_first_question(box) -> None:
    rc, out, _ = box.run("use", "local", answers=[PASSPHRASE, "n"])
    assert rc == 1
    plan = out[:out.index(FIRST)]
    port = planned(plan)
    assert port != box.port
    assert f"  Airflow  alice@localhost:{box.port}/airflow_alice\n" in plan
    assert f"  cache    alice@localhost:{box.port}/sledgehammer_studio\n" \
           f"       ->  alice@localhost:{port}/sledgehammer_studio" in plan
    assert f"WARNING: DAG run history and cached results stay on localhost:{box.port}. " \
           f"localhost:{port} starts empty" in plan
    assert f"Your local Postgres is created in {box.pg_root} on port {port} and started." in plan
    assert_secrets_untouched(box, box.before)


@pytest.mark.parametrize("second", ["no", "", "y"])
def test_anything_but_local_at_the_second_question_changes_nothing(box, second) -> None:
    rc, out, _ = box.run("use", "local", answers=[PASSPHRASE, "y", second])
    assert rc == 1
    assert "Nothing changed." in out
    assert [p for _, p in box.prompts] == [f"GPG passphrase for {box.secrets}: ", FIRST, SECOND]
    assert_secrets_untouched(box, box.before)
    assert box.calls("initdb") == [] and box.calls("pg_ctl") == []


def test_two_yeses_switch_to_the_port_the_plan_showed(box) -> None:
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE, "y", "local"])
    assert rc == 0, out + err
    assert [p for _, p in box.prompts] == [f"GPG passphrase for {box.secrets}: ", FIRST, SECOND]
    port = planned(out)
    password = box.local_password()
    assert box.secrets_lines()[CONN] == f"postgresql+psycopg2://alice:{password}@localhost:{port}/airflow_alice"
    assert box.target_lines()["HAMMER_PG_PORT"] == str(port)
    assert box.prev.read_text() == box.before
    assert password not in out + err


def test_a_dry_run_shows_the_free_port_and_creates_nothing(box, monkeypatch) -> None:
    monkeypatch.setenv("SLEDGE_DRYRUN", "1")
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE, "y", "local"])
    assert rc == 0, out + err
    port = planned(out)
    assert port != box.port
    assert f"WARNING: DAG run history and cached results stay on localhost:{box.port}." in out
    assert len(box.prompts) == 1
    assert_secrets_untouched(box, box.before)
    assert box.calls("initdb") == [] and box.calls("pg_ctl") == []
    assert not (box.pg_root / "data").exists()


def test_a_port_taken_before_the_setup_shows_the_switch_again(racing) -> None:
    rc, out, _ = racing.run("use", "local", answers=[PASSPHRASE, "y", "local", "y", "no"])
    assert rc == 1
    assert [p for _, p in racing.prompts] == [f"GPG passphrase for {racing.secrets}: ",
                                              FIRST, SECOND, FIRST, SECOND]
    first, again = out.split(PLAN)[1:]
    assert planned(first) == racing.port
    port = planned(again)
    assert port != racing.port
    assert f"Your local Postgres was set up as alice@localhost:{port}/airflow_alice, not " \
           f"alice@localhost:{racing.port}/airflow_alice, so check the switch again." in first
    assert "WARNING: DAG run history and cached results stay on barney.example.edu:5544." in again
    assert f"The secrets and {racing.target} are unchanged" in again
    assert_secrets_untouched(racing, SECRETS)


def test_yeses_to_the_second_plan_switch_to_the_port_it_shows(racing) -> None:
    rc, out, err = racing.run("use", "local", answers=[PASSPHRASE, "y", "local", "y", "local"])
    assert rc == 0, out + err
    port = planned(out.split(PLAN)[2])
    assert port != racing.port
    password = racing.local_password()
    assert racing.secrets_lines()[CONN] == f"postgresql+psycopg2://alice:{password}@localhost:{port}/airflow_alice"
    assert racing.target_lines()["HAMMER_PG_PORT"] == str(port)
    assert racing.prev.read_text() == SECRETS


def test_a_running_cluster_is_planned_on_its_own_port(running) -> None:
    rc, out, err = running.run("use", "local", answers=[PASSPHRASE, "y", "local"])
    assert rc == 0, out + err
    assert [p for _, p in running.prompts] == [f"GPG passphrase for {running.secrets}: ", FIRST, SECOND]
    assert out.count(PLAN) == 1 and planned(out) == running.port
    assert f"Your local Postgres in {running.pg_root} is already set up" in out
    password = running.local_password()
    assert running.secrets_lines()[CONN] == \
        f"postgresql+psycopg2://alice:{password}@localhost:{running.port}/airflow_alice"


def test_a_listener_on_the_ipv6_loopback_alone_moves_the_plan_on(ipv6) -> None:
    rc, out, err = ipv6.run("use", "local", answers=[PASSPHRASE, "y", "local"])
    assert rc == 0, out + err
    assert [p for _, p in ipv6.prompts] == [f"GPG passphrase for {ipv6.secrets}: ", FIRST, SECOND]
    port = planned(out)
    assert out.count(PLAN) == 1 and port != ipv6.port
    assert ipv6.target_lines()["HAMMER_PG_PORT"] == str(port)


def test_a_cluster_set_up_under_another_user_name_shows_the_switch_again(tmp_path, monkeypatch, capsys,
                                                                         tools) -> None:
    login = subprocess.run(["id", "-un"], capture_output=True, text=True)
    me = login.stdout.strip()
    if login.returncode != 0 or me in ("", "alice"):
        pytest.skip("needs a login name other than alice")
    box = Sandbox(tmp_path, monkeypatch, capsys, tools)
    monkeypatch.delenv("USER")
    try:
        rc, out, _ = box.run("use", "local", answers=[PASSPHRASE, "y", "local", "y", "no"])
        assert rc == 1
        assert [p for _, p in box.prompts] == [f"GPG passphrase for {box.secrets}: ", FIRST, SECOND, FIRST, SECOND]
        first, again = out.split(PLAN)[1:]
        assert f"       ->  alice@localhost:{box.port}/airflow_alice\n" in first
        assert f"Your local Postgres was set up as {me}@localhost:{box.port}/airflow_{me}, not " \
               f"alice@localhost:{box.port}/airflow_alice, so check the switch again." in first
        assert f"       ->  {me}@localhost:{box.port}/airflow_{me}\n" in again
        assert_secrets_untouched(box, SECRETS)
    finally:
        box.restore()
