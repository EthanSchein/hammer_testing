import os
import re
import shutil
import socket
import stat
import subprocess
import sys

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="sledge-local-postgres.sh needs bash")


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


class Box:
    """A home, a checkout and stand-in Postgres programs for one test."""

    def __init__(self, tmp_path, tools, venv=False, pg_dir=None):
        self.home = tmp_path / "home"
        self.home.mkdir(parents=True)
        self.log = tmp_path / "calls.log"
        self.repo = fake_postgres.checkout(tmp_path / "checkout", "sledge-local-postgres.sh", venv=venv, log=self.log)
        self.script = str(self.repo / "scripts" / "sledge-local-postgres.sh")
        self.pg_bin = fake_postgres.postgres(tmp_path / "pgbin", self.log)
        self.tools = tools
        self.root = pg_dir or self.home / ".sledgehammer" / "postgres"
        self.env = {"SLEDGE_PG_DIR": str(pg_dir)} if pg_dir else {}
        self.port = fake_postgres.free_port()

    def run(self, *args, path=None, env=None):
        return subprocess.run(["bash", self.script, *args], capture_output=True, text=True, timeout=60,
                              env={"HOME": str(self.home), "USER": "alice", "SLEDGE_PG_PORT": str(self.port),
                                   "PATH": path or os.pathsep.join((self.pg_bin, self.tools)),
                                   **self.env, **(env or {})})

    def calls(self, program=""):
        return fake_postgres.calls(self.log, program)

    def conf_port(self):
        return re.findall(r"^port = (\d+)$", (self.root / "data" / "postgresql.conf").read_text(), re.M)[-1]

    def password(self):
        return (self.root / "password").read_text().strip()


def test_setup_creates_a_password_protected_cluster_with_both_databases(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    res = box.run("setup")
    assert res.returncode == 0, res.stderr
    [initdb] = box.calls("initdb")
    assert initdb.split()[1:] == ["-D", str(box.root / "data"), "-U", "alice", "-E", "UTF8", "--locale=C",
                                  "--auth-local=scram-sha-256", "--auth-host=scram-sha-256",
                                  f"--pwfile={box.root / 'password'}"]
    conf = (box.root / "data" / "postgresql.conf").read_text()
    assert re.findall(r"^listen_addresses = (.*)$", conf, re.M) == ["'localhost'"]
    assert re.findall(r"^unix_socket_directories = (.*)$", conf, re.M) == [f"'{box.root}'"]
    assert box.conf_port() == str(box.port)
    assert box.calls("pg_ctl")[-1].split()[1:] == ["-D", str(box.root / "data"), "-l", str(box.root / "postgres.log"),
                                                   "-w", "start"]
    assert (box.root / "data" / "databases").read_text().split() == ["airflow_alice", "sledgehammer_studio"]
    assert stat.S_IMODE(os.stat(box.root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(box.root / "password").st_mode) == 0o600
    assert re.fullmatch(r"[0-9a-f]{48}", box.password())
    assert box.password() not in box.log.read_text() + res.stdout + res.stderr


def test_a_second_setup_creates_nothing(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    assert box.run("setup").returncode == 0
    password = box.password()
    starts = len([c for c in box.calls("pg_ctl") if c.endswith(" start")])
    res = box.run("setup")
    assert res.returncode == 0, res.stderr
    assert len(box.calls("initdb")) == 1
    assert len([c for c in box.calls("pg_ctl") if c.endswith(" start")]) == starts
    assert (box.root / "data" / "databases").read_text().split() == ["airflow_alice", "sledgehammer_studio"]
    assert box.password() == password
    assert "already running" in res.stdout


def test_a_taken_port_moves_a_new_cluster_to_the_next_free_one(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    with socket.socket() as other:
        other.bind(("127.0.0.1", box.port))
        other.listen()
        res = box.run("setup")
        assert res.returncode == 0, res.stderr
        chosen = int(box.conf_port())
        assert chosen > box.port
        assert f"port {box.port} is in use by another program; using {chosen}" in res.stdout
        assert box.run("stop").returncode == 0
        assert box.run("start").returncode == 0
    assert f"SLEDGE_PG_PORT={chosen}" in box.run("env").stdout.splitlines()
    assert all(f"-p {chosen} " in c for c in box.calls("psql"))


def test_start_refuses_a_port_another_program_took_meanwhile(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    assert box.run("setup").returncode == 0
    assert box.run("stop").returncode == 0
    starts = len(box.calls("pg_ctl"))
    with socket.socket() as other:
        other.bind(("127.0.0.1", box.port))
        other.listen()
        res = box.run("start")
    assert res.returncode == 1
    assert f"port {box.port} is in use by another program" in res.stderr
    assert not [c for c in box.calls("pg_ctl")[starts:] if c.endswith(" start")]


def test_env_prints_settings_and_the_password_file_but_never_the_password(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    assert box.run("setup").returncode == 0
    res = box.run("env")
    assert res.returncode == 0
    assert dict(line.split("=", 1) for line in res.stdout.splitlines()) == {
        "SLEDGE_PG_DIR": str(box.root),
        "SLEDGE_PG_HOST": "localhost",
        "SLEDGE_PG_PORT": str(box.port),
        "SLEDGE_PG_USER": "alice",
        "SLEDGE_PG_DB": "airflow_alice",
        "SLEDGE_PG_CACHE_DB": "sledgehammer_studio",
        "SLEDGE_PG_PASSWORD_FILE": str(box.root / "password"),
    }
    assert box.password() not in res.stdout + res.stderr


def test_status_start_and_stop(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    assert box.run("status").returncode == 4
    assert box.run("setup").returncode == 0
    assert box.run("status").returncode == 0
    assert box.run("stop").returncode == 0
    res = box.run("status")
    assert (res.returncode, res.stdout.split(";")[0]) == (3, "stopped")
    assert box.run("start").returncode == 0
    assert box.run("status").returncode == 0


def test_a_cluster_left_running_on_another_host_is_not_started_or_stopped_here(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    assert box.run("setup").returncode == 0
    (box.root / "host").write_text("elsewhere.example.edu\n")
    calls = len(box.calls("pg_ctl"))
    assert box.run("status").returncode == 2
    for action in ("start", "stop"):
        res = box.run(action)
        assert res.returncode == 1
        assert "elsewhere.example.edu" in res.stderr
    assert len(box.calls("pg_ctl")) == calls


def test_postgres_comes_from_a_conda_on_path_when_none_is_installed(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    conda_bin = fake_postgres.conda(tmp_path / "condabin", box.log, box.pg_bin)
    res = box.run("setup", path=os.pathsep.join((conda_bin, box.tools)))
    assert res.returncode == 0, res.stderr
    prefix = box.home / ".sledgehammer" / "pg-env"
    assert box.calls("conda") == [f"conda create -y -p {prefix} --override-channels -c conda-forge postgresql"]
    assert f"installing Postgres from conda-forge into {prefix}" in res.stdout
    assert (box.root / "bindir").read_text().strip() == str(prefix / "bin")
    assert box.run("status", path=box.tools).returncode == 0
    assert box.calls("conda") == [f"conda create -y -p {prefix} --override-channels -c conda-forge postgresql"]


def test_no_postgres_and_no_conda_says_how_to_get_one(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    res = box.run("setup", path=box.tools)
    assert res.returncode == 1
    assert "micromamba or conda" in res.stderr
    assert "/usr/lib/postgresql/<version>/bin" in res.stderr
    assert not (box.root / "data").exists()


def test_free_space_is_reported_before_initdb(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    res = box.run("setup")
    assert res.stdout.index("free space where") < res.stdout.index("creating a Postgres cluster")
    low = fake_postgres.executable(tmp_path / "lowdisk" / "df", fake_postgres.LOW_DISK_DF)
    other = Box(tmp_path / "second", box.tools)
    res = other.run("setup", path=os.pathsep.join((str(low.parent), other.pg_bin, other.tools)))
    assert res.returncode == 0, res.stderr
    assert "free space where" in res.stdout and "1.0 GB" in res.stdout
    assert res.stdout.index("WARNING: that is under 5 GB") < res.stdout.index("creating a Postgres cluster")


def test_cache_tables_are_created_when_the_venv_can(tmp_path, tools) -> None:
    box = Box(tmp_path, tools, venv=True)
    res = box.run("setup")
    assert res.returncode == 0, res.stderr
    assert f"schema alice@localhost:{box.port}/sledgehammer_studio" in box.calls("schema")
    assert "cache tables ready in sledgehammer_studio" in res.stdout
    assert box.password() not in box.log.read_text() + res.stdout + res.stderr


def test_cache_tables_wait_for_a_venv(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    res = box.run("setup")
    assert res.returncode == 0
    assert "cache tables: not created yet" in res.stdout


def test_cache_tables_that_cannot_be_made_yet_do_not_fail_the_setup(tmp_path, tools) -> None:
    box = Box(tmp_path, tools, venv=True)
    fake_postgres.executable(box.repo / ".venv" / "bin" / "python",
                             '#!/bin/sh\ncase "$*" in *ensure_schema*) exit 1 ;; esac\nexit 0\n')
    res = box.run("setup")
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert "cache tables: could not create them now in sledgehammer_studio; they are created on first use" in lines
    assert lines[-1].startswith("local Postgres ready: alice@localhost:")
    assert box.run("status").returncode == 0


def test_a_rerun_tightens_loosened_modes(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    assert box.run("setup").returncode == 0
    box.root.chmod(0o755)
    (box.root / "password").chmod(0o644)
    assert box.run("setup").returncode == 0
    assert stat.S_IMODE(os.stat(box.root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(box.root / "password").st_mode) == 0o600


def test_a_port_taken_only_on_ipv6_localhost_counts_as_taken(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    other = socket.socket(socket.AF_INET6)
    try:
        other.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        other.bind(("::1", box.port))
    except OSError:
        other.close()
        pytest.skip("no IPv6 localhost here")
    with other:
        other.listen()
        res = box.run("setup")
    assert res.returncode == 0, res.stderr
    assert int(box.conf_port()) > box.port
    assert f"port {box.port} is in use by another program" in res.stdout


def test_a_data_dir_with_a_comma_gets_tcp_only(tmp_path, tools) -> None:
    box = Box(tmp_path, tools, pg_dir=tmp_path / "pg,data")
    res = box.run("setup")
    assert res.returncode == 0, res.stderr
    assert "has a comma or backslash" in res.stdout
    conf = (box.root / "data" / "postgresql.conf").read_text()
    assert re.findall(r"^unix_socket_directories = (.*)$", conf, re.M) == ["''"]
    assert (box.root / "data" / "databases").read_text().split() == ["airflow_alice", "sledgehammer_studio"]


def test_the_server_does_not_inherit_the_secrets(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    assert box.run("setup").returncode == 0
    assert box.run("stop").returncode == 0
    secrets = {"AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": "postgresql+psycopg2://alice:hunter2@localhost/airflow_alice",
               "AIRFLOW__CORE__FERNET_KEY": "fernet", "AIRFLOW__API_AUTH__JWT_SECRET": "jwt",
               "AIRFLOW_CONN_METADATA": "postgresql://alice:hunter2@localhost/airflow_alice",
               "HAMMER_PG_PASSWORD": "hunter2", "PGPASSWORD": "hunter2", "SLEDGE_SMTP_PASSWORD": "mail",
               "GITHUB_TOKEN": "token"}
    assert box.run("start", env=secrets).returncode == 0
    server_env = dict(line.split("=", 1) for line in
                      (box.root / "data" / "postmaster.env").read_text().splitlines() if "=" in line)
    assert server_env["HOME"] == str(box.home)
    assert not set(secrets) & set(server_env)


def test_stopping_a_crashed_server_lets_another_host_start_it(tmp_path, tools) -> None:
    box = Box(tmp_path, tools)
    assert box.run("setup").returncode == 0
    (box.root / "data" / "postmaster.pid").write_text("crashed\n")
    uname = fake_postgres.executable(tmp_path / "otherhost" / "uname", "#!/bin/sh\necho otherhost\n")
    on_other_host = os.pathsep.join((str(uname.parent), box.pg_bin, box.tools))
    res = box.run("start", path=on_other_host)
    assert res.returncode == 1
    assert "not stopped there" in res.stderr
    res = box.run("stop")
    assert (res.returncode, res.stdout.split(" (")[0]) == (0, "not running")
    res = box.run("start", path=on_other_host)
    assert res.returncode == 0, res.stderr
    assert (box.root / "host").read_text().strip() == "otherhost"
