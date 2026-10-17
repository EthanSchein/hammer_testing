#!/usr/bin/env python3
"""
Starts this checkout's Airflow stack: `airflow standalone`'s components for the
full stack, or scripts/sledge-lite-supervisor.sh for the lite one. Either way
the UI has one login, the user who launched it, with its password in
.sledgehammer/airflow-ui-password.json.

Usage:
    source ./venv.sh
    export PATH=$(pwd)/.venv/bin:$PATH
    ./scripts/airflow-standalone-ldap.py
"""

import os
import sys

# Re-exec under this checkout's venv python if we're not already using it, so
# running the script directly (./scripts/airflow-standalone-ldap.py) works even
# when the venv isn't activated. Otherwise it runs under whatever python3 is on
# PATH (e.g. conda base), which has no airflow -> ModuleNotFoundError. This runs
# before anything else, so nothing (like the GPG decrypt) happens twice.
_venv_py = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv", "bin", "python")
if os.path.exists(_venv_py) and os.path.realpath(_venv_py) != os.path.realpath(sys.executable):
    os.execv(_venv_py, [_venv_py] + sys.argv)

# Put this checkout's venv bin first on PATH so the component subprocesses the
# standalone command spawns (it runs `airflow ...` by name) resolve to the venv
# instead of failing with "No such file or directory: 'airflow'".
if os.path.isdir(os.path.dirname(_venv_py)):
    os.environ["PATH"] = os.path.dirname(_venv_py) + os.pathsep + os.environ.get("PATH", "")

import json
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.request

# Airflow is imported further down, after _setup_secrets() runs -- see the note there.


def _port_in_use(port: int) -> bool:
    """True if something is already listening on 127.0.0.1:<port>."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_pgadmin(port: int) -> bool:
    """True if a pgAdmin server actually answers on 127.0.0.1:<port>."""
    if not _port_in_use(port):
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/browser/", timeout=3) as r:
            return "pgadmin" in r.read(8192).decode("utf-8", "ignore").lower()
    except Exception:
        return False


def _find_running_pgadmin(base_port: int, span: int = 12):
    """Find an already-running pgAdmin at/after base_port (it may have
    auto-incremented off a busy port). Returns the live port, or None."""
    for p in range(base_port, base_port + span):
        if _is_pgadmin(p):
            return p
    return None


def _read_pgadmin_port_from_log(log_path: str, timeout: int = 45):
    """Poll pgAdmin's startup output for the port it actually bound
    ('navigate to http://host:PORT'). Returns the real port, or None."""
    pat = re.compile(r"navigate to https?://[^:/\s]+:(\d+)", re.IGNORECASE)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(log_path, "r", errors="ignore") as f:
                hits = pat.findall(f.read())
            if hits:
                return int(hits[-1])
        except OSError:
            pass
        time.sleep(1)
    return None


# Single source of truth shared with the pgadmin_link plugin: the launcher
# writes the live pgAdmin URL here, the plugin reads it to build the nav link.
# Whatever port pgAdmin actually ends up on, the nav link follows -- no need to
# keep an env var in sync across the two.
PGADMIN_URL_FILE = os.path.expanduser("~/.pgadmin/airflow_pgadmin_url")


def _publish_pgadmin_url(port: int) -> None:
    """Record the live pgAdmin URL for the nav-link plugin to pick up."""
    try:
        os.makedirs(os.path.dirname(PGADMIN_URL_FILE), exist_ok=True)
        with open(PGADMIN_URL_FILE, "w") as f:
            f.write(f"http://localhost:{port}/browser/\n")
    except OSError:
        pass


def _start_pgadmin() -> None:
    """
    Launch pgAdmin alongside Airflow so the 'pgAdmin' nav-bar link works
    without a separate manual start.

    No-op when:
      * SLEDGE_NO_PGADMIN is set (opt out), or
      * something is already serving on the port (reuse it), or
      * pgadmin4 isn't installed.

    pgAdmin runs in its own session (survives a Ctrl-C meant for Airflow's
    subprocesses) and is terminated when this launcher exits normally. If the
    launcher is killed hard, pgAdmin is left running and simply reused next
    time (the port check makes startup idempotent).
    """
    if os.environ.get("SLEDGE_NO_PGADMIN"):
        return
    base_port = int(os.environ.get("PGADMIN_PORT", "5050"))
    pgadmin = shutil.which("pgadmin4") or os.path.expanduser("~/miniforge3/bin/pgadmin4")
    if not os.path.exists(pgadmin):
        print("[pgadmin] pgadmin4 not found (pip install pgadmin4); skipping auto-start.")
        return

    # 1. If a pgAdmin is already up, link the nav item to ITS actual port --
    #    even if a prior/manual start landed it on 5051 instead of 5050. This is
    #    what keeps the nav link and the real server in lock-step.
    running = _find_running_pgadmin(base_port)
    if running is not None:
        print(f"[pgadmin] found running pgAdmin on :{running}; linking the nav item to it.")
        _publish_pgadmin_url(running)
        return

    # 2. None running -> start one. Pin the base port only if it's free; if it's
    #    occupied, let pgAdmin auto-increment to the next free port. Either way
    #    we then read the ACTUAL port it reports and publish THAT, so the link
    #    can't drift from where pgAdmin really came up.
    log_dir = os.path.expanduser("~/.pgadmin")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "pgadmin4.startup.log")
    open(log_path, "w").close()  # clear so we read THIS run's 'navigate to' line
    logf = open(log_path, "ab")
    pg_env = dict(os.environ)
    if not _port_in_use(base_port):
        pg_env["PGADMIN_INT_PORT"] = str(base_port)
    proc = subprocess.Popen(
        [pgadmin],
        stdout=logf,
        stderr=logf,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        env=pg_env,
    )
    actual = _read_pgadmin_port_from_log(log_path) or base_port
    print(f"[pgadmin] started (pid {proc.pid}) -> http://127.0.0.1:{actual}/browser/  "
          f"(log: {log_path})")
    _publish_pgadmin_url(actual)
    # Intentionally NOT tied to this launcher's lifetime. pgAdmin keeps running
    # across Airflow restarts -- the probe at the top reuses it next time -- so a
    # quick standalone restart never leaves you with a dead pgAdmin + a broken
    # link, which is exactly what kept happening. Stop it by hand if you must:
    #   pkill -f pgadmin4   (or kill the pid printed above)


def _secrets_path() -> str:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.expanduser(
        os.environ.get("SLEDGE_SECRETS_FILE",
                       os.path.join(repo, ".sledgehammer", "airflow-secrets.env.gpg")))


def _setup_secrets() -> None:
    """Decrypt the GPG secrets file and load its KEY=VALUE lines as env vars.

    airflow.cfg is committed with its secret fields blank; the real values live
    in <checkout>/.sledgehammer/airflow-secrets.env.gpg and get injected here,
    in memory, so Airflow reads them instead of the blank cfg. Each checkout
    keeps its own secrets; create them with scripts/sledge-secrets-create.sh.
    """
    enc = _secrets_path()

    # reuse-already-loaded secrets: if venv.sh (or a prior step) already decrypted
    # and exported them into this environment, don't decrypt again -- that second
    # decrypt is what makes you enter the passphrase twice in one launch.
    if os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"):
        return

    if not os.path.exists(enc):
        # Allow an already-populated environment (e.g. CI exported the vars).
        if os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"):
            print(f"[secrets] {enc} not found; using secrets already in the environment.")
            return
        sys.exit(
            f"[secrets] ERROR: no secrets file at {enc}, and no secrets in the "
            f"environment.\n"
            f"          airflow.cfg ships with blank secrets, so Airflow cannot "
            f"start without them.\n"
            f"          Create them:  ./scripts/sledge-secrets-create.sh\n"
            f"          (or point SLEDGE_SECRETS_FILE at an existing .gpg file).")

    if not shutil.which("gpg"):
        sys.exit("[secrets] ERROR: gpg not found on PATH; cannot decrypt secrets.")

    # pinentry needs to know the controlling terminal to prompt over SSH.
    try:
        if sys.stdin.isatty():
            os.environ.setdefault("GPG_TTY", os.ttyname(sys.stdin.fileno()))
    except Exception:
        pass

    print(f"[secrets] decrypting {enc} (enter your GPG passphrase) ...")
    attempts = 3
    res = None
    for attempt in range(1, attempts + 1):
        # --no-symkey-cache so a wrong passphrase isn't cached and silently reused
        # on the next try -- that would burn through the retries without re-asking.
        res = subprocess.run(
            ["gpg", "--quiet", "--no-symkey-cache", "--decrypt", enc],
            capture_output=True)
        if res.returncode == 0:
            break
        if attempt < attempts:
            print(f"[secrets] that passphrase didn't work "
                  f"(attempt {attempt}/{attempts}) -- try again, or Ctrl-C to quit.")
        else:
            sys.stderr.write(res.stderr.decode("utf-8", "ignore"))
            sys.exit(f"[secrets] ERROR: could not decrypt {enc} after {attempts} "
                     f"tries (wrong passphrase or corrupt file). Aborting startup.")

    loaded = 0
    for raw in res.stdout.decode("utf-8", "ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ[key] = val
        loaded += 1
    if not loaded:
        sys.exit(f"[secrets] ERROR: {enc} decrypted to no KEY=VALUE lines.")
    print(f"[secrets] loaded {loaded} secret(s) into the environment.")


def _load_postgres_target() -> None:
    """Take the cache database and the local Postgres directory from the postgres.env that `sledgehammer postgres` writes beside the secrets."""
    try:
        from hammer.vlsi import pd_store
        target = pd_store.postgres_target(pd_store.postgres_target_file(_secrets_path()))
    except Exception as e:
        print(f"[secrets] WARNING: could not read the Postgres settings beside the secrets: {e}")
        return
    if target.get("HAMMER_PG_DB"):
        os.environ.setdefault("HAMMER_PG_DB", target["HAMMER_PG_DB"])
    if target.get("SLEDGE_PG_DIR"):
        os.environ["SLEDGE_PG_DIR"] = target["SLEDGE_PG_DIR"]


def _local_postgres_port(root: str):
    """The port in root's postgresql.conf, read as sledge-local-postgres.sh reads it; None without that file."""
    try:
        with open(os.path.join(root, "data", "postgresql.conf")) as f:
            ports = re.findall(r"^[ \t]*port[ \t]*=[ \t]*([0-9]+)", f.read(), re.M)
    except (OSError, ValueError):
        return None
    return int(ports[-1]) if ports else 5432


def _start_local_postgres() -> None:
    """Start the Postgres that sledge-local-postgres.sh made when the Airflow conn is on this host and it is stopped."""
    try:
        from hammer.vlsi import pd_store
    except Exception as e:
        print(f"[postgres] WARNING: could not check for your local Postgres: {e}")
        return
    settings = pd_store._parse_conn_uri(os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", ""))
    if settings.get("host") not in ("localhost", "127.0.0.1", "::1"):
        return
    root = os.environ.get("SLEDGE_PG_DIR") or os.path.join(os.path.expanduser("~"), ".sledgehammer", "postgres")
    if not os.path.isfile(os.path.join(root, "data", "PG_VERSION")):
        return
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sledge-local-postgres.sh")
    if subprocess.run(["bash", script, "status"], capture_output=True, text=True).returncode == 0:
        return
    res = subprocess.run(["bash", script, "start"], capture_output=True, text=True)
    if res.returncode == 0:
        print(f"[postgres] started your local Postgres in {root}")
        return
    lines = (res.stderr or res.stdout).strip().splitlines()
    reason = f"could not start your local Postgres in {root}: {lines[-1] if lines else 'no output'}"
    if settings.get("port", 5432) == _local_postgres_port(root):
        sys.exit(f"[postgres] ERROR: {reason}")
    print(f"[postgres] WARNING: {reason}")


def _mirror_metadata_conn_for_callbacks() -> None:
    """Stage the metadata-DB connection in a file DAG callbacks can read.

    Airflow 3 hands callbacks a decoy sqlite conn, not the real one, so the
    completion notifier can't reach the dag_run row the usual way. We mirror the
    real conn into a chmod-600 file beside the SMTP password file, which survives
    into the callback through the SLEDGE_ env (see
    pd_store.airflow_metadata_conn_settings). Rewritten on every startup.
    """
    conn = os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "")
    if not conn.startswith("postgres"):
        return
    target = os.environ.get("SLEDGE_METADATA_CONN_FILE")
    if not target:
        smtp_pw = os.environ.get("SLEDGE_SMTP_PASSWORD_FILE")
        if not smtp_pw:
            return
        target = os.path.join(os.path.dirname(smtp_pw), ".sledge_metadata_conn")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(conn + "\n")
        os.chmod(target, 0o600)
        print(f"[secrets] mirrored metadata conn for callbacks -> {target}")
    except Exception as e:
        print(f"[secrets] WARNING: could not stage metadata conn for callbacks: {e}")


def _load_smtp_settings() -> None:
    """Load SMTP settings from .sledgehammer/smtp.env if present.

    The sender address and password-file path aren't secrets (the password stays
    in its own chmod-600 file), so they sit in a plain file beside the encrypted
    secrets rather than in git or a manual export -- which is what lets
    `sledgehammer` send mail with no per-launch setup. Existing env vars win.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo, ".sledgehammer", "smtp.env")
    if not os.path.exists(path):
        return
    loaded = 0
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                os.environ.setdefault(key, val)
                loaded += 1
        print(f"[secrets] loaded {loaded} SMTP setting(s) from {path}")
    except Exception as e:
        print(f"[secrets] WARNING: could not load {path}: {e}")


def _load_launch_settings() -> None:
    """Take this checkout's launch defaults from .sledgehammer/launch.env (SLEDGE_LITE=1 on a new install); env vars win."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".sledgehammer", "launch.env")
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())


def _pin_airflow_home() -> None:
    """Point AIRFLOW_HOME at this checkout before Airflow is imported.

    Airflow finds airflow.cfg via AIRFLOW_HOME. Launch from a shell that never
    ran ``export AIRFLOW_HOME=$(pwd)`` and it falls back to ~/airflow -- the
    stock defaults on port 8080, without this checkout's settings. This
    launcher only ever runs this checkout, so pin it and remove that footgun.
    """
    os.environ["AIRFLOW_HOME"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_pd_cache() -> None:
    """Turn the PD build cache on by default for this deployment.

    hammer's cache_or_run is a no-op unless HAMMER_PD_CACHE=1 -- without it the
    flows never store or restore stage tarballs, so there are no cache hits and
    no time-saved summary. Default it on so every task run caches; launch with
    HAMMER_PD_CACHE=0 to opt out.
    """
    os.environ.setdefault("HAMMER_PD_CACHE", "1")


def _default_cache_db() -> None:
    """Point the PD cache at Airflow's Postgres server unless HAMMER_PG_* already says otherwise."""
    try:
        from hammer.vlsi import pd_store
        pd_store.export_conn_defaults()
    except Exception as e:
        print(f"[secrets] WARNING: could not take the cache DB host from the Airflow conn: {e}")


UI_PASSWORD_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                ".sledgehammer", "airflow-ui-password.json")


def _ui_owner() -> str:
    """The UI's one login: the launching user, named the way generated DAGs name their owner."""
    import getpass
    return os.environ.get("USER") or getpass.getuser()


def _simple_login() -> None:
    """Make the launching user the UI's only login, with a password in a file only they can read."""
    owner = _ui_owner()
    os.environ["AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS"] = f"{owner}:admin"
    path = os.environ.setdefault("AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE", UI_PASSWORD_FILE)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    if path == UI_PASSWORD_FILE:
        os.chmod(folder, 0o700)
    try:
        with open(path) as f:
            passwords = json.load(f)
    except (OSError, ValueError):
        passwords = {}
    if not isinstance(passwords, dict):
        passwords = {}
    if not passwords.get(owner):
        passwords[owner] = secrets.token_urlsafe(18)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(passwords, f)
    os.chmod(path, 0o600)


def _login_line() -> str:
    return (f"Log in as {_ui_owner()}; your password is in "
            f"{os.environ['AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE']} (edit that file to change it).")


def _tunnel_line(forwards: str) -> str:
    """The ssh command that brings the UI to a laptop; SLEDGE_SSH_HOST names the host users ssh to."""
    target = os.environ.get("SLEDGE_SSH_HOST", "").strip() or "<the host you ssh to>"
    return f"Tunnel from your laptop: ssh {forwards} {target}"


def _lite() -> bool:
    """True for the single-user lite stack (`sledgehammer lite` or SLEDGE_LITE=1)."""
    return os.environ.get("SLEDGE_LITE", "").strip().lower() not in ("", "0", "false", "no", "off")


def _default_lite_limits() -> None:
    """Small-memory defaults for the lite stack; anything already in the environment wins."""
    for key, val in (("AIRFLOW__CORE__PARALLELISM", "2"),
                     ("AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_SIZE", "2"),
                     ("AIRFLOW__DATABASE__SQL_ALCHEMY_MAX_OVERFLOW", "5")):
        os.environ.setdefault(key, val)
    if "MALLOC_ARENA_MAX" not in os.environ:
        # The marker tells DAG tasks the cap is ours, so they drop it again for hammer and the EDA tools.
        os.environ["MALLOC_ARENA_MAX"] = "2"
        os.environ["SLEDGE_SET_MALLOC_ARENA_MAX"] = "1"


# Pin AIRFLOW_HOME first so the right airflow.cfg is read,
# then load secrets, THEN import Airflow: importing it reads sql_alchemy_conn
# right away, so the environment has to be populated first or it crashes on a
# blank conn.
_pin_airflow_home()
_simple_login()
_default_pd_cache()
_setup_secrets()
_load_postgres_target()
_start_local_postgres()
_load_smtp_settings()
_mirror_metadata_conn_for_callbacks()
_default_cache_db()
_load_launch_settings()
if _lite():
    os.environ["SLEDGE_LITE"] = "1"
    _default_lite_limits()

from airflow.cli.commands.standalone_command import StandaloneCommand
from airflow.executors.executor_loader import ExecutorLoader
from airflow.executors import executor_constants


class HammerStandalone(StandaloneCommand):
    """StandaloneCommand that says where the UI login is and how to reach the UI."""

    def find_user_info(self):
        self.print_output("standalone", _login_line())

    def print_ready(self):
        # Upstream just prints "Airflow is ready" with no URL; tack on the actual
        # web UI + pgAdmin addresses and a tunnel command so it's not a guessing game.
        super().print_ready()
        host = socket.getfqdn()
        # Same source Airflow's own --port default uses, so it tracks airflow.cfg + env.
        try:
            from airflow.configuration import conf
            api_port = str(conf.get("api", "port", fallback="8080"))
        except Exception:
            api_port = os.environ.get("AIRFLOW__API__PORT", "8080")
        pg_url, pg_port = "", ""
        try:
            with open(PGADMIN_URL_FILE) as f:
                pg_url = f.read().strip()
            m = re.search(r":(\d+)", pg_url)
            pg_port = m.group(1) if m else ""
        except OSError:
            pass
        self.print_output("standalone", f"Web UI  : http://localhost:{api_port}   (running on {host})")
        if pg_url:
            self.print_output("standalone", f"pgAdmin : {pg_url}")
        fwd = f"-L {api_port}:localhost:{api_port}" + (f" -L {pg_port}:localhost:{pg_port}" if pg_port else "")
        self.print_output("standalone", _tunnel_line(fwd))


def _effective_api_port() -> int:
    """The port this launch will bind: env override, else airflow.cfg [api] port."""
    for var in ("AIRFLOW__API__PORT", "SLEDGE_PORT"):
        val = os.environ.get(var, "")
        if val.isdigit():
            return int(val)
    cfg = os.path.join(os.environ.get("AIRFLOW_HOME", ""), "airflow.cfg")
    try:
        in_api = False
        with open(cfg) as f:
            for line in f:
                s = line.strip()
                if s.startswith("["):
                    in_api = (s == "[api]")
                elif in_api and s.startswith("port"):
                    parts = s.split("=", 1)
                    if len(parts) == 2 and parts[1].strip().isdigit():
                        return int(parts[1].strip())
    except OSError:
        pass
    return 8080


def _port_is_free(port: int) -> bool:
    import socket
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _configure_port() -> int:
    """Apply SLEDGE_PORT and keep base_url + the edge worker endpoint
    consistent with it. Returns the effective port.

    Unset defaults to 'auto': take the cfg port if free, else the next free
    one -- so a bare `sledgehammer` always comes up. Pin an explicit
    SLEDGE_PORT=<n> to demand that port (the launch then refuses if taken)."""
    req = os.environ.get("SLEDGE_PORT", "auto").strip() or "auto"
    port = None
    if req == "auto":
        base = _effective_api_port()
        for cand in range(base, base + 20):
            if _port_is_free(cand):
                port = cand
                break
        if port is None:
            sys.exit(f"[guard] ERROR: no free port found in {base}..{base + 19}.")
        if port != base:
            print(f"[port] {base} busy; auto-selected {port}.")
    elif req.isdigit():
        port = int(req)
    if port is not None:
        os.environ["AIRFLOW__API__PORT"] = str(port)
        os.environ["AIRFLOW__API__BASE_URL"] = f"http://localhost:{port}"
        os.environ.setdefault(
            "AIRFLOW__EDGE__API_URL",
            f"http://localhost:{port}/edge_worker/v1/rpcapi")
        print(f"[port] serving on {port}.")
        return port
    return _effective_api_port()


def _refuse_if_port_taken(port: int) -> None:
    """Two api-servers cannot share one port -- that conflict is a hard error.
    (The shared-DB scheduler overlap is only warned about; see below.)"""
    if not _port_is_free(port):
        sys.exit(
            f"[guard] ERROR: port {port} is already in use on this host -- another\n"
            f"        sledgehammer/api-server is listening there. You pinned this port\n"
            f"        with SLEDGE_PORT={port}; stop the other instance, pick a different\n"
            f"        port, or unset SLEDGE_PORT to let the launcher auto-select.")


def _connect_airflow_db():
    """Connect to Airflow's database with the conn split into fields, so a raw / ? or # in its password still connects."""
    import psycopg2
    from hammer.vlsi import pd_store
    settings = pd_store._parse_conn_uri(os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", ""))
    if not settings:
        raise ValueError("the Airflow conn is not a Postgres URL")
    return psycopg2.connect(**settings, connect_timeout=pd_store._connect_timeout())


def _refuse_if_scheduler_running() -> None:
    """Warn if another scheduler is already live on this metadata DB.

    Two deployments on one DB (a second checkout, a half-finished takeover) means
    duelling schedulers. We check the job table for a recent SchedulerJob
    heartbeat -- cross-host, since every scheduler heartbeats to the shared DB --
    and print a loud notice. The full launch warns and starts anyway; the lite
    launch exits instead.
    """
    conn_uri = os.environ.get("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "")
    if os.environ.get("SLEDGE_ALLOW_MULTI_SCHEDULER") or not conn_uri.startswith("postgresql"):
        return  # opt-out, or sqlite/local where there's nothing to race
    try:
        conn = _connect_airflow_db()
    except Exception:
        return  # no driver or DB unreachable -- Airflow's own startup will report it
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT hostname, latest_heartbeat FROM job "
                "WHERE job_type = 'SchedulerJob' AND state = 'running' "
                "AND latest_heartbeat > now() - interval '60 seconds' "
                "ORDER BY latest_heartbeat DESC")
            rows = cur.fetchall()
    except Exception:
        return  # no job table yet (fresh DB) or a schema we don't recognise
    finally:
        conn.close()
    if rows:
        where = "; ".join(f"{host} (heartbeat {ts:%Y-%m-%d %H:%M:%S})" for host, ts in rows)
        if _lite():
            here = socket.getfqdn().lower()
            if all(str(host).lower() == here for host, _ in rows) and _wait_for_stale(max(ts for _, ts in rows)):
                return _refuse_if_scheduler_running()
            sys.exit(
                f"[guard] ERROR: another scheduler is already heartbeating on this database:\n"
                f"          {where}\n"
                f"        Lite mode runs the only scheduler. Stop the other one first, or set\n"
                f"        SLEDGE_ALLOW_MULTI_SCHEDULER=1 if two are intentional.")
        print(
            f"[guard] WARNING: another scheduler is already heartbeating on this database:\n"
            f"          {where}\n"
            f"        Starting anyway. Two schedulers with different dags folders WILL\n"
            f"        fight over the DAG table and misroute tasks; make sure the other\n"
            f"        one is yours and shutting down, or that this is intentional HA.",
            file=sys.stderr)


_stale_wait: list = []


def _wait_for_stale(newest) -> bool:
    """Pause 2 s and say True while this host's scheduler may be one that just stopped: under a minute, heartbeat not advancing."""
    if not _stale_wait:
        print("[guard] a scheduler on this host heartbeated in the last minute; if it stopped or crashed, "
              "its heartbeat goes stale within a minute. Waiting for that ...")
        _stale_wait[:] = [time.time() + 65, newest]
    if newest > _stale_wait[1] or time.time() >= _stale_wait[0]:
        return False
    time.sleep(2)
    return True


def _prepare_cache_schema() -> None:
    """Create or upgrade the PD cache tables now, so the first task or page does not wait on it."""
    try:
        from hammer.vlsi import pd_store
        conn = pd_store._connect()
        try:
            pd_store._ensure_schema(conn, quiet=True)
        finally:
            conn.close()
    except Exception as e:
        print(f"[cache] WARNING: could not prepare the cache database ({e}).")


LITE_SUPERVISOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sledge-lite-supervisor.sh")


def _lite_port() -> int:
    """Pin SLEDGE_PORT or the configured port, refusing if it is busy rather than moving: tasks report to {base_url}/execution/."""
    req = os.environ.get("SLEDGE_PORT", "").strip()
    port = int(req) if req.isdigit() else _effective_api_port()
    if not _port_is_free(port):
        sys.exit(
            f"[guard] ERROR: port {port} is already in use on this host. Lite mode does not move\n"
            f"        to another port, because its tasks report to this one. Stop whatever is\n"
            f"        listening there, or choose a port with SLEDGE_PORT=<n>.")
    os.environ["AIRFLOW__API__PORT"] = str(port)
    os.environ["AIRFLOW__API__BASE_URL"] = f"http://localhost:{port}"
    os.environ.setdefault("AIRFLOW__EDGE__API_URL", f"http://localhost:{port}/edge_worker/v1/rpcapi")
    print(f"[port] serving on {port}.")
    return port


def _lite_main():
    """Set up as the full launch does, then hand off to the lite supervisor, so no launcher stays resident."""
    port = _lite_port()
    _refuse_if_scheduler_running()
    executor_class, _ = ExecutorLoader.import_default_executor_cls()
    if not executor_class.is_local:
        print("[lite] forcing the executor to LocalExecutor")
        os.environ["AIRFLOW__CORE__EXECUTOR"] = executor_constants.LOCAL_EXECUTOR
    from airflow.utils import db
    print("[lite] checking the database is initialized")
    db.initdb()
    _prepare_cache_schema()
    host = socket.getfqdn()
    print("[lite] starting the scheduler, dag-processor and api-server (no triggerer, log server or pgAdmin)")
    print("[lite] The Health panel on the UI's home page shows the Triggerer as down; that is expected, "
          "since lite runs none and SledgeHammer needs none.")
    print(f"[lite] Web UI  : http://localhost:{port}   (running on {host})")
    print(f"[lite] {_login_line()}")
    print(f"[lite] {_tunnel_line(f'-L {port}:localhost:{port}')}")
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp("bash", ["bash", LITE_SUPERVISOR])


def main():
    if _lite():
        return _lite_main()
    _refuse_if_port_taken(_configure_port())
    _refuse_if_scheduler_running()
    _prepare_cache_schema()
    _start_pgadmin()
    HammerStandalone().run()


if __name__ == "__main__":
    sys.exit(main())
