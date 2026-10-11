import os
import shutil
import sys
from pathlib import Path

import pytest

from hammer.shell import sledgehammer_cli
from hammer.vlsi import pd_store
from tests.utils import fake_postgres
from tests.utils.postgres_switch import CONN, OLD_CONN, PASSPHRASE, SECRETS, Sandbox, mode

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

NEW_PASSWORD = "n3w p@ss/w:rd%#?"
FIELDS = ["db2.example.edu", "6543", "", "", "cache_x"]
FIRST = "Switch SledgeHammer to db2.example.edu:6543? [y/N]: "
SECOND = "Your DAG run history and cached results stay on barney.example.edu:5544. Switch anyway? [y/N]: "


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    sandbox.db.serve("db2.example.edu", 6543, NEW_PASSWORD, "airflow_alice", "cache_x")
    yield sandbox
    sandbox.restore()


def assert_nothing_changed(box) -> None:
    assert box.secrets.read_text() == SECRETS
    assert not box.prev.exists() and not box.target.exists()


def test_fields_are_asked_in_order_with_the_current_values_as_defaults(box) -> None:
    rc, out, err = box.run("configure", answers=[PASSPHRASE, *FIELDS, "first", "second",
                                                 NEW_PASSWORD, NEW_PASSWORD, "y", "y"])
    assert rc == 0, out + err
    assert box.prompts == [
        ("getpass", f"GPG passphrase for {box.secrets}: "),
        ("input", "  host [barney.example.edu]: "),
        ("input", "  port [5544]: "),
        ("input", "  user [alice]: "),
        ("input", "  Airflow database [airflow_alice]: "),
        ("input", "  cache database [sledgehammer_studio]: "),
        ("getpass", "  password: "), ("getpass", "  password again: "),
        ("getpass", "  password: "), ("getpass", "  password again: "),
        ("input", FIRST),
        ("input", SECOND),
    ]
    assert "empty or mismatched; try again" in out
    assert "  Airflow database airflow_alice: reachable yes" in out
    assert "  cache database cache_x: reachable yes" in out


def test_a_switch_rewrites_the_two_lines_under_the_same_passphrase(box) -> None:
    rc, out, err = box.run("configure", answers=[PASSPHRASE, *FIELDS, NEW_PASSWORD, NEW_PASSWORD, "y", "y"])
    assert rc == 0, out + err
    written = box.secrets.read_text().splitlines()
    assert written[0] == f"{CONN}=postgresql+psycopg2://alice:n3w%20p%40ss%2Fw%3Ard%25%23%3F@db2.example.edu:6543/airflow_alice"
    assert written[-1] == f"HAMMER_PG_PASSWORD={NEW_PASSWORD}"
    assert written[1:-1] == SECRETS.splitlines()[1:-1]
    parsed = pd_store._parse_conn_uri(written[0].split("=", 1)[1])
    assert (parsed["host"], parsed["port"], parsed["user"], parsed["password"]) == \
        ("db2.example.edu", 6543, "alice", NEW_PASSWORD)
    assert box.prev.read_text() == SECRETS
    assert mode(box.secrets) == 0o600 and mode(box.prev) == 0o600
    assert box.target_lines() == {"HAMMER_PG_HOST": "db2.example.edu", "HAMMER_PG_PORT": "6543",
                                  "HAMMER_PG_USER": "alice", "HAMMER_PG_DB": "cache_x"}
    assert {c["passphrase"] for c in box.gpg_calls()} == {PASSPHRASE}
    assert NEW_PASSWORD not in out + err and "oldpw" not in out + err


def test_a_failed_connection_test_can_stop_with_nothing_changed(box) -> None:
    rc, out, _ = box.run("configure", answers=[PASSPHRASE, *FIELDS, "wrong", "wrong", "n"])
    assert rc == 1
    assert "  Airflow database airflow_alice: reachable no (" in out
    assert "password authentication failed" in out
    assert box.prompts[-1] == ("input", "Continue anyway? [y/N]: ")
    assert "Nothing changed." in out
    assert_nothing_changed(box)


def test_a_failed_connection_test_can_go_on_to_the_two_questions(box) -> None:
    rc, out, _ = box.run("configure", answers=[PASSPHRASE, "nowhere.example.edu", *FIELDS[1:],
                                               "pw", "pw", "y", "y", "y"])
    assert rc == 0
    assert "Connection refused" in out
    assert [p for _, p in box.prompts[-3:]] == ["Continue anyway? [y/N]: ",
                                                "Switch SledgeHammer to nowhere.example.edu:6543? [y/N]: ", SECOND]
    assert box.target_lines()["HAMMER_PG_HOST"] == "nowhere.example.edu"


@pytest.mark.parametrize("confirm", [["n"], ["y", "n"], ["y", ""]])
def test_either_no_aborts_with_nothing_changed(box, confirm) -> None:
    rc, out, _ = box.run("configure", answers=[PASSPHRASE, *FIELDS, NEW_PASSWORD, NEW_PASSWORD, *confirm])
    assert rc == 1
    assert "Nothing changed." in out
    assert_nothing_changed(box)


def test_bad_values_are_asked_again(box) -> None:
    rc, out, _ = box.run("configure", answers=[PASSPHRASE, "db2.example.edu", "port", "70000", "6543",
                                               "", "", "cache_x", NEW_PASSWORD, NEW_PASSWORD, "y", "y"])
    assert rc == 0
    assert out.count("a port number from 1 to 65535") == 2
    assert box.target_lines()["HAMMER_PG_PORT"] == "6543"


def test_with_no_secrets_yet_they_are_created_and_the_password_file_is_gone(tmp_path, monkeypatch, capsys,
                                                                            tools) -> None:
    box = Sandbox(tmp_path, monkeypatch, capsys, tools, secrets=None)
    box.db.serve("db2.example.edu", 6543, NEW_PASSWORD, "airflow_alice", "cache_x")
    shm = Path("/dev/shm")
    before = set(shm.glob("sledge-pg-*")) if shm.is_dir() else set()
    try:
        rc, out, err = box.run("configure", answers=[*FIELDS, NEW_PASSWORD, NEW_PASSWORD, "y", "y"])
        assert rc == 0, out + err
        assert [p for _, p in box.prompts[:5]] == ["  host [localhost]: ", "  port [5544]: ", "  user [alice]: ",
                                                   "  Airflow database [airflow_alice]: ",
                                                   "  cache database [sledgehammer_studio]: "]
        written = box.secrets_lines()
        parsed = pd_store._parse_conn_uri(written[CONN])
        assert (parsed["host"], parsed["port"], parsed["password"]) == ("db2.example.edu", 6543, NEW_PASSWORD)
        assert written["HAMMER_PG_PASSWORD"] == NEW_PASSWORD
        assert "SLEDGE_PG_DIR" not in written
        assert box.target_lines()["HAMMER_PG_DB"] == "cache_x"
        assert (set(shm.glob("sledge-pg-*")) if shm.is_dir() else set()) == before
        assert NEW_PASSWORD not in out + err
    finally:
        box.restore()


def test_the_warning_and_the_plan_precede_the_questions(box) -> None:
    rc, out, _ = box.run("configure", answers=[PASSPHRASE, *FIELDS, NEW_PASSWORD, NEW_PASSWORD, "n"])
    plan = out[:out.index(FIRST)]
    assert "  Airflow  alice@barney.example.edu:5544/airflow_alice\n" \
           "       ->  alice@db2.example.edu:6543/airflow_alice" in plan
    assert "       ->  alice@db2.example.edu:6543/cache_x" in plan
    assert "WARNING: DAG run history and cached results stay on barney.example.edu:5544. db2.example.edu:6543 " \
           "starts empty unless it already holds them." in plan
    assert "Merging back into the shared database" in plan
    assert OLD_CONN not in out


def test_the_airflow_database_default_is_the_one_in_use(tmp_path, monkeypatch, capsys, tools) -> None:
    box = Sandbox(tmp_path, monkeypatch, capsys, tools, secrets=SECRETS.replace("/airflow_alice", "/ss_conference"))
    box.db.serve("db2.example.edu", 6543, NEW_PASSWORD, "ss_conference", "cache_x")
    try:
        rc, out, err = box.run("configure", answers=[PASSPHRASE, *FIELDS, NEW_PASSWORD, NEW_PASSWORD, "y", "y"])
        assert rc == 0, out + err
        assert ("input", "  Airflow database [ss_conference]: ") in box.prompts
        assert box.secrets_lines()[CONN].endswith("@db2.example.edu:6543/ss_conference")
    finally:
        box.restore()


@pytest.mark.parametrize("password", [" pw' ", '"pw"'])
def test_a_password_with_quotes_or_spaces_loads_back_as_typed(box, password) -> None:
    box.db.serve("db2.example.edu", 6543, password, "airflow_alice", "cache_x")
    rc, out, err = box.run("configure", answers=[PASSPHRASE, *FIELDS, password, password, "y", "y"])
    assert rc == 0, out + err
    sledgehammer_cli._load_secrets()
    assert os.environ["HAMMER_PG_PASSWORD"] == password
    assert pd_store._parse_conn_uri(os.environ[CONN])["password"] == password


def test_an_ipv6_host_is_bracketed_in_new_secrets(tmp_path, monkeypatch, capsys, tools) -> None:
    box = Sandbox(tmp_path, monkeypatch, capsys, tools, secrets=None)
    box.db.serve("::1", 6543, NEW_PASSWORD, "airflow_alice", "cache_x")
    try:
        rc, out, err = box.run("configure", answers=["::1", *FIELDS[1:], NEW_PASSWORD, NEW_PASSWORD, "y", "y"])
        assert rc == 0, out + err
        assert "@[::1]:6543/airflow_alice" in box.secrets_lines()[CONN]
        parsed = pd_store._parse_conn_uri(box.secrets_lines()[CONN])
        assert (parsed["host"], parsed["port"], parsed["password"]) == ("::1", 6543, NEW_PASSWORD)
    finally:
        box.restore()
