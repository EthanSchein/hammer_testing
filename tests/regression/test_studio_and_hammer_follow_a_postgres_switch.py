import os
import shutil
import sys

import pytest

from hammer.shell import pd_store_cli
from hammer.vlsi import pd_store
from tests.utils import fake_postgres
from tests.utils.postgres_switch import CONN, PASSPHRASE, Sandbox

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

PASSWORD = "s3cret-db2"
CONFIGURE = [PASSPHRASE, "db2.example.edu", "6543", "", "", "cache_x", PASSWORD, PASSWORD, "y", "y"]


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    sandbox.db.serve("db2.example.edu", 6543, PASSWORD, "airflow_alice", "cache_x")
    yield sandbox
    sandbox.restore()


def test_studio_outside_airflow_goes_to_the_new_server(box) -> None:
    assert box.run("configure", answers=CONFIGURE)[0] == 0
    pgpass = box.home / ".pgpass"
    pgpass.write_text(f"db2.example.edu:6543:cache_x:alice:{PASSWORD}\n")
    pgpass.chmod(0o600)
    assert not os.environ.get(CONN) and not os.environ.get("HAMMER_PG_HOST")
    assert pd_store_cli.main(["init"]) == 1
    call = box.db.calls[-1]
    assert (call["host"], call["port"], call["dbname"], call["user"], call["passfile"]) == \
        ("db2.example.edu", 6543, "cache_x", "alice", str(pgpass))


def test_hammer_runs_outside_airflow_resolve_the_new_server(box, monkeypatch) -> None:
    assert box.run("configure", answers=CONFIGURE)[0] == 0
    monkeypatch.setenv("HAMMER_PG_PASSWORD", PASSWORD)
    settings = pd_store._pg_settings()
    assert (settings["host"], settings["port"], settings["dbname"], settings["user"]) == \
        ("db2.example.edu", 6543, "cache_x", "alice")


def test_after_use_local_the_cache_resolves_the_local_server(box, monkeypatch) -> None:
    assert box.run("use", "local", answers=[PASSPHRASE, "y", "local"])[0] == 0
    monkeypatch.setenv("HAMMER_PG_PASSWORD", box.local_password())
    settings = pd_store._pg_settings()
    assert (settings["host"], settings["port"], settings["dbname"]) == ("localhost", box.port, "sledgehammer_studio")


def test_explicit_settings_still_win_over_the_switch(box, monkeypatch) -> None:
    assert box.run("configure", answers=CONFIGURE)[0] == 0
    monkeypatch.setenv("HAMMER_PG_PASSWORD", PASSWORD)
    monkeypatch.setenv("HAMMER_PG_HOST", "mine.example.edu")
    assert pd_store._pg_settings()["host"] == "mine.example.edu"
    monkeypatch.delenv("HAMMER_PG_HOST")
    monkeypatch.setenv(CONN, "postgresql+psycopg2://bob:pw@iris.example.edu:6000/airflow_bob")
    settings = pd_store._pg_settings()
    assert (settings["host"], settings["port"], settings["user"], settings["dbname"]) == \
        ("iris.example.edu", 6000, "bob", "cache_x")
