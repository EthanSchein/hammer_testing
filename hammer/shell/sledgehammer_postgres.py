"""sledgehammer postgres: see and change which Postgres SledgeHammer uses.

  sledgehammer postgres show
      Where Airflow's database and the cache point (host, port, database,
      user), where each value comes from, whether each answers, and the state
      of your local Postgres. Never prints a password.
  sledgehammer postgres use local
      Switch Airflow and the cache to your own Postgres (the one
      scripts/sledge-local-postgres.sh keeps in ~/.sledgehammer/postgres),
      creating and starting it if needed.
  sledgehammer postgres configure
      Switch them to any other Postgres, such as a lab server or another
      port, asking for each setting in turn.

A switch shows what changes and asks before it touches anything, twice when
Airflow or the cache moves to another server or database. It rewrites
AIRFLOW__DATABASE__SQL_ALCHEMY_CONN and HAMMER_PG_PASSWORD in the encrypted
secrets under the same passphrase, keeps the old file as <secrets>.prev, and
writes the host, port, user and cache database (never the password) to
postgres.env beside it, where studio and hammer-vlsi find them outside
Airflow. DAG run history and cached results stay on the old server. With
SLEDGE_DRYRUN=1, use local and configure stop after showing what would
change, so nothing is changed or started.
"""
import getpass
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import quote

from hammer.shell import sledgehammer_cli
from hammer.vlsi import pd_store

CONN = "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"
PASSWORD = "HAMMER_PG_PASSWORD"
PG_LINES = ("HAMMER_PG_HOST", "HAMMER_PG_PORT", "HAMMER_PG_DB", "HAMMER_PG_USER")
WATCHED = (CONN, *PG_LINES, PASSWORD)
FIELD_VARS = {"host": "HAMMER_PG_HOST", "port": "HAMMER_PG_PORT", "dbname": "HAMMER_PG_DB",
              "user": "HAMMER_PG_USER", "password": PASSWORD}
SHOWN = (("host", "host"), ("port", "port"), ("dbname", "database"), ("user", "user"))
CACHE_DB = "sledgehammer_studio"
PROBE_TIMEOUT = 5
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
HOST_RE = re.compile(r"[A-Za-z0-9_.:-]+\Z")
NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.$-]*\Z")
MERGE_STEPS = '"Merging back into the shared database" in readme_documents/LOCAL_POSTGRES_SETUP.md'
SHELL = "this shell's environment"
SERVER = "the server sledgehammer starts"
COMMANDS = "the other sledgehammer commands"
SWITCH_LOCAL = "Switch SledgeHammer to your local Postgres? [y/N]: "


class Target(NamedTuple):
    host: str
    port: int
    user: str
    airflow_db: str
    cache_db: str

    def conn(self, password: str) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return (f"postgresql+psycopg2://{quote(self.user, safe='')}:{quote(password, safe='')}"
                f"@{host}:{self.port}/{self.airflow_db}")


class _View(NamedTuple):
    who: str
    env: Dict[str, str]
    src: Dict[str, str]


def _script(name: str) -> str:
    return os.path.join(sledgehammer_cli.REPO, "scripts", name)


def _err(message: str) -> None:
    print(f"[sledgehammer] {message}", file=sys.stderr)


def _where(user, host, port, db) -> str:
    return f"{user}@{_server(host, port)}/{db}"


def _conn_where(conn: Dict) -> str:
    return _where(conn.get("user"), conn.get("host"), conn.get("port"), conn.get("dbname"))


def _fields_where(fields: Dict) -> str:
    return _where(fields["user"][0], fields["host"][0], fields["port"][0], fields["dbname"][0])


def _server(host, port) -> str:
    return f"[{host}]:{port}" if ":" in str(host) else f"{host}:{port}"


def _same_server(a: Tuple, b: Tuple) -> bool:
    def norm(host) -> str:
        host = str(host or "").lower()
        return "localhost" if host in LOCAL_HOSTS else host
    return norm(a[0]) == norm(b[0]) and str(a[1]) == str(b[1])


def _who(names: List[str]) -> str:
    if len(names) == 4:
        return "everything started from this shell"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _last_line(data: bytes) -> str:
    lines = data.decode("utf-8", "replace").strip().splitlines()
    return lines[-1] if lines else "no message"


def _parse_env(text: str) -> Dict[str, str]:
    """KEY=value lines, read the way the launcher reads the secrets."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def _env_value(value: str) -> str:
    """value as a secrets line holds it: quoted when the loaders would otherwise trim it."""
    if value[:1] in ("'", '"') or value[-1:] in ("'", '"') or value != value.strip():
        return f"'{value}'"
    return value


def _replace(text: str, values: Dict[str, str]) -> str:
    """text with the line of each key in values rewritten, and any missing key appended; other lines are left as they are."""
    out: List[str] = []
    seen = set()
    for line in text.splitlines(keepends=True):
        body = line.strip()
        prefix = "export " if body.startswith("export ") else ""
        key = body[len(prefix):].split("=", 1)[0].strip() if "=" in body and not body.startswith("#") else ""
        if key in values:
            ending = line[len(line.rstrip("\r\n")):] or "\n"
            out.append(f"{prefix}{key}={values[key]}{ending}")
            seen.add(key)
        else:
            out.append(line)
    missing = [f"{key}={value}\n" for key, value in values.items() if key not in seen]
    if missing and out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    return "".join(out + missing)


def _gpg(args: List[str], passphrase: str, data: Optional[bytes] = None) -> subprocess.CompletedProcess:
    """Run gpg with the passphrase on a pipe, never in argv or the environment."""
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, passphrase.encode("utf-8") + b"\n")
    finally:
        os.close(write_end)
    stdin = {"input": data} if data is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(["gpg", "--batch", "--yes", "--quiet", "--no-symkey-cache", "--pinentry-mode", "loopback",
                               "--passphrase-fd", str(read_end), *args],
                              capture_output=True, pass_fds=(read_end,), **stdin)
    finally:
        os.close(read_end)


def _private_temp(directory: str) -> str:
    fd, path = tempfile.mkstemp(prefix=".sledge-", dir=directory)
    os.close(fd)
    return path


def _fsync(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_private(src: str, dst: str) -> None:
    tmp = _private_temp(os.path.dirname(dst) or ".")
    try:
        shutil.copyfile(src, tmp)
        _fsync(tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _encrypt_into(path: str, text: str, passphrase: str) -> None:
    """Encrypt text over path under passphrase, keeping the old file as path.prev; each lands whole, mode 600."""
    data = text.encode("utf-8", "surrogateescape")
    new = _private_temp(os.path.dirname(path) or ".")
    try:
        res = _gpg(["--symmetric", "--cipher-algo", "AES256", "-o", new], passphrase, data)
        if res.returncode != 0:
            raise RuntimeError(f"gpg could not encrypt them: {_last_line(res.stderr)}")
        check = _gpg(["--decrypt", new], passphrase)
        if check.returncode != 0 or check.stdout != data:
            raise RuntimeError("the newly encrypted file did not decrypt back to the new secrets")
        os.chmod(new, 0o600)
        _fsync(new)
        _copy_private(path, path + ".prev")
        os.replace(new, path)
    finally:
        if os.path.exists(new):
            os.unlink(new)


def _shred(path: str) -> None:
    if subprocess.run(["shred", "-u", path], capture_output=True).returncode == 0:
        return
    try:
        with open(path, "r+b") as f:
            f.write(b"\0" * os.path.getsize(path))
            f.flush()
            os.fsync(f.fileno())
    finally:
        os.unlink(path)


def _first_line(path: str) -> str:
    with open(path) as f:
        return f.readline().rstrip("\n")


def _probe(settings: Dict) -> Tuple[Optional[bool], str]:
    """(True, ""), (False, why) or (None, why not checked) for a login with these psycopg2 settings."""
    driver = pd_store.psycopg2
    if driver is None:
        return None, "psycopg2 is not installed"
    timeout = pd_store._connect_timeout() if os.environ.get("HAMMER_PG_CONNECT_TIMEOUT") else PROBE_TIMEOUT
    try:
        driver.connect(**{**settings, "connect_timeout": timeout}).close()
    except Exception as e:
        lines = [line.strip() for line in str(e).splitlines() if line.strip()]
        return False, lines[0] if lines else type(e).__name__
    return True, ""


def _reachable(settings: Dict) -> str:
    ok, why = _probe(settings)
    if ok:
        return "yes"
    return f"no ({why})" if ok is False else f"not checked ({why})"


def _row(name: str, value, source: str = "") -> None:
    print(f"  {name:<10}{str(value):<31} {source}".rstrip())


def _yes(prompt: str) -> bool:
    return input(prompt).strip().lower() in ("y", "yes")


def _dryrun() -> bool:
    if not os.environ.get("SLEDGE_DRYRUN"):
        return False
    print("[dryrun] SLEDGE_DRYRUN is set, so this stops here; nothing was changed or started.")
    return True


class _Session:
    """What this shell, the stack env file and, once decrypted, the secrets say about Postgres."""

    def __init__(self) -> None:
        self.shell = dict(os.environ)
        self.stack_file = sledgehammer_cli._load_stack_env()
        self.stack = {var: value for var, value in os.environ.items() if var not in self.shell}
        self.stack_label = f"the stack env file {self.stack_file}"
        os.environ.setdefault("AIRFLOW_HOME", sledgehammer_cli.REPO)
        self.overrides: Dict[str, str] = {}
        for var in WATCHED:
            if self.shell.get(var):
                self.overrides[var] = SHELL
            elif self.stack.get(var):
                self.overrides[var] = self.stack_label
        self.secrets_file = pd_store.secrets_file()
        self.secrets_label = f"the encrypted secrets {self.secrets_file}"
        self.target_file = pd_store.postgres_target_file(self.secrets_file)
        self.target = pd_store.postgres_target(self.target_file)
        self.plaintext: Optional[str] = None
        self.passphrase: Optional[str] = None
        self.secrets: Dict[str, str] = {}
        self.unread = ""
        self.applying = False

    def unlock(self) -> bool:
        """Decrypt the secrets with a passphrase asked for here; True when there are none to decrypt."""
        if not os.path.exists(self.secrets_file):
            return True
        if shutil.which("gpg") is None:
            self.unread = "gpg is not on PATH"
            _err(f"{self.unread}, so {self.secrets_file} cannot be read")
            return False
        res = None
        for attempt in range(1, 4):
            passphrase = getpass.getpass(f"GPG passphrase for {self.secrets_file}: ")
            res = _gpg(["--decrypt", self.secrets_file], passphrase)
            if res.returncode == 0:
                self.passphrase = passphrase
                self.plaintext = res.stdout.decode("utf-8", "surrogateescape")
                self.secrets = _parse_env(self.plaintext)
                return True
            if attempt < 3:
                _err(f"that passphrase did not work ({attempt}/3); try again")
        self.unread = f"gpg could not decrypt it ({_last_line(res.stderr)})"
        _err(f"could not decrypt {self.secrets_file}: {_last_line(res.stderr)}")
        return False

    def views(self, secrets: Optional[Dict[str, str]] = None) -> List[_View]:
        """The environment of the server sledgehammer starts, the other sledgehammer commands, studio and hammer-vlsi."""
        secrets = self.secrets if secrets is None else secrets
        out = []
        for who, with_stack, with_secrets in ((SERVER, False, True), (COMMANDS, True, True),
                                              ("studio", True, False), ("hammer-vlsi", False, False)):
            src = dict.fromkeys(self.stack, self.stack_label) if with_stack else {}
            src.update(dict.fromkeys(self.shell, SHELL))
            env = {var: self.shell[var] if where == SHELL else self.stack[var] for var, where in src.items()}
            if with_secrets and not env.get(CONN):
                env.update(secrets)
                src.update(dict.fromkeys(secrets, self.secrets_label))
            out.append(_View(who, env, src))
        return out

    def airflow(self, view: _View) -> Tuple[Dict, str]:
        """Airflow's connection settings in view, password included, and where they come from."""
        if view.env.get(CONN):
            return pd_store._parse_conn_uri(view.env[CONN]), view.src[CONN]
        cfg = pd_store._parse_airflow_cfg_conn()
        if cfg:
            return cfg, f"airflow.cfg ({pd_store._find_airflow_cfg()})"
        return {}, ""

    def label(self, view: _View, field: str, source: str) -> str:
        if source == "env":
            return view.src.get(FIELD_VARS[field], SHELL)
        if source == "conn":
            return f"Airflow's connection, from {view.src.get(CONN, SHELL)}"
        return {"target": self.target_file, "cfg": "airflow.cfg", "default": "the built-in fallback",
                "libpq": "libpq's default for a connection without a port (PGPORT, else 5432)"}[source]

    def reach(self, views: List[_View]) -> List[Tuple[str, str, str]]:
        """(variable, where it is set, who takes it) for each override and each HAMMER_PG_* line of the secrets."""
        found = list(self.overrides.items()) + [(var, self.secrets_label) for var in PG_LINES if self.secrets.get(var)]
        out = []
        for var, where in found:
            who = [view.who for view in views if view.env.get(var) and view.src.get(var) == where]
            if who:
                out.append((var, where, _who(who)))
        return out

    def local_dir(self) -> str:
        pg_dir = (os.environ.get("SLEDGE_PG_DIR") or self.target.get("SLEDGE_PG_DIR")
                  or self.secrets.get("SLEDGE_PG_DIR")
                  or os.path.join(os.path.expanduser("~"), ".sledgehammer", "postgres"))
        return os.path.abspath(os.path.expanduser(pg_dir))


def _local(pg_dir: str, *args: str, capture: bool = True) -> subprocess.CompletedProcess:
    sys.stdout.flush()
    return subprocess.run(["bash", _script("sledge-local-postgres.sh"), *args],
                          env=dict(os.environ, SLEDGE_PG_DIR=pg_dir), capture_output=capture, text=True)


def _local_settings(pg_dir: str) -> Optional[Dict[str, str]]:
    res = _local(pg_dir, "env")
    if res.returncode != 0:
        return None
    return dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)


def _port_busy(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            socket.create_connection((host, port), timeout=1).close()
            return True
        except (OSError, OverflowError):
            pass
    return False


def _free_port(first: int) -> int:
    """The port sledge-local-postgres.sh setup gives a new cluster: first, or the next one nothing listens on."""
    return next((port for port in range(first, first + 101) if not _port_busy(port)), first)


def _password_source(session: _Session, view: _View, fields: Dict) -> Optional[str]:
    if "password" in fields:
        return f"from {session.label(view, 'password', fields['password'][1])} (not shown)"
    if view.env.get("PGPASSWORD"):
        return "from PGPASSWORD (not shown)"
    pgpass = view.env.get("PGPASSFILE") or os.path.join(os.path.expanduser("~"), ".pgpass")
    if os.path.isfile(pgpass):
        return f"from {pgpass}, if it has a line for this server"
    if pd_store.local_password(fields, view.env, session.target)[0] is not None:
        return f"from {os.path.join(pd_store.local_postgres_dir(view.env, session.target), 'password')} (not shown)"
    return None


def _no_password(session: _Session, views: List[_View]) -> None:
    """Say when studio or hammer-vlsi in this shell has no password for the cache, and where it would find one."""
    lacking: Dict[Tuple[str, str, str], List[str]] = {}
    for view in views[2:]:
        theirs = pd_store.pg_fields(view.env, session.target)
        if _password_source(session, view, theirs) is None:
            why = pd_store.local_password(theirs, view.env, session.target)[1]
            server = _server(theirs["host"][0], theirs["port"][0])
            lacking.setdefault((server, view.env.get("PGPASSFILE") or "~/.pgpass", why), []).append(view.who)
    env_file = pd_store.stack_env_file()
    loads = (f"\"source {env_file}\" loads if that file sets it" if env_file
             else "\"source venv.sh\" loads from the encrypted secrets")
    for (server, pgpass, why), who in lacking.items():
        they = "they" if len(who) > 1 else "it"
        print(f"  {_who(who)} in this shell {'have' if len(who) > 1 else 'has'} no password for the cache, so "
              f"{they} cannot log in; {they} would take one from HAMMER_PG_PASSWORD, which {loads}, "
              f"or from a line for {server} in {pgpass}")
        if why:
            print(f"  {why}")


def _outside(session: _Session, views: List[_View], fields: Dict) -> None:
    """Say where studio and hammer-vlsi in this shell go when that is not where the DAG tasks go."""
    differ = []
    for view in views[2:]:
        theirs = pd_store.pg_fields(view.env, session.target)
        if any(theirs[field][0] != fields[field][0] for field, _ in SHOWN):
            differ.append((view, theirs))
    if len(differ) == 2 and all(differ[0][1][field][0] == differ[1][1][field][0] for field, _ in SHOWN):
        differ = [(differ[0][0]._replace(who="studio and hammer-vlsi"), differ[0][1])]
    for view, theirs in differ:
        print(f"  {view.who} in this shell {'use' if ' and ' in view.who else 'uses'} {_fields_where(theirs)} instead "
              f"(host from {session.label(view, 'host', theirs['host'][1])})")
    if differ and all(theirs[field][1] in ("target", "cfg", "default") and fields[field][1] != "env"
                      for view, theirs in differ for field, _ in SHOWN if theirs[field][0] != fields[field][0]):
        print("  sledgehammer postgres configure, given the values above, makes them follow")


def _show(session: _Session) -> int:
    if os.path.exists(session.secrets_file):
        if sys.stdin.isatty():
            session.unlock()
        else:
            session.unread = "no terminal to ask for its passphrase"
    views = session.views()
    server = views[0]
    airflow, airflow_source = session.airflow(server)
    print("Airflow's database (the server sledgehammer starts)")
    if airflow:
        for field, name in SHOWN:
            _row(name, airflow.get(field, ""), airflow_source)
        _row("reachable", _reachable(airflow))
        hidden = pd_store._parse_conn_uri(session.secrets.get(CONN, ""))
        if session.overrides.get(CONN) == SHELL and hidden:
            print(f"  this wins over the encrypted secrets, which name {_conn_where(hidden)}")
    elif server.env.get(CONN):
        print(f"  {CONN} from {airflow_source} is not a Postgres URL this command can read")
    elif not session.unread:
        print(f"  not set: no {CONN} in the environment, the encrypted secrets or airflow.cfg")
    other, other_source = session.airflow(views[1])
    if other and any(other.get(field) != airflow.get(field) for field, _ in SHOWN):
        print(f"  {COMMANDS} (run, status, the airflow passthrough) use {_conn_where(other)} instead, "
              f"from {other_source}")
    if session.unread:
        print(f"  {session.secrets_file} was not read: {session.unread}")

    print("Cache (DAG tasks on that server)")
    fields = pd_store.pg_fields(server.env, session.target)
    for field, name in SHOWN:
        value, source = fields[field]
        _row(name, value, session.label(server, field, source))
    _row("password", _password_source(session, server, fields)
         or "none found; set HAMMER_PG_PASSWORD or add a line to ~/.pgpass")
    try:
        _row("reachable", _reachable(pd_store._pg_settings(server.env, session.target)))
    except pd_store.DatabaseUnavailable:
        _row("reachable", "not checked (no password)")
    _outside(session, views, fields)
    _no_password(session, views)

    airflow_port = airflow.get("port") or pd_store.libpq_default_port(server.env)
    if airflow and not _same_server((airflow.get("host"), airflow_port), (fields["host"][0], fields["port"][0])):
        print(f"NOTE: Airflow and the cache point at different servers: Airflow at "
              f"{_server(airflow.get('host'), airflow_port)}, the cache at "
              f"{_server(fields['host'][0], fields['port'][0])}.")
    for var, where, who in session.reach(views):
        if where == session.secrets_label:
            print(f"NOTE: {var} is set in {where}. It wins over Airflow's connection and {session.target_file} "
                  f"for {who}, and sledgehammer postgres leaves it as it is.")
        else:
            print(f"NOTE: {var} is set in {where}. It wins over the encrypted secrets and {session.target_file} "
                  f"for {who}.")

    pg_dir = session.local_dir()
    if os.path.isfile(os.path.join(pg_dir, "data", "PG_VERSION")):
        res = _local(pg_dir, "status")
        lines = (res.stdout.strip() or res.stderr.strip()).splitlines()
        print(f"Local Postgres: {lines[-1] if lines else 'sledge-local-postgres.sh status said nothing'}")
    else:
        print(f"Local Postgres: none in {pg_dir} (sledgehammer postgres use local creates one)")
    return 0


def _warn(session: _Session, views: List[_View]) -> None:
    for var, where, who in session.reach(views):
        if where == session.secrets_label:
            print(f"WARNING: {var} is set in {where}, so it keeps winning for {who}: this switch rewrites only "
                  f"{CONN} and {PASSWORD} there. Remove it from the secrets (see \"Rotating a secret\" in "
                  f"readme_documents/SECRETS.md) to let the switch reach them.")
        else:
            undo = "until you unset it" if where == SHELL else "until you remove it from that file"
            print(f"WARNING: {var} is set in {where}, so it keeps winning over this switch for {who} {undo}.")


def _moved(a: Tuple, b: Tuple) -> bool:
    return not _same_server(a[:2], b[:2]) or a[2] != b[2]


def _plan(session: _Session, new: Target, takes_over: Optional[str] = None) -> Optional[str]:
    """Print what the switch changes and its warnings; return what stays behind, or None when nothing moves."""
    after = session.views({**session.secrets, CONN: new.conn("-"), PASSWORD: "-"})
    old = pd_store._parse_conn_uri(session.secrets.get(CONN, ""))
    cache = pd_store.pg_fields(session.views()[0].env, session.target)
    then = pd_store.pg_fields(after[0].env, _target_values(new, None))
    if old:
        old_airflow = _conn_where(old)
    else:
        old_airflow = "nothing yet" if session.plaintext is None else "nothing in the encrypted secrets"
    print("This switch changes:")
    print(f"  Airflow  {old_airflow}")
    print(f"       ->  {_where(new.user, new.host, new.port, new.airflow_db)}")
    print(f"  cache    {_fields_where(cache)}")
    print(f"       ->  {_fields_where(then)}")
    if session.plaintext is not None:
        print(f"It rewrites {CONN} and {PASSWORD} in {session.secrets_file}, keeping the current file as "
              f"{session.secrets_file}.prev, and writes {session.target_file}.")
    else:
        print(f"It creates {session.secrets_file} with new Airflow keys, under a passphrase gpg asks you "
              f"to choose, and writes {session.target_file}.")
    _warn(session, after)
    left = []
    airflow_was = (old.get("host"), old.get("port"), old.get("dbname"))
    if old and (takes_over or _moved(airflow_was, (new.host, new.port, new.airflow_db))):
        left.append(("DAG run history", "stays", airflow_was))
    cache_was = tuple(cache[field][0] for field in ("host", "port", "dbname"))
    cache_set = any(cache[field][1] != "default" for field in ("host", "port", "dbname"))
    if (takes_over and cache_set) or _moved(cache_was, tuple(then[field][0] for field in ("host", "port", "dbname"))):
        left.append(("cached results", "stay", cache_was))
    if not left:
        return None
    here = _server(new.host, new.port)
    on_new = [_same_server(was[:2], (new.host, new.port)) for _, _, was in left]
    if takes_over:
        places = [f"the Postgres that served {here} before" if same else _server(*was[:2])
                  for (_, _, was), same in zip(left, on_new)]
    else:
        places = [_server(*was[:2]) + (f"/{was[2]}" if same else "") for (_, _, was), same in zip(left, on_new)]
    if len(left) == 2 and places[0] == places[1]:
        stay = f"DAG run history and cached results stay on {places[0]}"
    else:
        stay = " and ".join(f"{what} {verb} on {place}" for (what, verb, _), place in zip(left, places))
    if takes_over and any(on_new):
        fresh = (f"Your local Postgres in {takes_over} starts empty unless it already holds them, and it takes over "
                 f"{here}, so the old one cannot start while yours runs")
    elif not all(on_new):
        fresh = f"{here} starts empty unless it already holds them"
    elif len(left) == 1:
        fresh = "The new database starts empty unless it already holds them"
    else:
        fresh = "The new databases start empty unless they already hold them"
    print(f"WARNING: {stay}. {fresh}. To merge them later, see {MERGE_STEPS}.")
    return stay


def _create_secrets(session: _Session, new: Target, password: str, password_file: Optional[str],
                    pg_dir: Optional[str]) -> bool:
    """Make the secrets file with sledge-secrets-create.sh, which generates the Airflow keys."""
    tmpdir = None
    if password_file is None:
        tmpdir = tempfile.mkdtemp(prefix="sledge-pg-", dir="/dev/shm" if os.access("/dev/shm", os.W_OK) else None)
        password_file = os.path.join(tmpdir, "password")
        with os.fdopen(os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(password + "\n")
    env = dict(os.environ, SLEDGE_SECRETS_FILE=session.secrets_file, SLEDGE_PG_USER=new.user,
               SLEDGE_PG_DB=new.airflow_db, SLEDGE_PG_HOST=new.host, SLEDGE_PG_PORT=str(new.port),
               SLEDGE_PG_PASSWORD_FILE=password_file)
    env.pop("SLEDGE_PG_DIR", None)
    if pg_dir:
        env["SLEDGE_PG_DIR"] = pg_dir
    try:
        env.setdefault("GPG_TTY", os.ttyname(sys.stdin.fileno()))
    except Exception:
        pass
    sys.stdout.flush()
    try:
        res = subprocess.run(["bash", _script("sledge-secrets-create.sh")], env=env)
        return res.returncode == 0 and os.path.exists(session.secrets_file)
    finally:
        if tmpdir:
            _shred(password_file)
            shutil.rmtree(tmpdir, ignore_errors=True)


def _target_values(new: Target, pg_dir: Optional[str]) -> Dict[str, str]:
    values = {"HAMMER_PG_HOST": new.host, "HAMMER_PG_PORT": str(new.port), "HAMMER_PG_USER": new.user,
              "HAMMER_PG_DB": new.cache_db}
    if pg_dir:
        values["SLEDGE_PG_DIR"] = pg_dir
    return values


def _write_target(path: str, values: Dict[str, str]) -> None:
    tmp = _private_temp(os.path.dirname(path) or ".")
    try:
        with open(tmp, "w") as f:
            f.write("# Written by sledgehammer postgres; the password stays in the encrypted secrets.\n")
            f.write("".join(f"{key}={value}\n" for key, value in values.items()))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _holds(secrets: Dict[str, str], new: Target, password: str) -> bool:
    """Whether both lines a switch rewrites already name new with this password."""
    old = pd_store._parse_conn_uri(secrets.get(CONN, ""))
    return (_same_server((old.get("host"), old.get("port")), (new.host, new.port))
            and (old.get("user"), old.get("dbname"), old.get("password")) == (new.user, new.airflow_db, password)
            and secrets.get(PASSWORD) == password)


def _another_server(secrets: Dict[str, str], new: Target, password: Optional[str]) -> bool:
    """Whether the secrets name new's address with a password that this local Postgres does not take."""
    old = pd_store._parse_conn_uri(secrets.get(CONN, ""))
    return (bool(old) and _same_server((old.get("host"), old.get("port")), (new.host, new.port))
            and old.get("password") != password)


def _apply(session: _Session, new: Target, password: str, password_file: Optional[str] = None,
           pg_dir: Optional[str] = None, rewrite: bool = True) -> int:
    session.applying = True
    os.makedirs(os.path.dirname(session.secrets_file) or ".", mode=0o700, exist_ok=True)
    if rewrite and session.plaintext is not None:
        try:
            _encrypt_into(session.secrets_file,
                          _replace(session.plaintext, {CONN: new.conn(password), PASSWORD: _env_value(password)}),
                          session.passphrase)
        except (OSError, RuntimeError) as e:
            _err(f"the secrets are unchanged: {e}")
            return 1
        print(f"Rewrote {session.secrets_file} (the previous one is {session.secrets_file}.prev).")
    elif rewrite and not _create_secrets(session, new, password, password_file, pg_dir):
        _err(f"could not create {session.secrets_file}; postgres.env is unchanged")
        return 1
    try:
        _write_target(session.target_file, _target_values(new, pg_dir))
    except OSError as e:
        _err(f"the secrets now name {_server(new.host, new.port)}, but {session.target_file} could not be written "
             f"({e}), so studio and hammer-vlsi outside Airflow do not follow yet; rerun this command")
        return 1
    print(f"Wrote {session.target_file}.")
    login = ("HAMMER_PG_PASSWORD, a ~/.pgpass line or, on the host your local Postgres runs on, its password file"
             if pg_dir else "HAMMER_PG_PASSWORD or a ~/.pgpass line")
    if not rewrite:
        print(f"Outside Airflow, studio and hammer-vlsi now follow it too and log in with {login}.")
        return 0
    print(f"SledgeHammer now uses {_server(new.host, new.port)} as {new.user}: Airflow database {new.airflow_db}, "
          f"cache database {new.cache_db}.")
    print("Restart a running sledgehammer server so Airflow picks it up. Outside Airflow, studio and hammer-vlsi "
          f"follow now and log in with {login}.")
    return 0


def _confirm(first: str, stay: Optional[str]) -> bool:
    return _yes(first) and (stay is None or input('Type "local" to confirm the switch: ').strip() == "local")


def _local_target(local: Dict[str, str]) -> Target:
    return Target("localhost", int(local["SLEDGE_PG_PORT"]), local["SLEDGE_PG_USER"], local["SLEDGE_PG_DB"],
                  local["SLEDGE_PG_CACHE_DB"])


def _use_local(session: _Session) -> int:
    if not session.unlock():
        return 1
    pg_dir = session.local_dir()
    local = _local_settings(pg_dir) or {}
    user = local.get("SLEDGE_PG_USER") or os.environ.get("USER") or getpass.getuser()
    try:
        port = int(local.get("SLEDGE_PG_PORT") or os.environ.get("SLEDGE_PG_PORT") or 5544)
    except ValueError:
        port = 5544
    if not local:
        port = _free_port(port)
    new = Target("localhost", port, user, local.get("SLEDGE_PG_DB") or f"airflow_{user}",
                 local.get("SLEDGE_PG_CACHE_DB") or CACHE_DB)
    password = None
    if local:
        try:
            password = _first_line(local["SLEDGE_PG_PASSWORD_FILE"])
        except OSError:
            pass
    held = password is not None and _holds(session.secrets, new, password)
    takes_over = pg_dir if not local or _another_server(session.secrets, new, password) else None
    if held and session.target == _target_values(new, pg_dir):
        print(f"SledgeHammer already uses your local Postgres in {pg_dir}.")
        _warn(session, session.views())
        if _dryrun():
            return 0
        return _local(pg_dir, "setup", capture=False).returncode
    if held:
        print(f"Your encrypted secrets already point at your local Postgres in {pg_dir}, but studio and hammer-vlsi "
              f"outside Airflow do not follow it yet. This writes {session.target_file} and starts your local "
              f"Postgres if it is stopped.")
        _warn(session, session.views())
        stay = None
    else:
        stay = _plan(session, new, takes_over)
        if local:
            print(f"Your local Postgres in {pg_dir} is already set up; it is started if it is stopped.")
        else:
            print(f"Your local Postgres is created in {pg_dir} on port {port} and started.")
    if _dryrun():
        return 0
    if not _confirm(f"Write {session.target_file}? [y/N]: " if held else SWITCH_LOCAL, stay):
        print("Nothing changed.")
        return 1
    session.applying = True
    if _local(pg_dir, "setup", capture=False).returncode != 0:
        _err("the local Postgres setup did not finish; the secrets and postgres.env are unchanged")
        return 1
    local = _local_settings(pg_dir)
    if not local:
        _err(f"no local Postgres in {pg_dir} after its setup; the secrets and postgres.env are unchanged")
        return 1
    shown = new
    new = _local_target(local)
    if new != shown:
        print(f"Your local Postgres was set up as {_where(new.user, new.host, new.port, new.airflow_db)}, not "
              f"{_where(shown.user, shown.host, shown.port, shown.airflow_db)}, so check the switch again.")
        if not _confirm(SWITCH_LOCAL, _plan(session, new, takes_over)):
            print(f"The secrets and {session.target_file} are unchanged; your new local Postgres in {pg_dir} "
                  f"keeps running.")
            return 1
    password_file = local["SLEDGE_PG_PASSWORD_FILE"]
    password = _first_line(password_file)
    rc = _apply(session, new, password, password_file, pg_dir, rewrite=not _holds(session.secrets, new, password))
    if rc == 0:
        print(f"Its password is in {password_file}.")
    return rc


def write_local_target(pg_dir: str) -> int:
    """Write postgres.env for the local Postgres in pg_dir as use local does, without asking; uv_setup.sh runs it."""
    pg_dir = os.path.abspath(os.path.expanduser(pg_dir))
    target_file = pd_store.postgres_target_file()
    local = _local_settings(pg_dir)
    if not local:
        _err(f"no local Postgres in {pg_dir}, so {target_file} was not written")
        return 1
    values = _target_values(_local_target(local), pg_dir)
    if os.path.exists(target_file) and pd_store.postgres_target(target_file) != values:
        print(f"  {target_file} already holds other Postgres settings, so it is left as it is. To point studio and "
              f"hammer-vlsi outside Airflow at your local Postgres, run: sledgehammer postgres use local")
        return 0
    try:
        _write_target(target_file, values)
    except OSError as e:
        _err(f"{target_file} could not be written ({e})")
        return 1
    print(f"  wrote {target_file}")
    return 0


def _ask(label: str, default, check) -> str:
    while True:
        reply = input(f"  {label} [{default}]: ").strip() or str(default)
        problem = check(reply)
        if problem is None:
            return reply
        print(f"  {problem}")


def _check_host(value: str) -> Optional[str]:
    return None if HOST_RE.match(value) else "a host name or IP address, such as db.example.edu"


def _check_port(value: str) -> Optional[str]:
    return None if value.isdigit() and 0 < int(value) < 65536 else "a port number from 1 to 65535"


def _check_name(value: str) -> Optional[str]:
    return None if NAME_RE.match(value) else "letters, digits and _ . $ -, starting with a letter, digit or _"


def _ask_password() -> str:
    while True:
        first = getpass.getpass("  password: ")
        if first and getpass.getpass("  password again: ") == first:
            return first
        print("  empty or mismatched; try again")


def _configure(session: _Session) -> int:
    if not session.unlock():
        return 1
    old = pd_store._parse_conn_uri(session.secrets.get(CONN, ""))
    cache = pd_store.pg_fields(session.views()[0].env, session.target)
    print("The Postgres for Airflow and the cache (Enter keeps the value in brackets):")
    host = _ask("host", next(h for h in (old.get("host"), cache["host"][0], "localhost")
                             if h and not _check_host(str(h))), _check_host)
    port = int(_ask("port", old.get("port") or cache["port"][0], _check_port))
    user = _ask("user", old.get("user") or cache["user"][0], _check_name)
    airflow_db = _ask("Airflow database", old["dbname"] if old.get("dbname") and old.get("user") == user
                      else f"airflow_{user}", _check_name)
    cache_db = _ask("cache database", cache["dbname"][0], _check_name)
    password = _ask_password()
    new = Target(host, port, user, airflow_db, cache_db)
    print(f"Testing {_server(host, port)} as {user}:")
    ok = True
    for name, db in (("Airflow database", airflow_db), ("cache database", cache_db)):
        result = _reachable({"host": host, "port": port, "user": user, "password": password, "dbname": db})
        print(f"  {name} {db}: reachable {result}")
        ok = ok and result == "yes"
    if not ok and not _yes("Continue anyway? [y/N]: "):
        print("Nothing changed.")
        return 1
    stay = _plan(session, new)
    if _dryrun():
        return 0
    if not _yes(f"Switch SledgeHammer to {_server(host, port)}? [y/N]: ") \
            or (stay is not None and not _yes(f"Your {stay}. Switch anyway? [y/N]: ")):
        print("Nothing changed.")
        return 1
    return _apply(session, new, password)


def main(args: List[str]) -> int:
    if not args or args[0] in sledgehammer_cli.HELP_WORDS:
        sys.stdout.write(__doc__)
        return 0
    command = {("show",): _show, ("use", "local"): _use_local, ("configure",): _configure}.get(tuple(args))
    if command is None:
        if args[0] == "use":
            _err("postgres use takes only local; for any other Postgres run: sledgehammer postgres configure")
        else:
            _err(f"no such command: sledgehammer postgres {' '.join(args)} (see sledgehammer postgres --help)")
        return 2
    if command is not _show and not sys.stdin.isatty():
        _err(f"postgres {' '.join(args)} needs a terminal to ask you to confirm the switch; nothing changed")
        return 1
    session = _Session()
    try:
        return command(session)
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        if session.applying:
            _err("stopped part way through the switch; check it with: sledgehammer postgres show")
        elif command is not _show:
            _err("stopped; nothing changed")
        return 1
