#!/usr/bin/env bash
# Your own Postgres for SledgeHammer, run as you with no root: the default setup creates it if missing and starts it.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_ROOT="${SLEDGE_PG_DIR:-$HOME/.sledgehammer/postgres}"
case "$PG_ROOT" in /*) ;; *) PG_ROOT="$PWD/$PG_ROOT" ;; esac
PG_DATA="$PG_ROOT/data"
PW_FILE="$PG_ROOT/password"
LOG_FILE="$PG_ROOT/postgres.log"
PG_ENV="$HOME/.sledgehammer/pg-env"
PG_USER="${USER:-$(id -un)}"
AIRFLOW_DB="airflow_$PG_USER"
CACHE_DB="sledgehammer_studio"
PY="$REPO/.venv/bin/python"

usage() {
    cat <<EOF
usage: $(basename "$0") [setup|start|stop|status|env]
  setup   create your local Postgres if it is missing, start it, and create the
          $AIRFLOW_DB and $CACHE_DB databases (the default; safe to rerun)
  start   start it
  stop    stop it
  status  say whether it is running (exit 0 running here, 2 started on another
          host, 3 stopped, 4 not set up)
  env     print its settings as KEY=value lines, with the password file's path
          and never the password
It lives in \$SLEDGE_PG_DIR, now $PG_ROOT. A new one listens on
\$SLEDGE_PG_PORT (default 5544), or on the next free port when that one is taken.
EOF
}

die() {
    echo "sledge-local-postgres: $*" >&2
    exit 1
}

_use_dir() {
    [ -x "$1/initdb" ] && [ -x "$1/pg_ctl" ] && [ -x "$1/psql" ] || return 1
    INITDB="$1/initdb"
    PG_CTL="$1/pg_ctl"
    PSQL="$1/psql"
}

_find_postgres() {
    local recorded=""
    [ -f "$PG_ROOT/bindir" ] && recorded="$(cat "$PG_ROOT/bindir")"
    if [ -n "$recorded" ] && _use_dir "$recorded"; then
        return 0
    fi
    if INITDB="$(command -v initdb)" && PG_CTL="$(command -v pg_ctl)" && PSQL="$(command -v psql)"; then
        return 0
    fi
    _use_dir "$PG_ENV/bin"
}

_install_postgres() {
    local tool="" t
    for t in conda mamba micromamba; do
        if command -v "$t" >/dev/null 2>&1; then
            tool="$t"
            break
        fi
    done
    if [ -z "$tool" ]; then
        cat >&2 <<EOF
sledge-local-postgres: no Postgres found. initdb, pg_ctl and psql are not on PATH or in
$PG_ENV/bin, and there is no conda, mamba or micromamba on PATH to install them with.
Either install micromamba or conda (no root needed) and rerun, which puts Postgres in
$PG_ENV, or install your distro's Postgres server and put the directory holding
initdb and pg_ctl on PATH (Debian and Ubuntu: /usr/lib/postgresql/<version>/bin,
PGDG packages: /usr/pgsql-<version>/bin), then rerun.
EOF
        exit 1
    fi
    echo "installing Postgres from conda-forge into $PG_ENV with $tool (no root; your other environments are not touched)"
    "$tool" create -y -p "$PG_ENV" --override-channels -c conda-forge postgresql \
        || die "$tool could not install Postgres into $PG_ENV"
    _use_dir "$PG_ENV/bin" || die "$PG_ENV/bin has no initdb, pg_ctl and psql after the install"
}

_port() {
    local p
    p="$(sed -n 's/^[[:space:]]*port[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$PG_DATA/postgresql.conf" | tail -n 1)"
    echo "${p:-5432}"
}

_port_busy() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null || (exec 3<>"/dev/tcp/::1/$1") 2>/dev/null
}

_drop_secrets() {
    local v
    for v in $(compgen -e); do
        case "$v" in AIRFLOW_*|*PASSWORD*|*SECRET*|*TOKEN*) unset "$v" ;; esac
    done
}

_running() {
    "$PG_CTL" -D "$PG_DATA" status >/dev/null 2>&1
}

_elsewhere() {
    local host
    [ -f "$PG_DATA/postmaster.pid" ] && [ -f "$PG_ROOT/host" ] || return 1
    host="$(cat "$PG_ROOT/host")"
    [ "$host" != "$(uname -n)" ] || return 1
    echo "$host"
}

_report_space() {
    local kb
    kb="$(df -Pk "$PG_ROOT" | awk 'NR == 2 {print $4}')" || return 0
    case "$kb" in ''|*[!0-9]*) return 0 ;; esac
    echo "free space where $PG_ROOT lives: $(awk -v kb="$kb" 'BEGIN {printf "%.1f", kb / 1048576}') GB"
    if [ "$kb" -lt $((5 * 1048576)) ]; then
        echo "WARNING: that is under 5 GB, and cache blobs run to about 1 GB each. For a bigger disk,"
        echo "         stop this Postgres, remove $PG_ROOT and rerun with SLEDGE_PG_DIR set."
    fi
}

_init() {
    local port first sockdir
    _find_postgres || _install_postgres
    mkdir -p "$PG_ROOT"
    chmod 700 "$PG_ROOT"
    _report_space
    port="${SLEDGE_PG_PORT:-5544}"
    case "$port" in ''|*[!0-9]*) die "SLEDGE_PG_PORT must be a port number, not '$port'" ;; esac
    first="$port"
    while _port_busy "$port"; do
        port=$((port + 1))
        [ "$port" -le $((first + 100)) ] || die "ports $first to $((first + 100)) are all in use; set SLEDGE_PG_PORT to a free one"
    done
    [ "$port" = "$first" ] || echo "port $first is in use by another program; using $port"
    (umask 077 && od -An -tx1 -N24 /dev/urandom | tr -d ' \n' > "$PW_FILE")
    chmod 600 "$PW_FILE"
    echo "creating a Postgres cluster in $PG_DATA (user $PG_USER, port $port)"
    "$INITDB" -D "$PG_DATA" -U "$PG_USER" -E UTF8 --locale=C \
        --auth-local=scram-sha-256 --auth-host=scram-sha-256 --pwfile="$PW_FILE" \
        > "$PG_ROOT/initdb.log" 2>&1 \
        || { tail -n 20 "$PG_ROOT/initdb.log" >&2; die "initdb failed; log: $PG_ROOT/initdb.log"; }
    sockdir="$PG_ROOT"
    if [ $((${#sockdir} + 15)) -gt 107 ]; then
        echo "note: $PG_ROOT is too long a path for a Unix socket, so this Postgres takes TCP connections only"
        sockdir=""
    elif [[ "$sockdir" == *[,\\]* ]]; then
        echo "note: $PG_ROOT has a comma or backslash, which Postgres cannot take in a socket path, so this Postgres takes TCP connections only"
        sockdir=""
    fi
    {
        echo ""
        echo "# added by sledge-local-postgres.sh"
        echo "listen_addresses = 'localhost'"
        echo "port = $port"
        echo "unix_socket_directories = '${sockdir//\'/\'\'}'"
        echo "max_connections = 120"
    } >> "$PG_DATA/postgresql.conf"
    dirname "$PG_CTL" > "$PG_ROOT/bindir"
}

_start() {
    local port other
    port="$(_port)"
    if other="$(_elsewhere)"; then
        die "this Postgres was started on $other and not stopped there. Two servers on one data directory corrupt it, so stop it on $other first ($0 stop). If $other is gone for good, remove $PG_ROOT/host and start again."
    fi
    if _running; then
        echo "already running on port $port ($PG_ROOT)"
        return 0
    fi
    if _port_busy "$port"; then
        die "port $port is in use by another program, so this Postgres cannot start on it. Stop that program, or change port in $PG_DATA/postgresql.conf and in your secrets."
    fi
    if ! (_drop_secrets; "$PG_CTL" -D "$PG_DATA" -l "$LOG_FILE" -w start >/dev/null); then
        tail -n 20 "$LOG_FILE" >&2 || true
        die "Postgres did not start; log: $LOG_FILE"
    fi
    uname -n > "$PG_ROOT/host"
    echo "started on port $port ($PG_ROOT)"
}

_stop() {
    local other
    if other="$(_elsewhere)"; then
        die "this Postgres was started on $other; stop it there"
    fi
    if ! _running; then
        rm -f "$PG_ROOT/host"
        echo "not running ($PG_ROOT)"
        return 0
    fi
    "$PG_CTL" -D "$PG_DATA" -m fast -w stop >/dev/null
    echo "stopped ($PG_ROOT)"
}

_status() {
    local other
    if other="$(_elsewhere)"; then
        echo "started on $other and not stopped there ($PG_ROOT)"
        return 2
    fi
    if _running; then
        echo "running on port $(_port) ($PG_ROOT)"
        return 0
    fi
    echo "stopped; port $(_port) ($PG_ROOT)"
    return 3
}

_sql() {
    PGPASSWORD="$(cat "$PW_FILE")" "$PSQL" -X -q -At -v ON_ERROR_STOP=1 \
        -h localhost -p "$(_port)" -U "$PG_USER" -d postgres "$@"
}

_ensure_db() {
    local found
    found="$(echo "SELECT 1 FROM pg_database WHERE datname = :'db';" | _sql -v db="$1")" \
        || die "could not reach this Postgres on port $(_port) as $PG_USER"
    [ -z "$found" ] || return 0
    echo 'CREATE DATABASE :"db";' | _sql -v db="$1"
    echo "created database $1"
}

_cache_tables() {
    if [ ! -x "$PY" ] || ! "$PY" -I -c 'import psycopg2, hammer.vlsi.pd_store' >/dev/null 2>&1; then
        echo "cache tables: not created yet ($REPO/.venv has no psycopg2); they are created on first use"
        return 0
    fi
    if ! HAMMER_PG_HOST=localhost HAMMER_PG_PORT="$(_port)" HAMMER_PG_DB="$CACHE_DB" HAMMER_PG_USER="$PG_USER" \
        HAMMER_PG_PASSWORD="$(cat "$PW_FILE")" \
        "$PY" -I -c 'from hammer.vlsi import pd_store; pd_store.ensure_schema()'; then
        echo "cache tables: could not create them now in $CACHE_DB; they are created on first use"
        return 0
    fi
    echo "cache tables ready in $CACHE_DB"
}

cmd="${1:-setup}"
case "$cmd" in
    setup|start|stop|status|env) ;;
    -h|--help|help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

if [ ! -f "$PG_DATA/PG_VERSION" ]; then
    if [ "$cmd" != setup ]; then
        echo "no local Postgres in $PG_ROOT; create it with: $0 setup" >&2
        exit 4
    fi
    _init
fi

if [ "$cmd" = env ]; then
    printf '%s\n' "SLEDGE_PG_DIR=$PG_ROOT" "SLEDGE_PG_HOST=localhost" "SLEDGE_PG_PORT=$(_port)" \
        "SLEDGE_PG_USER=$PG_USER" "SLEDGE_PG_DB=$AIRFLOW_DB" "SLEDGE_PG_CACHE_DB=$CACHE_DB" \
        "SLEDGE_PG_PASSWORD_FILE=$PW_FILE"
    exit 0
fi

_find_postgres || die "no initdb, pg_ctl and psql found for $PG_ROOT; put the directory holding them on PATH"

case "$cmd" in
    start) _start ;;
    stop) _stop ;;
    status) _status ;;
    setup)
        [ -f "$PW_FILE" ] || die "$PW_FILE is missing, so nothing can log in to this Postgres"
        chmod 700 "$PG_ROOT"
        chmod 600 "$PW_FILE"
        _start
        _ensure_db "$AIRFLOW_DB"
        _ensure_db "$CACHE_DB"
        _cache_tables
        echo "local Postgres ready: $PG_USER@localhost:$(_port), databases $AIRFLOW_DB and $CACHE_DB, password in $PW_FILE"
        ;;
esac
