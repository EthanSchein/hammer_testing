"""A sandbox for `sledgehammer postgres`: a stand-in gpg and psycopg2, the real local Postgres script on fake binaries, and scripted answers."""
import io
import json
import os
import re
import stat
import sys
from pathlib import Path

from hammer.shell import sledgehammer_cli
from hammer.vlsi import pd_store
from tests.utils import fake_postgres

CONN = "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"
OLD_CONN = "postgresql+psycopg2://alice:oldpw-1234@barney.example.edu:5544/airflow_alice"
SECRETS = (f"{CONN}={OLD_CONN}\n"
           "AIRFLOW__CORE__FERNET_KEY=fernet-key-stays\n"
           "AIRFLOW__CORE__INTERNAL_API_SECRET_KEY=api-key-stays\n"
           "AIRFLOW__API__SECRET_KEY=api-key-stays\n"
           "AIRFLOW__API_AUTH__JWT_SECRET=jwt-stays\n"
           "HAMMER_PG_PASSWORD=oldpw-1234\n")
PASSPHRASE = "correct horse"
CLEARED = (CONN, "HAMMER_PG_HOST", "HAMMER_PG_PORT", "HAMMER_PG_DB", "HAMMER_PG_USER", "HAMMER_PG_PASSWORD",
           "SLEDGE_PG_DIR", "SLEDGE_ENV_FILE", "SLEDGE_DRYRUN", "PGPASSWORD", "PGPASSFILE",
           "HAMMER_PG_CONNECT_TIMEOUT", "GPG_TTY", "SLEDGE_PG_PASSWORD_FILE", "FAKE_GPG_GARBLE")

# Copies its input through and logs each call with its passphrase; FAKE_GPG_GARBLE=1 makes --symmetric add junk.
GPG = """#!@PY@
import json, os, sys
args = sys.argv[1:]
out = source = passphrase = None
i = 0
while i < len(args):
    a = args[i]
    if a in ("-o", "--output"):
        out = args[i + 1]
        i += 2
        continue
    if a == "--passphrase-fd":
        with os.fdopen(int(args[i + 1])) as f:
            passphrase = f.readline().rstrip("\\n")
        i += 2
        continue
    if a in ("--cipher-algo", "--pinentry-mode"):
        i += 2
        continue
    if not a.startswith("-"):
        source = a
    i += 1
with open("@LOG@", "a") as f:
    f.write(json.dumps({"args": args, "passphrase": passphrase}) + "\\n")
wanted = os.environ.get("FAKE_GPG_PASSPHRASE")
if passphrase is not None and wanted is not None and passphrase != wanted:
    sys.stderr.write("gpg: decryption failed: Bad session key\\n")
    sys.exit(2)
data = open(source, "rb").read() if source else sys.stdin.buffer.read()
if "--symmetric" in args and os.environ.get("FAKE_GPG_GARBLE"):
    data += b"garbled\\n"
if out:
    with open(out, "wb") as f:
        f.write(data)
else:
    sys.stdout.buffer.write(data)
"""


class FakePsycopg2:
    """Servers keyed by (host, port), each with one password and a set of databases."""

    class OperationalError(Exception):
        pgcode = None

    def __init__(self):
        self.servers = {}
        self.calls = []

    def serve(self, host, port, password, *dbs):
        self.servers[(host, int(port))] = (password, set(dbs))

    def connect(self, **kw):
        self.calls.append(kw)
        where = f'connection to server at "{kw.get("host")}", port {kw.get("port")} failed'
        server = self.servers.get((kw.get("host"), int(kw.get("port") or 5432)))
        if server is None:
            raise self.OperationalError(f"{where}: Connection refused\n"
                                        "\tIs the server running on that host and accepting TCP/IP connections?")
        if kw.get("password") != server[0]:
            raise self.OperationalError(f'{where}: FATAL:  password authentication failed for user "{kw.get("user")}"')
        if kw.get("dbname") not in server[1]:
            raise self.OperationalError(f'{where}: FATAL:  database "{kw.get("dbname")}" does not exist')
        return io.StringIO()


class FakeTty:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty

    def fileno(self):
        raise io.UnsupportedOperation("fileno")


class Sandbox:
    """A home, a checkout, encrypted-looking secrets and stand-in programs, driven through sledgehammer_cli.main()."""

    def __init__(self, tmp_path: Path, monkeypatch, capsys, tools: str, secrets: str = SECRETS):
        self.tmp = tmp_path
        self.mp = monkeypatch
        self.capsys = capsys
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.log = tmp_path / "calls.log"
        self.gpg_log = tmp_path / "gpg.log"
        self.repo = fake_postgres.checkout(tmp_path / "checkout", "sledge-local-postgres.sh",
                                           "sledge-secrets-create.sh", venv=True, log=self.log)
        self.pg_bin = fake_postgres.postgres(tmp_path / "pgbin", self.log)
        gpg = fake_postgres.executable(tmp_path / "gpgbin" / "gpg",
                                       GPG.replace("@PY@", sys.executable).replace("@LOG@", str(self.gpg_log)))
        private = tmp_path / "private"
        private.mkdir(mode=0o700)
        self.secrets = private / "airflow-secrets.env.gpg"
        self.prev = private / "airflow-secrets.env.gpg.prev"
        self.target = private / "postgres.env"
        if secrets is not None:
            self.secrets.write_text(secrets)
            self.secrets.chmod(0o600)
        self.port = fake_postgres.free_port()
        self.pg_root = self.home / ".sledgehammer" / "postgres"
        saved = dict(os.environ)
        for name in CLEARED:
            self.mp.delenv(name, raising=False)
        self.mp.setenv("HOME", str(self.home))
        self.mp.setenv("USER", "alice")
        self.mp.setenv("LOGNAME", "alice")
        self.mp.setenv("PATH", os.pathsep.join((str(gpg.parent), self.pg_bin, tools)))
        self.mp.setenv("SLEDGE_SECRETS_FILE", str(self.secrets))
        self.mp.setenv("SLEDGE_PG_PORT", str(self.port))
        self.mp.setenv("FAKE_GPG_PASSPHRASE", PASSPHRASE)
        self.mp.delenv("AIRFLOW_HOME", raising=False)
        self.mp.chdir(tmp_path)
        self.mp.setattr(sledgehammer_cli, "REPO", str(self.repo))
        self.db = FakePsycopg2()
        self.mp.setattr(pd_store, "psycopg2", self.db)
        self.mp.setattr("builtins.input", self._input)
        self.mp.setattr("getpass.getpass", self._getpass)
        self.mp.setattr(os, "execve", self._exec)
        self.mp.setattr(os, "execvp", self._exec)
        self._saved_env = saved
        self.answers = []
        self.prompts = []

    def restore(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def _answer(self, kind, prompt):
        print(prompt)
        self.prompts.append((kind, prompt))
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _input(self, prompt=""):
        return self._answer("input", prompt)

    def _getpass(self, prompt="Password: ", stream=None):
        return self._answer("getpass", prompt)

    @staticmethod
    def _exec(path, argv, *env):
        raise AssertionError(f"handed over to {' '.join(argv)}")

    def run(self, *args, answers=(), tty=True):
        """`sledgehammer postgres <args>` with these answers, in its own copy of the environment; (exit code, stdout, stderr)."""
        self.answers = list(answers)
        self.prompts = []
        self.mp.setattr(sys, "stdin", FakeTty(tty))
        self.mp.setattr(sys, "argv", ["sledgehammer", "postgres", *args])
        environ = dict(os.environ)
        try:
            rc = sledgehammer_cli.main()
        except SystemExit as e:
            rc = e.code
        finally:
            os.environ.clear()
            os.environ.update(environ)
        out, err = self.capsys.readouterr()
        return rc, out, err

    def gpg_calls(self):
        if not self.gpg_log.exists():
            return []
        return [json.loads(line) for line in self.gpg_log.read_text().splitlines()]

    def secrets_lines(self):
        return dict(line.split("=", 1) for line in self.secrets.read_text().splitlines() if "=" in line)

    def target_lines(self):
        return dict(line.split("=", 1) for line in self.target.read_text().splitlines()
                    if "=" in line and not line.startswith("#"))

    def local_password(self):
        return (self.pg_root / "password").read_text().strip()

    def calls(self, program=""):
        return fake_postgres.calls(self.log, program)


def mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def row(out: str, block: str, name: str):
    """The value and source on a `name` row of the block that starts with `block`."""
    text = out.split(block, 1)[1]
    match = re.search(rf"^  {name} +(\S+)(?: +(.*))?$", text, re.M)
    assert match, out
    return match.group(1), (match.group(2) or "").strip()
