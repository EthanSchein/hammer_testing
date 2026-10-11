import shutil
import sys

import pytest

from tests.utils import fake_postgres
from tests.utils.postgres_switch import CONN, OLD_CONN, PASSPHRASE, SECRETS, Sandbox, row

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

STACK_CONN = "postgresql+psycopg2://bob:stackpw-77@iris.example.edu:6000/airflow_bob"
CONFIGURE = [PASSPHRASE, "db2.example.edu", "6543", "", "", "cache_x", "pw2", "pw2"]


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    sandbox.db.serve("db2.example.edu", 6543, "pw2", "airflow_alice", "cache_x")
    yield sandbox
    sandbox.restore()


def stack_file(box, monkeypatch, text):
    env_file = box.tmp / "env.sh"
    env_file.write_text(text)
    monkeypatch.setenv("SLEDGE_ENV_FILE", str(env_file))
    return env_file


def test_a_stack_env_conn_reaches_the_commands_but_not_the_server_sledgehammer_starts(box, monkeypatch) -> None:
    env_file = stack_file(box, monkeypatch, f"export {CONN}={STACK_CONN}\n")
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, err
    secrets = f"the encrypted secrets {box.secrets}"
    stack = f"the stack env file {env_file}"
    assert row(out, "Airflow's database", "host") == ("barney.example.edu", secrets)
    assert "  the other sledgehammer commands (run, status, the airflow passthrough) use " \
           f"bob@iris.example.edu:6000/airflow_bob instead, from {stack}\n" in out
    assert row(out, "Cache", "host") == ("barney.example.edu", f"Airflow's connection, from {secrets}")
    assert "  studio in this shell uses bob@iris.example.edu:6000/sledgehammer_studio instead " \
           f"(host from Airflow's connection, from {stack})\n" in out
    assert f"NOTE: {CONN} is set in {stack}. It wins over the encrypted secrets and {box.target} for the other " \
           "sledgehammer commands and studio." in out
    assert "makes them follow" not in out
    assert "stackpw" not in out + err and "oldpw" not in out + err


def test_a_switch_warns_that_a_stack_env_conn_keeps_winning(box, monkeypatch) -> None:
    env_file = stack_file(box, monkeypatch, f"export {CONN}={STACK_CONN}\n")
    rc, out, _ = box.run("configure", answers=[*CONFIGURE, "n"])
    assert rc == 1
    assert f"WARNING: {CONN} is set in the stack env file {env_file}, so it keeps winning over this switch for " \
           "the other sledgehammer commands and studio until you remove it from that file." in out
    assert box.secrets.read_text() == SECRETS


def test_already_switched_still_names_an_exported_override(box, monkeypatch) -> None:
    assert box.run("use", "local", answers=[PASSPHRASE, "y", "local"])[0] == 0
    monkeypatch.setenv(CONN, OLD_CONN)
    rc, out, _ = box.run("use", "local", answers=[PASSPHRASE])
    assert rc == 0
    assert f"SledgeHammer already uses your local Postgres in {box.pg_root}." in out
    assert f"WARNING: {CONN} is set in this shell's environment, so it keeps winning over this switch for " \
           "everything started from this shell until you unset it." in out
    assert "oldpw" not in out


def test_a_hammer_pg_line_in_the_secrets_is_named_and_the_plan_shows_where_the_cache_lands(tmp_path, monkeypatch,
                                                                                           capsys, tools) -> None:
    box = Sandbox(tmp_path, monkeypatch, capsys, tools, secrets=SECRETS + "HAMMER_PG_HOST=barney.example.edu\n")
    box.db.serve("db2.example.edu", 6543, "pw2", "airflow_alice", "cache_x")
    try:
        rc, out, _ = box.run("configure", answers=[*CONFIGURE, "y", "y"])
        assert rc == 0
        plan = out[:out.index("Switch SledgeHammer to")]
        assert "  cache    alice@barney.example.edu:5544/sledgehammer_studio\n" \
               "       ->  alice@barney.example.edu:6543/cache_x\n" in plan
        assert f"WARNING: HAMMER_PG_HOST is set in the encrypted secrets {box.secrets}, so it keeps winning for " \
               f"the server sledgehammer starts and the other sledgehammer commands: this switch rewrites only " \
               f"{CONN} and HAMMER_PG_PASSWORD there." in plan
        assert box.secrets_lines()["HAMMER_PG_HOST"] == "barney.example.edu"
        rc, out, _ = box.run("show", answers=[PASSPHRASE])
        assert rc == 0
        assert row(out, "Cache", "host") == ("barney.example.edu", f"the encrypted secrets {box.secrets}")
        assert f"NOTE: HAMMER_PG_HOST is set in the encrypted secrets {box.secrets}. It wins over Airflow's " \
               f"connection and {box.target} for the server sledgehammer starts and the other sledgehammer " \
               "commands, and sledgehammer postgres leaves it as it is." in out
        assert "  studio and hammer-vlsi in this shell use alice@db2.example.edu:6543/cache_x instead" in out
        assert "makes them follow" not in out
    finally:
        box.restore()
