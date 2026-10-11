import shutil
import sys

import pytest

from tests.utils import fake_postgres
from tests.utils.postgres_switch import CONN, OLD_CONN, PASSPHRASE, SECRETS, Sandbox, mode

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

FIRST = "Switch SledgeHammer to your local Postgres? [y/N]: "
SECOND = 'Type "local" to confirm the switch: '


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    yield sandbox
    sandbox.restore()


def assert_nothing_changed(box) -> None:
    assert box.secrets.read_text() == SECRETS
    assert not box.prev.exists() and not box.target.exists()
    assert box.calls("initdb") == [] and box.calls("pg_ctl") == []
    assert [c for c in box.gpg_calls() if "--symmetric" in c["args"]] == []


@pytest.mark.parametrize("answers", [["n"], ["", "local"], ["y", "yes"], ["y", "no"]])
def test_either_no_aborts_with_nothing_changed(box, answers) -> None:
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE, *answers])
    assert rc == 1
    assert "Nothing changed." in out
    assert_nothing_changed(box)


def test_the_plan_and_the_warning_come_before_the_first_question(box) -> None:
    rc, out, _ = box.run("use", "local", answers=[PASSPHRASE, "n"])
    assert box.prompts == [("getpass", f"GPG passphrase for {box.secrets}: "), ("input", FIRST)]
    plan = out[:out.index(FIRST)]
    assert "  Airflow  alice@barney.example.edu:5544/airflow_alice\n" \
           f"       ->  alice@localhost:{box.port}/airflow_alice" in plan
    assert "  cache    alice@barney.example.edu:5544/sledgehammer_studio\n" \
           f"       ->  alice@localhost:{box.port}/sledgehammer_studio" in plan
    assert "WARNING: DAG run history and cached results stay on barney.example.edu:5544." in plan
    assert "Merging back into the shared database" in plan and "LOCAL_POSTGRES_SETUP.md" in plan
    assert f"Your local Postgres is created in {box.pg_root}" in plan


def test_two_yeses_switch_and_rewrite_only_the_two_lines(box) -> None:
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE, "y", "local"])
    assert rc == 0, out + err
    assert [p for _, p in box.prompts] == [f"GPG passphrase for {box.secrets}: ", FIRST, SECOND]
    password = box.local_password()
    before = SECRETS.splitlines()
    after = box.secrets.read_text().splitlines()
    assert len(after) == len(before)
    changed = [(old, new) for old, new in zip(before, after) if old != new]
    assert changed == [(f"{CONN}={OLD_CONN}",
                        f"{CONN}=postgresql+psycopg2://alice:{password}@localhost:{box.port}/airflow_alice"),
                       ("HAMMER_PG_PASSWORD=oldpw-1234", f"HAMMER_PG_PASSWORD={password}")]
    assert box.prev.read_text() == SECRETS
    assert mode(box.secrets) == 0o600 and mode(box.prev) == 0o600
    assert box.target_lines() == {"HAMMER_PG_HOST": "localhost", "HAMMER_PG_PORT": str(box.port),
                                  "HAMMER_PG_USER": "alice", "HAMMER_PG_DB": "sledgehammer_studio",
                                  "SLEDGE_PG_DIR": str(box.pg_root)}
    assert (box.pg_root / "data" / "postmaster.pid").exists()
    encrypt = [c for c in box.gpg_calls() if "--symmetric" in c["args"]]
    assert [c["passphrase"] for c in encrypt] == [PASSPHRASE]
    assert all(PASSPHRASE not in " ".join(c["args"]) for c in box.gpg_calls())
    assert password not in out + err and "oldpw" not in out + err


def test_a_second_run_finds_it_already_switched(box) -> None:
    assert box.run("use", "local", answers=[PASSPHRASE, "y", "local"])[0] == 0
    switched = box.secrets.read_text()
    rc, out, _ = box.run("use", "local", answers=[PASSPHRASE])
    assert rc == 0
    assert f"SledgeHammer already uses your local Postgres in {box.pg_root}." in out
    assert box.secrets.read_text() == switched and box.prev.read_text() == SECRETS


def test_overrides_are_named_before_the_questions(box, monkeypatch) -> None:
    env_file = box.tmp / "env.sh"
    env_file.write_text("export HAMMER_PG_HOST=stackhost.example.edu\n")
    monkeypatch.setenv("SLEDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("HAMMER_PG_PASSWORD", "exported-pw")
    rc, out, _ = box.run("use", "local", answers=[PASSPHRASE, "n"])
    plan = out[:out.index(FIRST)]
    assert f"WARNING: HAMMER_PG_HOST is set in the stack env file {env_file}, so it keeps winning over this " \
           "switch for the other sledgehammer commands and studio until you remove it from that file." in plan
    assert "WARNING: HAMMER_PG_PASSWORD is set in this shell's environment, so it keeps winning over this " \
           "switch for studio and hammer-vlsi until you unset it." in plan
    assert "exported-pw" not in out
    assert_nothing_changed(box)


@pytest.mark.parametrize("answers", [[KeyboardInterrupt()], ["y", KeyboardInterrupt()], ["y", EOFError()]])
def test_ctrl_c_or_end_of_input_at_either_question_changes_nothing(box, answers) -> None:
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE, *answers])
    assert rc == 1
    assert err.endswith("[sledgehammer] stopped; nothing changed\n")
    assert_nothing_changed(box)


def test_with_no_secrets_yet_they_are_created_by_the_secrets_script(tmp_path, monkeypatch, capsys, tools) -> None:
    box = Sandbox(tmp_path, monkeypatch, capsys, tools, secrets=None)
    try:
        rc, out, err = box.run("use", "local", answers=["y", "local"])
        assert rc == 0, out + err
        assert "  Airflow  nothing yet\n" in out
        assert f"WARNING: cached results stay on localhost:5544. localhost:{box.port} starts empty" in out
        password = box.local_password()
        written = box.secrets_lines()
        assert written[CONN] == f"postgresql+psycopg2://alice:{password}@localhost:{box.port}/airflow_alice"
        assert written["HAMMER_PG_PASSWORD"] == password
        assert written["SLEDGE_PG_DIR"] == str(box.pg_root)
        assert {"AIRFLOW__CORE__FERNET_KEY", "AIRFLOW__API_AUTH__JWT_SECRET"} <= set(written)
        assert box.target_lines()["HAMMER_PG_PORT"] == str(box.port)
        assert not box.prev.exists()
        assert password not in out + err
    finally:
        box.restore()
