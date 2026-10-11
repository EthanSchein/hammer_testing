import shutil
import sys

import pytest

from tests.utils import fake_postgres
from tests.utils.postgres_switch import PASSPHRASE, SECRETS, Sandbox

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

DRYRUN = "[dryrun] SLEDGE_DRYRUN is set, so this stops here; nothing was changed or started."


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    sandbox.db.serve("db2.example.edu", 6543, "pw2", "airflow_alice", "cache_x")
    monkeypatch.setenv("SLEDGE_DRYRUN", "1")
    yield sandbox
    sandbox.restore()


def assert_nothing_changed(box) -> None:
    assert box.secrets.read_text() == SECRETS
    assert not box.prev.exists() and not box.target.exists()
    assert box.calls("initdb") == [] and box.calls("pg_ctl") == []
    assert [c for c in box.gpg_calls() if "--symmetric" in c["args"]] == []


def test_configure_shows_the_plan_and_stops(box) -> None:
    rc, out, err = box.run("configure", answers=[PASSPHRASE, "db2.example.edu", "6543", "", "", "cache_x",
                                                 "pw2", "pw2", "y", "y"])
    assert rc == 0, out + err
    assert "       ->  alice@db2.example.edu:6543/airflow_alice" in out
    assert out.rstrip().endswith(DRYRUN)
    assert box.prompts[-1] == ("getpass", "  password again: ")
    assert_nothing_changed(box)


def test_use_local_shows_the_plan_and_stops(box) -> None:
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE, "y", "local"])
    assert rc == 0, out + err
    assert f"       ->  alice@localhost:{box.port}/airflow_alice" in out
    assert out.rstrip().endswith(DRYRUN)
    assert len(box.prompts) == 1
    assert_nothing_changed(box)
