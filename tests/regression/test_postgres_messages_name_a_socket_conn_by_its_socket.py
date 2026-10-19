import shutil
import sys

import pytest

from tests.utils import fake_postgres
from tests.utils.postgres_switch import CONN, OLD_CONN, PASSPHRASE, SECRETS, Sandbox

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

SOCKET_CONN = "postgresql+psycopg2:///airflow_alice?host=/var/run/postgresql"
SOCKET = "/var/run/postgresql:5432/airflow_alice"
CACHE_PW = "t3am c@che"
FIELDS = ["cache.example.edu", "5432", "t1a", "team1_cache", CACHE_PW, CACHE_PW]


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def boxes(tmp_path, monkeypatch, capsys, tools):
    made = []
    monkeypatch.delenv("PGPORT", raising=False)

    def make(secrets) -> Sandbox:
        box = Sandbox(tmp_path, monkeypatch, capsys, tools, secrets=secrets)
        box.db.serve("cache.example.edu", 5432, CACHE_PW, "team1_cache")
        made.append(box)
        return box
    yield make
    for box in made:
        box.restore()


def stack_conn(box, monkeypatch, conn: str):
    env_file = box.tmp / "env.sh"
    env_file.write_text(f"export {CONN}='{conn}'\n")
    monkeypatch.setenv("SLEDGE_ENV_FILE", str(env_file))
    return env_file


@pytest.mark.parametrize("conn, shown", [
    (SOCKET_CONN, SOCKET),
    ("postgresql+psycopg2:///airflow_alice?host=/tmp/pg&port=5548", "/tmp/pg:5548/airflow_alice"),
    ("postgresql+psycopg2://bob@/airflow_bob?host=/var/run/postgresql", "bob@/var/run/postgresql:5432/airflow_bob"),
])
def test_cache_names_the_socket_airflow_stays_on(boxes, monkeypatch, conn, shown) -> None:
    box = boxes(None)
    stack_conn(box, monkeypatch, conn)
    rc, out, err = box.run("cache", answers=[*FIELDS, "y", "y"])
    assert rc == 0, out + err
    assert f"  Airflow  stays on {shown}\n" in out
    assert "None" not in out + err


def test_show_names_the_socket_the_other_commands_use(boxes, monkeypatch) -> None:
    box = boxes(SECRETS)
    env_file = stack_conn(box, monkeypatch, SOCKET_CONN)
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, out + err
    assert f"(run, status, the airflow passthrough) use {SOCKET} instead, from the stack env file {env_file}" in out
    assert "None" not in out + err


def test_show_names_the_socket_the_encrypted_secrets_hold(boxes, monkeypatch) -> None:
    box = boxes(SECRETS.replace(OLD_CONN, SOCKET_CONN))
    monkeypatch.setenv(CONN, OLD_CONN)
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, out + err
    assert f"  this wins over the encrypted secrets, which name {SOCKET}\n" in out
    assert "None" not in out + err


def test_a_switch_names_the_socket_airflow_leaves(boxes, monkeypatch) -> None:
    box = boxes(SECRETS.replace(OLD_CONN, SOCKET_CONN))
    monkeypatch.setenv("SLEDGE_DRYRUN", "1")
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE])
    assert rc == 0, out + err
    assert f"  Airflow  {SOCKET}\n       ->  alice@localhost:{box.port}/airflow_alice\n" in out
    assert f"WARNING: DAG run history and cached results stay on /var/run/postgresql:5432. localhost:{box.port} " \
           "starts empty unless it already holds them." in out
    assert "None" not in out + err
