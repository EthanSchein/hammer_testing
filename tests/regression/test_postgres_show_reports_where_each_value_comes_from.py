import shutil
import subprocess
import sys

import pytest

from tests.utils import fake_postgres
from tests.utils.postgres_switch import CONN, PASSPHRASE, Sandbox, row

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    yield sandbox
    sandbox.restore()


def test_show_names_each_value_and_its_source_and_never_a_password(box) -> None:
    box.db.serve("barney.example.edu", 5544, "oldpw-1234", "airflow_alice", "sledgehammer_studio")
    rc, out, err = box.run("show", answers=["wrong one", PASSPHRASE])
    assert rc == 0, err
    secrets = f"the encrypted secrets {box.secrets}"
    assert row(out, "Airflow's database", "host") == ("barney.example.edu", secrets)
    assert row(out, "Airflow's database", "port") == ("5544", secrets)
    assert row(out, "Airflow's database", "database") == ("airflow_alice", secrets)
    assert row(out, "Airflow's database", "user") == ("alice", secrets)
    assert row(out, "Airflow's database", "reachable")[0] == "yes"
    assert row(out, "Cache", "host") == ("barney.example.edu", f"Airflow's connection, from {secrets}")
    assert row(out, "Cache", "database") == ("sledgehammer_studio", "the built-in fallback")
    assert row(out, "Cache", "reachable")[0] == "yes"
    assert f"password  from {secrets} (not shown)" in out
    assert "  studio and hammer-vlsi in this shell use alice@localhost:5544/sledgehammer_studio instead " \
           "(host from the built-in fallback)\n" \
           "  sledgehammer postgres configure, given the values above, makes them follow\n" in out
    assert "that passphrase did not work (1/3)" in err
    assert "oldpw" not in out + err and PASSPHRASE not in out + err
    decrypts = box.gpg_calls()
    assert [c["passphrase"] for c in decrypts] == ["wrong one", PASSPHRASE]
    assert all(PASSPHRASE not in " ".join(c["args"]) for c in decrypts)
    assert all(call["connect_timeout"] == 5 for call in box.db.calls)
    assert box.secrets.read_text().startswith(CONN) and not box.prev.exists() and not box.target.exists()


def test_an_exported_override_is_named_and_a_split_is_called_out(box, monkeypatch) -> None:
    monkeypatch.setenv("HAMMER_PG_HOST", "otherhost.example.edu")
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, err
    assert row(out, "Cache", "host") == ("otherhost.example.edu", "this shell's environment")
    assert row(out, "Cache", "reachable")[0] == "no"
    assert f"NOTE: HAMMER_PG_HOST is set in this shell's environment. It wins over the encrypted secrets and " \
           f"{box.target} for everything started from this shell." in out
    assert "NOTE: Airflow and the cache point at different servers: Airflow at barney.example.edu:5544, " \
           "the cache at otherhost.example.edu:5544." in out
    assert "makes them follow" not in out


def test_an_exported_conn_is_named_as_the_winner_over_the_secrets(box, monkeypatch) -> None:
    monkeypatch.setenv(CONN, "postgresql+psycopg2://bob:envpw-99@iris.example.edu:6000/airflow_bob")
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, err
    assert row(out, "Airflow's database", "host") == ("iris.example.edu", "this shell's environment")
    assert "this wins over the encrypted secrets, which name alice@barney.example.edu:5544/airflow_alice" in out
    assert row(out, "Cache", "host") == ("iris.example.edu", "Airflow's connection, from this shell's environment")
    assert f"NOTE: {CONN} is set in this shell's environment" in out
    assert "envpw" not in out + err and "oldpw" not in out + err


def test_without_a_terminal_show_reads_no_secrets_and_says_so(box) -> None:
    rc, out, err = box.run("show", tty=False)
    assert rc == 0, err
    assert box.prompts == [] and box.gpg_calls() == []
    assert f"{box.secrets} was not read: no terminal to ask for its passphrase" in out


def test_show_reports_the_local_postgres(box) -> None:
    rc, out, _ = box.run("show", answers=[PASSPHRASE])
    assert f"Local Postgres: none in {box.pg_root}" in out
    script = box.repo / "scripts" / "sledge-local-postgres.sh"
    assert subprocess.run(["bash", str(script), "setup"], capture_output=True, timeout=60).returncode == 0
    rc, out, _ = box.run("show", answers=[PASSPHRASE])
    assert rc == 0
    assert f"Local Postgres: running on port {box.port} ({box.pg_root})" in out
