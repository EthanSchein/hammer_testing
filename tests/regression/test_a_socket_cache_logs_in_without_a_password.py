import shutil
import sys

import pytest

from hammer.vlsi import pd_store
from tests.utils import fake_postgres
from tests.utils.postgres_switch import FakePsycopg2, Sandbox, row

SOCKET = "/var/run/postgresql"
CLEARED = ("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "HAMMER_PG_HOST", "HAMMER_PG_PORT", "HAMMER_PG_DB",
           "HAMMER_PG_USER", "HAMMER_PG_PASSWORD", "PGPASSWORD", "PGPASSFILE", "PGHOST", "PGPORT", "PGHOSTADDR",
           "PGSERVICE", "SLEDGE_PG_DIR", "SLEDGE_ENV_FILE", "HAMMER_PG_CONNECT_TIMEOUT")


@pytest.fixture
def driver(tmp_path, monkeypatch):
    """A shell with no cache password anywhere: no env var, no ~/.pgpass, no secrets and no local Postgres."""
    for var in CLEARED:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    airflow_home = tmp_path / "airflow_home"
    airflow_home.mkdir()
    (airflow_home / "airflow.cfg").write_text("[database]\nsql_alchemy_conn = \n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AIRFLOW_HOME", str(airflow_home))
    monkeypatch.setenv("SLEDGE_SECRETS_FILE", str(tmp_path / "private" / "airflow-secrets.env.gpg"))
    monkeypatch.chdir(tmp_path)
    fake = FakePsycopg2()
    monkeypatch.setattr(pd_store, "psycopg2", fake)
    return fake


@pytest.mark.parametrize("env", [
    {"HAMMER_PG_HOST": SOCKET, "HAMMER_PG_PORT": "5432"},
    {"AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": f"postgresql+psycopg2:///airflow_ab?host={SOCKET}"},
], ids=["HAMMER_PG_HOST", "airflow-conn"])
def test_a_socket_host_connects_with_no_password(driver, monkeypatch, env) -> None:
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    driver.serve(SOCKET, 5432, None, "sledgehammer_studio")
    pd_store._connect()
    login = driver.calls[-1]
    assert (login["host"], login["port"], login["dbname"]) == (SOCKET, 5432, "sledgehammer_studio")
    assert "password" not in login and "passfile" not in login


def _refuse(driver, monkeypatch):
    def refuse(**kw):
        driver.calls.append(kw)
        raise driver.OperationalError(f'connection to server at "{kw["host"]}", port {kw["port"]} failed: '
                                      f'fe_sendauth: no password supplied')

    monkeypatch.setattr(driver, "connect", refuse)


def test_a_socket_server_that_wants_a_password_says_where_to_put_one(driver, monkeypatch) -> None:
    monkeypatch.setenv("HAMMER_PG_HOST", SOCKET)
    _refuse(driver, monkeypatch)
    with pytest.raises(pd_store.DatabaseUnavailable) as err:
        pd_store._connect()
    assert str(err.value) == (f"The Postgres server behind the socket in {SOCKET} asks for a password, and none was "
                              f"found. Export HAMMER_PG_PASSWORD, or add a ~/.pgpass line for this server.")


def test_the_password_message_names_the_pgpass_file_libpq_reads(driver, monkeypatch, tmp_path) -> None:
    passfile = tmp_path / "team.pgpass"
    passfile.write_text("db.example.edu:5432:*:alice:secret\n")
    passfile.chmod(0o600)
    monkeypatch.setenv("PGPASSFILE", str(passfile))
    monkeypatch.setenv("HAMMER_PG_HOST", SOCKET)
    _refuse(driver, monkeypatch)
    with pytest.raises(pd_store.DatabaseUnavailable, match=f"or add a line for this server to {passfile}\\.$"):
        pd_store._connect()


def test_a_tcp_server_that_wants_a_password_keeps_the_libpq_error(driver, monkeypatch, tmp_path) -> None:
    (tmp_path / "home" / ".pgpass").write_text("other.example.edu:5432:*:alice:secret\n")
    (tmp_path / "home" / ".pgpass").chmod(0o600)
    monkeypatch.setenv("HAMMER_PG_HOST", "db.example.edu")
    _refuse(driver, monkeypatch)
    with pytest.raises(driver.OperationalError, match="fe_sendauth: no password supplied"):
        pd_store._connect()


@pytest.mark.parametrize("host", ["db.example.edu", "localhost", "10.0.0.5"])
def test_a_tcp_host_still_needs_a_password(driver, monkeypatch, host) -> None:
    monkeypatch.setenv("HAMMER_PG_HOST", host)
    with pytest.raises(pd_store.DatabaseUnavailable, match="No Postgres password found"):
        pd_store._connect()
    assert driver.calls == []


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")
def test_postgres_show_says_the_socket_logs_in_without_a_password(tmp_path, monkeypatch, capsys) -> None:
    box = Sandbox(tmp_path, monkeypatch, capsys, fake_postgres.system_tools(tmp_path / "tools"), secrets=None)
    try:
        for var, value in {"AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": f"postgresql+psycopg2:///airflow_ab?host={SOCKET}",
                           "PGHOST": SOCKET, "HAMMER_PG_HOST": SOCKET, "HAMMER_PG_PORT": "5432",
                           "HAMMER_PG_DB": "sledgehammer_studio"}.items():
            monkeypatch.setenv(var, value)
        box.db.serve(SOCKET, 5432, None, "airflow_ab", "sledgehammer_studio")
        rc, out, err = box.run("show", tty=False)
        assert rc == 0, err
        assert row(out, "Cache", "password") == ("none;", "tries the Unix socket without one, which works with "
                                                           "peer auth")
        assert row(out, "Cache", "reachable")[0] == "yes"
        assert "no password for the cache" not in out
    finally:
        box.restore()
