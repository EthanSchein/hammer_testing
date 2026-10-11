"""Stand-ins for the Postgres programs that scripts/sledge-local-postgres.sh runs, so its tests need no server."""
import os
import socket
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# The data directory is found the way the script finds it; the fakes keep their state inside it.
_ROOT = 'root="${SLEDGE_PG_DIR:-$HOME/.sledgehammer/postgres}"\n'

INITDB = """#!/bin/sh
echo "initdb $*" >> "@LOG@"
data=
pwfile=
while [ "$#" -gt 0 ]; do
    case "$1" in
        -D) data="$2"; shift ;;
        --pwfile=*) pwfile="${1#--pwfile=}" ;;
    esac
    shift
done
[ -s "$pwfile" ] || { echo "initdb: no password file" >&2; exit 1; }
mkdir -p "$data" && chmod 700 "$data"
echo 16 > "$data/PG_VERSION"
printf "#listen_addresses = 'localhost'\\n#port = 5432\\n" > "$data/postgresql.conf"
"""

PG_CTL = """#!/bin/sh
echo "pg_ctl $*" >> "@LOG@"
data=
action=
while [ "$#" -gt 0 ]; do
    case "$1" in
        -D) data="$2"; shift ;;
        -l) touch "$2"; shift ;;
        -m|-t|-o) shift ;;
        -*) ;;
        *) action="$1" ;;
    esac
    shift
done
[ -f "$data/PG_VERSION" ] || exit 4
case "$action" in
    status) [ -f "$data/postmaster.pid" ] && [ "$(cat "$data/postmaster.pid")" != crashed ] || exit 3 ;;
    start) env > "$data/postmaster.env"; echo 4242 > "$data/postmaster.pid" ;;
    stop) rm -f "$data/postmaster.pid" ;;
esac
"""

PSQL = """#!/bin/sh
echo "psql $*" >> "@LOG@"
""" + _ROOT + """db=
port=
prev=
for a in "$@"; do
    [ "$prev" = -v ] && case "$a" in db=*) db="${a#db=}" ;; esac
    [ "$prev" = -p ] && port="$a"
    prev="$a"
done
conf_port="$(sed -n 's/^port = //p' "$root/data/postgresql.conf" | tail -n 1)"
[ -f "$root/data/postmaster.pid" ] && [ "$port" = "$conf_port" ] \\
    || { echo "psql: error: connection refused" >&2; exit 2; }
[ -n "$PGPASSWORD" ] && [ "$PGPASSWORD" = "$(head -n 1 "$root/password")" ] \\
    || { echo "psql: error: password authentication failed" >&2; exit 2; }
sql="$(cat)"
touch "$root/data/databases"
case "$sql" in
    *"CREATE DATABASE"*) echo "$db" >> "$root/data/databases" ;;
    *pg_database*) grep -qx "$db" "$root/data/databases" && echo 1 ;;
esac
exit 0
"""

CONDA = """#!/bin/sh
echo "conda $*" >> "@LOG@"
prefix=
prev=
for a in "$@"; do
    [ "$prev" = -p ] && prefix="$a"
    prev="$a"
done
mkdir -p "$prefix/bin"
cp "@POSTGRES@/initdb" "@POSTGRES@/pg_ctl" "@POSTGRES@/psql" "$prefix/bin/"
"""

VENV_PYTHON = """#!/bin/sh
echo "python $*" >> "@LOG@"
""" + _ROOT + """case "$*" in
    *ensure_schema*)
        [ "$HAMMER_PG_PASSWORD" = "$(head -n 1 "$root/password")" ] || exit 1
        echo "schema $HAMMER_PG_USER@$HAMMER_PG_HOST:$HAMMER_PG_PORT/$HAMMER_PG_DB" >> "@LOG@" ;;
    *version_info*) echo 3.11 ;;
    *Fernet*|*token_bytes*) echo stub ;;
esac
exit 0
"""

# Writes its plaintext input to the -o file, or to stdout without one, so "encrypted" secrets read back as written.
GPG = """#!/bin/sh
out=/dev/stdout
while [ "$#" -gt 1 ]; do
    [ "$1" = -o ] && out="$2"
    shift
done
cat "$1" > "$out"
"""

LOW_DISK_DF = """#!/bin/sh
echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'
echo 'fake 2097152 1048576 1048576 50% /'
"""

NOT_SYSTEM_TOOLS = {"initdb", "pg_ctl", "psql", "postgres", "pg_isready", "createdb",
                    "conda", "mamba", "micromamba", "gpg"}


def executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def system_tools(directory: Path) -> str:
    """A PATH entry linking every system command except Postgres, conda and gpg, so a real one here never answers."""
    directory.mkdir(parents=True, exist_ok=True)
    for src_dir in ("/usr/bin", "/bin"):
        for name in sorted(os.listdir(src_dir)):
            src = os.path.join(src_dir, name)
            dst = directory / name
            if name in NOT_SYSTEM_TOOLS or os.path.lexists(dst) or not os.path.isfile(src) \
                    or not os.access(src, os.X_OK):
                continue
            os.symlink(src, dst)
    return str(directory)


def postgres(directory: Path, log: Path) -> str:
    for name, text in (("initdb", INITDB), ("pg_ctl", PG_CTL), ("psql", PSQL)):
        executable(directory / name, text.replace("@LOG@", str(log)))
    return str(directory)


def conda(directory: Path, log: Path, postgres_dir: str) -> str:
    executable(directory / "conda", CONDA.replace("@LOG@", str(log)).replace("@POSTGRES@", postgres_dir))
    return str(directory)


def checkout(directory: Path, *scripts: str, venv: bool = False, log: Path = None) -> Path:
    """A copy of the named scripts in a bare checkout, with a stand-in venv python when asked."""
    for name in scripts:
        with open(REPO / "scripts" / name) as src:
            executable(directory / "scripts" / name, src.read().replace("\r\n", "\n"))
    if venv:
        for name in ("python", "python3"):
            executable(directory / ".venv" / "bin" / name, VENV_PYTHON.replace("@LOG@", str(log)))
    return directory


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def calls(log: Path, program: str = "") -> list:
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if line.startswith(program)]
