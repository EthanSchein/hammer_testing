import os
import shutil
import signal
import socket
import subprocess
import sys

import pytest

from hammer.vlsi import pd_store
from tests.utils import fake_postgres
from tests.utils.postgres_switch import PASSPHRASE, Sandbox, mode

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="uv_setup.sh needs bash and a pty")

# Test names stay clear of conda, mamba, miniforge and pixi: uv_setup.sh drops PATH entries containing them.
STUBS = {
    "bin/uv": '#!/bin/sh\n[ "$1" = --version ] && echo "uv 0.0.0 (stub)"\nexit 0\n',
    "home/pg_local/usr/bin/pg_config": "#!/bin/sh\necho 'PostgreSQL 16 (stub)'\n",
    "checkout/tools/slang/slang": "#!/bin/sh\necho 'slang version 11.0.0 (stub)'\n",
    "checkout/.venv/bin/airflow": "#!/bin/sh\necho 3.1.0\n",
    "checkout/.venv/bin/sledgehammer": "#!/bin/sh\nexit 0\n",
}

# The venv python runs the real sledgehammer_postgres from this tree, and the stand-in python3 for anything else.
VENV_PYTHON = """#!@PY@
import os, sys
args = sys.argv[1:]
if any("sledgehammer_postgres" in a for a in args):
    os.execve(sys.executable, [sys.executable, *(a for a in args if a != "-I")],
              dict(os.environ, PYTHONPATH="@REPO@"))
stub = os.path.join(os.path.dirname(os.path.abspath(__file__)), "python3")
os.execv(stub, [stub, *args])
"""

# Writes the secrets as the stand-in gpg does, then leaves no room beside them for postgres.env.
READ_ONLY_GPG = fake_postgres.GPG + 'chmod 500 "$(dirname "$out")"\n'

ENTER = "\n\n"
LAB_SECRETS = "\n\nlab.example.edu\n\npw\npw\n"
TARGET = ("private", "postgres.env")
PG_VARS = ("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "HAMMER_PG_HOST", "HAMMER_PG_PORT", "HAMMER_PG_DB",
           "HAMMER_PG_USER", "HAMMER_PG_PASSWORD", "SLEDGE_PG_DIR", "SLEDGE_ENV_FILE")


def _busy(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            socket.create_connection((host, port), timeout=1).close()
            return True
        except OSError:
            pass
    return False


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def taken():
    """Another program listens on 5544, so a new local Postgres takes 5545."""
    if _busy(5545):
        pytest.skip("port 5545 is in use here")
    holder = None
    if not _busy(5544):
        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 5544))
        holder.listen()
    yield
    if holder is not None:
        holder.close()


def _setup(tmp, tools, answers, gpg=fake_postgres.GPG) -> str:
    """uv_setup.sh run on a terminal with these answers, home in tmp/home and the secrets in tmp/private; its output."""
    log = tmp / "calls.log"
    scripts = [n for n in sorted(os.listdir(fake_postgres.REPO / "scripts"))
               if os.path.isfile(fake_postgres.REPO / "scripts" / n)]
    checkout = fake_postgres.checkout(tmp / "checkout", *scripts, venv=True, log=log)
    fake_postgres.executable(checkout / ".venv" / "bin" / "python",
                             VENV_PYTHON.replace("@PY@", sys.executable).replace("@REPO@", str(fake_postgres.REPO)))
    for rel, text in STUBS.items():
        fake_postgres.executable(tmp / rel, text)
    fake_postgres.executable(tmp / "bin" / "gpg", gpg)
    (checkout / ".venv" / "bin" / "activate").write_text(f'export PATH="{checkout}/.venv/bin:$PATH"\n')
    home = tmp / "home"
    (home / "libnsl_local" / "usr" / "lib64").mkdir(parents=True, exist_ok=True)
    (home / "libnsl_local" / "usr" / "lib64" / "libnsl.so.1").write_text("")
    (home / ".bashrc").write_text("")
    (tmp / "private").mkdir(mode=0o700, exist_ok=True)
    path = (str(tmp / "bin"), fake_postgres.postgres(tmp / "pgbin", log), tools)
    env = {"HOME": str(home), "USER": "alice", "TMPDIR": str(tmp), "PATH": os.pathsep.join(path),
           "SLEDGE_NO_PLUGINS": "1",
           "SLEDGE_SECRETS_FILE": str(tmp / "private" / "airflow-secrets.env.gpg")}
    import pty
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(["bash", str(checkout / "scripts" / "uv_setup.sh")], stdin=slave,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                                start_new_session=True)
        os.write(master, answers.encode())
        try:
            out, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            pytest.fail(f"uv_setup.sh waited for an answer it was not given:\n{out}")
    finally:
        os.close(slave)
        os.close(master)
    assert proc.returncode == 0, out
    return out


def _lines(path) -> dict:
    return dict(line.split("=", 1) for line in path.read_text().splitlines()
                if "=" in line and not line.startswith("#"))


def _port(root) -> str:
    return dict(line.split(" = ", 1) for line in (root / "data" / "postgresql.conf").read_text().splitlines()
                if line.startswith("port = "))["port"]


def test_enter_throughout_writes_postgres_env_for_the_port_the_local_postgres_took(tmp_path, tools, taken) -> None:
    out = _setup(tmp_path, tools, ENTER)
    root = tmp_path / "home" / ".sledgehammer" / "postgres"
    target = tmp_path.joinpath(*TARGET)
    assert _port(root) == "5545"
    assert _lines(target) == {"HAMMER_PG_HOST": "localhost", "HAMMER_PG_PORT": "5545", "HAMMER_PG_USER": "alice",
                              "HAMMER_PG_DB": "sledgehammer_studio", "SLEDGE_PG_DIR": str(root)}
    assert mode(target) == 0o600
    assert f"  wrote {target}" in out.splitlines()
    password = (root / "password").read_text().strip()
    assert password not in target.read_text()
    assert password not in out


def test_postgres_env_comes_only_with_a_local_setup_that_finished_and_its_secrets(tmp_path, tools, taken) -> None:
    """In "no" and "failed" a local Postgres from an earlier run is there and the secrets name a lab server."""
    for name in ("no", "failed"):
        _setup(tmp_path / name, tools, "\nn\n")
    (tmp_path / "failed" / "home" / ".sledgehammer" / "postgres" / "password").unlink()
    runs = {"enter": ENTER, "no_secrets": "\nn\n", "no": "n\n\n" + LAB_SECRETS, "failed": "\n\n" + LAB_SECRETS}
    out = {name: _setup(tmp_path / name, tools, answers) for name, answers in runs.items()}
    made = {name: sorted(os.listdir(tmp_path / name / "private")) for name in runs}
    assert made == {"enter": ["airflow-secrets.env.gpg", "postgres.env"], "no_secrets": [],
                    "no": ["airflow-secrets.env.gpg"], "failed": ["airflow-secrets.env.gpg"]}
    assert "  the local Postgres setup did not finish; the secrets step below asks which Postgres to use." \
           in out["failed"].splitlines()
    for name in ("no", "failed"):
        assert "@lab.example.edu:5544/" in (tmp_path / name / "private" / "airflow-secrets.env.gpg").read_text()


def test_a_plain_shell_finds_the_cache_on_the_local_postgres_after_setup(tmp_path, tools, taken, monkeypatch) -> None:
    _setup(tmp_path, tools, ENTER)
    for var in PG_VARS:
        monkeypatch.delenv(var, raising=False)
    airflow_home = tmp_path / "airflow_home"
    airflow_home.mkdir()
    (airflow_home / "airflow.cfg").write_text("[database]\nsql_alchemy_conn = \n")
    monkeypatch.setenv("AIRFLOW_HOME", str(airflow_home))
    monkeypatch.setenv("SLEDGE_SECRETS_FILE", str(tmp_path / "private" / "airflow-secrets.env.gpg"))
    monkeypatch.setenv("PGPASSWORD", "pw")
    monkeypatch.chdir(tmp_path)
    settings = pd_store._pg_settings()
    assert (settings["host"], settings["port"], settings["dbname"], settings["user"]) == \
        ("localhost", 5545, "sledgehammer_studio", "alice")


def test_setup_writes_the_postgres_env_that_use_local_writes(tmp_path, monkeypatch, capsys, tools, taken) -> None:
    box = Sandbox(tmp_path, monkeypatch, capsys, tools, secrets=None)
    try:
        _setup(tmp_path, tools, ENTER)
        written = box.target.read_bytes()
        rc, out, err = box.run("use", "local", answers=[PASSPHRASE])
        assert rc == 0, out + err
        assert f"SledgeHammer already uses your local Postgres in {box.pg_root}." in out.splitlines()
        assert [p for _, p in box.prompts] == [f"GPG passphrase for {box.secrets}: "]
        assert box.target.read_bytes() == written
        box.target.unlink()
        rc, out, err = box.run("use", "local", answers=[PASSPHRASE, "y"])
        assert rc == 0, out + err
        assert [p for _, p in box.prompts] == [f"GPG passphrase for {box.secrets}: ", f"Write {box.target}? [y/N]: "]
        assert box.target.read_bytes() == written
        assert mode(box.target) == 0o600
    finally:
        box.restore()


def test_a_postgres_env_with_other_settings_is_kept_and_named(tmp_path, tools, taken) -> None:
    target = tmp_path.joinpath(*TARGET)
    target.parent.mkdir(mode=0o700)
    before = "HAMMER_PG_HOST=db2.example.edu\nHAMMER_PG_PORT=6543\nHAMMER_PG_USER=alice\nHAMMER_PG_DB=cache_x\n"
    target.write_text(before)
    out = _setup(tmp_path, tools, ENTER)
    assert target.read_text() == before
    assert f"  {target} already holds other Postgres settings, so it is left as it is. To point studio and " \
           "hammer-vlsi outside Airflow at your local Postgres, run: sledgehammer postgres use local" \
           in out.splitlines()


def test_setup_finishes_and_names_use_local_when_postgres_env_cannot_be_written(tmp_path, tools, taken) -> None:
    private = tmp_path / "private"
    try:
        out = _setup(tmp_path, tools, ENTER, gpg=READ_ONLY_GPG)
    finally:
        private.chmod(0o700)
    assert sorted(os.listdir(private)) == ["airflow-secrets.env.gpg"]
    assert any(line.startswith(f"[sledgehammer] {private / 'postgres.env'} could not be written (")
               for line in out.splitlines()), out
    assert "  to point studio and hammer-vlsi outside Airflow at it, run: sledgehammer postgres use local" \
           in out.splitlines()
    assert (tmp_path / "home" / ".sledgehammer" / "postgres" / "password").read_text().strip() not in out
