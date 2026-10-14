import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hammer.vlsi import pd_store
from tests.utils.postgres_switch import FakePsycopg2

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="file modes and owners are POSIX")

PASSWORD = "5f1e0c9a7b3d2e4f6a8c0b1d3e5f7a9c2b4d6e8f0a1c3e5d"
PORT = 5545
CLEARED = ("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "HAMMER_PG_HOST", "HAMMER_PG_PORT", "HAMMER_PG_DB",
           "HAMMER_PG_USER", "HAMMER_PG_PASSWORD", "PGPASSWORD", "PGPASSFILE", "PGHOSTADDR", "PGSERVICE",
           "SLEDGE_PG_DIR", "HAMMER_PG_CONNECT_TIMEOUT", "SLEDGE_ENV_FILE")
TARGET = "HAMMER_PG_HOST=localhost\nHAMMER_PG_PORT={port}\nHAMMER_PG_USER=alice\nHAMMER_PG_DB=sledgehammer_studio\n"


def _cluster(root, port=PORT, password=PASSWORD):
    """A local Postgres as sledge-local-postgres.sh leaves it running here: its conf, pid, host and a mode 600 password."""
    (root / "data").mkdir(parents=True)
    (root / "data" / "postgresql.conf").write_text(
        f"#port = 5432\n\n# added by sledge-local-postgres.sh\nlisten_addresses = 'localhost'\nport = {port}\n")
    (root / "data" / "postmaster.pid").write_text(f"{os.getpid()}\n{root / 'data'}\n1700000000\n{port}\n")
    (root / "host").write_text(os.uname().nodename + "\n")
    (root / "password").write_text(password)
    (root / "password").chmod(0o600)
    return root / "password"


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A plain shell after the default uv_setup.sh: no secrets loaded, no ~/.pgpass, postgres.env naming the local Postgres."""
    for var in CLEARED:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    root = home / ".sledgehammer" / "postgres"
    password = _cluster(root)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "postgres.env"
    target.write_text(TARGET.format(port=PORT) + f"SLEDGE_PG_DIR={root}\n")
    airflow_home = tmp_path / "airflow_home"
    airflow_home.mkdir()
    (airflow_home / "airflow.cfg").write_text("[database]\nsql_alchemy_conn = \n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AIRFLOW_HOME", str(airflow_home))
    monkeypatch.setenv("SLEDGE_SECRETS_FILE", str(private / "airflow-secrets.env.gpg"))
    monkeypatch.chdir(tmp_path)
    db = FakePsycopg2()
    db.serve("localhost", PORT, PASSWORD, "sledgehammer_studio")
    monkeypatch.setattr(pd_store, "psycopg2", db)
    opened = []
    real_open = os.open

    def recording_open(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    return SimpleNamespace(home=home, root=root, password=password, target=target, db=db, opened=opened,
                           here=os.uname().nodename)


def _no_password(install) -> str:
    with pytest.raises(pd_store.DatabaseUnavailable) as raised:
        pd_store._connect()
    assert install.db.calls == []
    message = str(raised.value)
    assert PASSWORD not in message
    return message


def _hung(signum, frame):
    raise TimeoutError("reading the password file hung")


def test_a_plain_shell_logs_in_with_the_local_postgres_password_file(install) -> None:
    settings = pd_store._pg_settings()
    assert (settings["host"], settings["port"], settings["dbname"], settings["user"]) == \
        ("localhost", PORT, "sledgehammer_studio", "alice")
    assert settings["password"] == PASSWORD
    pd_store._connect()
    assert [call["password"] for call in install.db.calls] == [PASSWORD]
    assert "passfile" not in install.db.calls[0]


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "::1"])
def test_each_local_host_name_reaches_the_password_file(install, monkeypatch, host) -> None:
    monkeypatch.setenv("HAMMER_PG_HOST", host)
    assert pd_store._pg_settings()["password"] == PASSWORD


def test_the_local_postgres_is_found_where_sledge_local_postgres_keeps_it(install, tmp_path, monkeypatch) -> None:
    install.target.write_text(TARGET.format(port=PORT))
    assert pd_store._pg_settings()["password"] == PASSWORD
    _cluster(tmp_path / "elsewhere", password="elsewhere-pw")
    install.target.write_text(TARGET.format(port=PORT) + f"SLEDGE_PG_DIR={tmp_path / 'elsewhere'}\n")
    assert pd_store._pg_settings()["password"] == "elsewhere-pw"
    _cluster(tmp_path / "exported", password="exported-pw")
    monkeypatch.setenv("SLEDGE_PG_DIR", str(tmp_path / "exported"))
    assert pd_store._pg_settings()["password"] == "exported-pw"


def test_the_port_is_read_from_postgresql_conf_as_sledge_local_postgres_reads_it(install, monkeypatch) -> None:
    conf = install.root / "data" / "postgresql.conf"
    conf.write_text(f"port = {PORT + 1}\n  port={PORT}  # moved\n#port = 5432\n")
    assert pd_store._pg_settings()["password"] == PASSWORD
    conf.write_text("listen_addresses = 'localhost'\n")
    monkeypatch.setenv("HAMMER_PG_PORT", "5432")
    assert pd_store._pg_settings()["password"] == PASSWORD
    install.opened.clear()
    monkeypatch.setenv("HAMMER_PG_PORT", "5544")
    _no_password(install)
    assert str(install.password) not in install.opened


def test_the_password_is_the_first_line_of_the_file(install) -> None:
    install.password.write_text(PASSWORD + "\nnot the password\n")
    assert pd_store._pg_settings()["password"] == PASSWORD


def _someone_else(install, monkeypatch):
    real = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real + 1)
    return "was not used because it is owned by someone else"


def _symlink(install, monkeypatch):
    install.password.rename(install.root / "real-password")
    install.password.symlink_to(install.root / "real-password")
    return "was not used because it is a symbolic link"


def _directory(install, monkeypatch):
    install.password.unlink()
    install.password.mkdir()
    return "was not used because it is not a regular file"


def _fifo(install, monkeypatch):
    install.password.unlink()
    os.mkfifo(install.password, 0o600)
    return "was not used because it is not a regular file"


def _mode(bits):
    def chmod(install, monkeypatch):
        install.password.chmod(bits)
        return f"was not used because group or others have access to it (mode {bits:03o}); chmod 600 it"
    return chmod


def _contents(text):
    def write(install, monkeypatch):
        install.password.write_text(text)
        return "is empty"
    return write


UNUSABLE = {"mode_644": _mode(0o644), "mode_640": _mode(0o640), "mode_602": _mode(0o602),
            "someone_else": _someone_else, "symlink": _symlink, "directory": _directory, "fifo": _fifo,
            "empty": _contents(""), "blank_line": _contents("\n")}


@pytest.mark.parametrize("unusable", UNUSABLE.values(), ids=UNUSABLE.keys())
def test_an_unusable_password_file_is_ignored_and_the_error_says_why(install, monkeypatch, unusable) -> None:
    why = unusable(install, monkeypatch)
    previous = signal.signal(signal.SIGALRM, _hung)
    signal.alarm(10)
    try:
        message = _no_password(install)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert message.startswith(f"No Postgres password found. {install.password} {why}. Run \"source venv.sh\"")


def _stopped(install):
    (install.root / "data" / "postmaster.pid").unlink()
    return f"is not running on {install.here}"


def _crashed(install):
    gone = subprocess.Popen(["true"])
    gone.wait()
    (install.root / "data" / "postmaster.pid").write_text(f"{gone.pid}\n{install.root / 'data'}\n1700000000\n{PORT}\n")
    return f"is not running on {install.here}"


def _no_pid(install):
    (install.root / "data" / "postmaster.pid").write_text(f"0\n{install.root / 'data'}\n1700000000\n{PORT}\n")
    return f"is not running on {install.here}"


def _started_elsewhere(install):
    (install.root / "host").write_text("otherhost\n")
    return f"was started on otherhost, not on {install.here}"


def _no_host_file(install):
    (install.root / "host").unlink()
    return f"is not running on {install.here}"


NOT_HERE = {"stopped": _stopped, "crashed": _crashed, "no_pid": _no_pid, "started_elsewhere": _started_elsewhere,
            "no_host_file": _no_host_file}


@pytest.mark.parametrize("not_here", NOT_HERE.values(), ids=NOT_HERE.keys())
def test_the_password_file_is_not_read_unless_the_local_postgres_runs_on_this_host(install, not_here) -> None:
    assert pd_store._pg_settings()["password"] == PASSWORD
    install.opened.clear()
    why = not_here(install)
    message = _no_password(install)
    assert str(install.password) not in install.opened
    assert message.startswith(f"No Postgres password found. {install.password} was not used because your local "
                              f"Postgres {why}. Run \"source venv.sh\"")


def test_a_missing_password_file_is_not_named_even_when_the_local_postgres_is_stopped(install) -> None:
    assert pd_store._pg_settings()["password"] == PASSWORD
    install.password.unlink()
    _stopped(install)
    assert str(install.password) not in _no_password(install)


@pytest.mark.parametrize("var, value", [("PGHOSTADDR", "192.0.2.10"), ("PGSERVICE", "elsewhere")])
def test_the_password_file_is_not_read_when_libpq_could_connect_elsewhere(install, monkeypatch, var, value) -> None:
    assert pd_store._pg_settings()["password"] == PASSWORD
    install.opened.clear()
    monkeypatch.setenv(var, value)
    message = _no_password(install)
    assert str(install.password) not in install.opened
    assert message.startswith(f"No Postgres password found. {install.password} was not used because {var} is set "
                              f"and can send the login to another server. Run \"source venv.sh\"")


def test_the_password_file_is_not_read_without_posix_file_owners(install, monkeypatch) -> None:
    assert pd_store._pg_settings()["password"] == PASSWORD
    install.opened.clear()
    monkeypatch.delattr(os, "getuid")
    message = _no_password(install)
    assert str(install.password) not in install.opened
    assert str(install.password) not in message


@pytest.mark.parametrize("var, value", [("HAMMER_PG_HOST", "db.example.edu"), ("HAMMER_PG_HOST", "localhost2"),
                                        ("HAMMER_PG_PORT", str(PORT + 1)), ("HAMMER_PG_PORT", "5432")])
def test_the_password_file_is_not_read_for_another_server(install, monkeypatch, var, value) -> None:
    assert pd_store._pg_settings()["password"] == PASSWORD
    install.opened.clear()
    monkeypatch.setenv(var, value)
    message = _no_password(install)
    assert str(install.password) not in install.opened
    assert str(install.password) not in message


def test_a_postgres_env_on_another_port_does_not_reach_the_password_file(install) -> None:
    assert pd_store._pg_settings()["password"] == PASSWORD
    install.opened.clear()
    install.target.write_text(TARGET.format(port=PORT + 1) + f"SLEDGE_PG_DIR={install.root}\n")
    _no_password(install)
    assert str(install.password) not in install.opened


def _pgpass(install, monkeypatch):
    (install.home / ".pgpass").write_text(f"localhost:{PORT}:*:alice:from-pgpass\n")
    (install.home / ".pgpass").chmod(0o600)


WINNERS = {
    "hammer_pg_password": (lambda install, mp: mp.setenv("HAMMER_PG_PASSWORD", "from-env"),
                           {"password": "from-env"}),
    "conn": (lambda install, mp: mp.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN",
                                           f"postgresql+psycopg2://alice:from-conn@localhost:{PORT}/airflow_alice"),
             {"password": "from-conn"}),
    "pgpassword": (lambda install, mp: mp.setenv("PGPASSWORD", "from-pgpassword"), {}),
    "pgpass": (_pgpass, {"passfile": "@HOME@/.pgpass"}),
}


@pytest.mark.parametrize("winner, expected", WINNERS.values(), ids=WINNERS.keys())
def test_every_other_password_source_wins_over_the_password_file(install, monkeypatch, winner, expected) -> None:
    assert pd_store._pg_settings()["password"] == PASSWORD
    install.opened.clear()
    winner(install, monkeypatch)
    settings = pd_store._pg_settings()
    got = {key: settings[key] for key in ("password", "passfile") if key in settings}
    assert got == {key: value.replace("@HOME@", str(install.home)) for key, value in expected.items()}
    assert str(install.password) not in install.opened


def test_the_no_password_error_points_at_venv_sh_and_postgres_show(install, monkeypatch) -> None:
    monkeypatch.setenv("HAMMER_PG_HOST", "db.example.edu")
    message = _no_password(install)
    assert message == ("No Postgres password found. Run \"source venv.sh\" to load HAMMER_PG_PASSWORD from the "
                       "encrypted secrets, export HAMMER_PG_PASSWORD, or add a ~/.pgpass line for this server; "
                       "\"sledgehammer postgres show\" says where each setting comes from.")
    assert "airflow.cfg" not in message
