import shutil
import sys

import pytest

from tests.utils import fake_postgres
from tests.utils.postgres_switch import CONN, PASSPHRASE, SECRETS, Sandbox, row

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

FULL = "2001:db8:85a3:0:0:8a2e:370:7334"
PASSWORD = "n3w-pw"


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def secrets_on(tmp_path, monkeypatch, capsys, tools):
    made = []

    def make(server: str) -> Sandbox:
        box = Sandbox(tmp_path, monkeypatch, capsys, tools,
                      secrets=SECRETS.replace("@barney.example.edu:5544/", f"@{server}/"))
        made.append(box)
        return box
    yield make
    for box in made:
        box.restore()


def configure_to(box, host: str):
    return box.run("configure", answers=[PASSPHRASE, host, "6543", "", "", "cache_x", PASSWORD, PASSWORD, "y", "y"])


@pytest.mark.parametrize("host, shown", [("::1", "[::1]"), (FULL, f"[{FULL}]"), ("192.0.2.10", "192.0.2.10")])
def test_configure_names_the_new_server_with_an_ipv6_host_in_brackets(secrets_on, host, shown) -> None:
    box = secrets_on("barney.example.edu:5544")
    box.db.serve(host, 6543, PASSWORD, "airflow_alice", "cache_x")
    rc, out, err = configure_to(box, host)
    assert rc == 0, out + err
    assert f"Testing {shown}:6543 as alice:\n" in out
    assert f"       ->  alice@{shown}:6543/airflow_alice\n" in out
    assert f"       ->  alice@{shown}:6543/cache_x\n" in out
    assert f"stay on barney.example.edu:5544. {shown}:6543 starts empty unless it already holds them." in out
    assert ("input", f"Switch SledgeHammer to {shown}:6543? [y/N]: ") in box.prompts
    assert f"SledgeHammer now uses {shown}:6543 as alice: " in out
    assert f"{host}:6543" not in out.replace(f"{shown}:6543", "")
    assert f"@{shown}:6543/airflow_alice" in box.secrets_lines()[CONN]
    assert box.target_lines()["HAMMER_PG_HOST"] == host


@pytest.mark.parametrize("host", ["::1", FULL])
def test_the_plan_names_an_ipv6_server_left_behind_in_brackets(secrets_on, host) -> None:
    box = secrets_on(f"[{host}]:5548")
    box.db.serve("db2.example.edu", 6543, PASSWORD, "airflow_alice", "cache_x")
    rc, out, err = configure_to(box, "db2.example.edu")
    assert rc == 0, out + err
    assert f"  Airflow  alice@[{host}]:5548/airflow_alice\n" in out
    assert f"  cache    alice@[{host}]:5548/sledgehammer_studio\n" in out
    stay = f"DAG run history and cached results stay on [{host}]:5548"
    assert f"WARNING: {stay}. db2.example.edu:6543 starts empty unless it already holds them." in out
    assert box.prompts[-1] == ("input", f"Your {stay}. Switch anyway? [y/N]: ")
    assert "SledgeHammer now uses db2.example.edu:6543 as alice: " in out


def test_use_local_names_an_ipv6_server_left_behind_in_brackets(secrets_on, monkeypatch) -> None:
    box = secrets_on(f"[{FULL}]:5544")
    monkeypatch.setenv("SLEDGE_DRYRUN", "1")
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE])
    assert rc == 0, out + err
    assert f"  Airflow  alice@[{FULL}]:5544/airflow_alice\n" \
           f"       ->  alice@localhost:{box.port}/airflow_alice\n" in out
    assert f"WARNING: DAG run history and cached results stay on [{FULL}]:5544. localhost:{box.port} starts empty" \
           in out


def test_show_names_ipv6_servers_in_brackets(secrets_on, monkeypatch) -> None:
    box = secrets_on("[::1]:5548")
    monkeypatch.setenv(CONN, f"postgresql+psycopg2://bob:envpw-99@[{FULL}]:6000/airflow_bob")
    monkeypatch.setenv("HAMMER_PG_HOST", "192.0.2.10")
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, err
    assert row(out, "Airflow's database", "host") == (FULL, "this shell's environment")
    assert "this wins over the encrypted secrets, which name alice@[::1]:5548/airflow_alice\n" in out
    assert f"NOTE: Airflow and the cache point at different servers: Airflow at [{FULL}]:6000, " \
           "the cache at 192.0.2.10:6000." in out


def test_show_names_the_ipv6_servers_of_the_commands_and_the_cache_in_brackets(secrets_on, monkeypatch) -> None:
    box = secrets_on("[::1]:5548")
    env_file = box.tmp / "env.sh"
    env_file.write_text(f"export {CONN}=postgresql+psycopg2://bob:stackpw@[{FULL}]:6000/airflow_bob\n")
    monkeypatch.setenv("SLEDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("HAMMER_PG_HOST", FULL)
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, err
    assert f"(run, status, the airflow passthrough) use bob@[{FULL}]:6000/airflow_bob instead, " in out
    assert "NOTE: Airflow and the cache point at different servers: Airflow at [::1]:5548, " \
           f"the cache at [{FULL}]:5548." in out
    assert "stackpw" not in out + err


def test_show_names_the_ipv6_server_studio_and_hammer_vlsi_use_in_brackets(secrets_on) -> None:
    box = secrets_on("barney.example.edu:5544")
    box.target.write_text(f"HAMMER_PG_HOST={FULL}\nHAMMER_PG_PORT=5548\nHAMMER_PG_USER=alice\n"
                          "HAMMER_PG_DB=sledgehammer_studio\n")
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, err
    assert f"  studio and hammer-vlsi in this shell use alice@[{FULL}]:5548/sledgehammer_studio instead " in out


def test_a_failed_postgres_env_write_names_the_ipv6_server_in_brackets(secrets_on) -> None:
    box = secrets_on("barney.example.edu:5544")
    box.db.serve("::1", 6543, PASSWORD, "airflow_alice", "cache_x")
    box.target.mkdir()
    rc, out, err = configure_to(box, "::1")
    assert rc == 1, out + err
    assert "the secrets now name [::1]:6543, but " in err
