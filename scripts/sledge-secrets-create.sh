#!/usr/bin/env bash
# Create the GPG-encrypted Airflow secrets file from scratch.
# Prompts for the Postgres connection, generates the Airflow keys, and encrypts
# the lot under a passphrase you choose. Run it standalone or via uv_setup.sh.
# SLEDGE_PG_USER, SLEDGE_PG_DB, SLEDGE_PG_HOST, SLEDGE_PG_PORT and
# SLEDGE_PG_PASSWORD_FILE, when set, answer their prompts; SLEDGE_PG_DIR, when
# set, is saved so the launcher finds that local Postgres.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_FILE="${SLEDGE_SECRETS_FILE:-$REPO/.sledgehammer/airflow-secrets.env.gpg}"
case "$SECRETS_FILE" in "~/"*) SECRETS_FILE="$HOME/${SECRETS_FILE#"~/"}" ;; esac
SECRETS_DIR="$(dirname "$SECRETS_FILE")"
PY="$REPO/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

if [ -f "$SECRETS_FILE" ]; then
    echo "already present: $SECRETS_FILE (delete it first to recreate)"
    exit 0
fi
mkdir -p -m 700 "$SECRETS_DIR"
[ ! "$SECRETS_DIR" -ef "$REPO/.sledgehammer" ] || chmod 700 "$SECRETS_DIR"
[ -w "$SECRETS_DIR" ] && [ -x "$SECRETS_DIR" ] || { echo "cannot write to $SECRETS_DIR" >&2; exit 1; }

ask() {
    local given="SLEDGE_$1" reply
    if [ -n "${!given:-}" ]; then
        printf -v "$1" '%s' "${!given}"
        echo "  $2: ${!given} (from $given)"
    else
        read -rp "  $2 [$3]: " reply
        printf -v "$1" '%s' "${reply:-$3}"
    fi
}
ask PG_USER "Postgres user" "$USER"
ask PG_DB "Postgres db" "airflow_$USER"
ask PG_HOST "Postgres host" localhost
ask PG_PORT "Postgres port" 5544
if [ -n "${SLEDGE_PG_PASSWORD_FILE:-}" ]; then
    PG_PASS="$(head -n 1 "$SLEDGE_PG_PASSWORD_FILE")" || exit 1
    [ -n "$PG_PASS" ] || { echo "  $SLEDGE_PG_PASSWORD_FILE holds no password" >&2; exit 1; }
    echo "  Postgres password: from $SLEDGE_PG_PASSWORD_FILE"
fi
while [ -z "${SLEDGE_PG_PASSWORD_FILE:-}" ]; do
    read -rsp "  Postgres password: " PG_PASS; echo
    read -rsp "  confirm password: " PG_PASS2; echo
    [ -n "$PG_PASS" ] && [ "$PG_PASS" = "$PG_PASS2" ] && break
    echo "  empty or mismatched -- try again"
done
FERNET="$("$PY" -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
# secret_key must equal internal_api_secret_key or task auth fails
APIKEY="$("$PY" -c 'import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())')"
JWT="$("$PY" -c 'import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())')"
urlquote() {
    local LC_ALL=C s="$1" out="" c i
    for ((i = 0; i < ${#s}; i++)); do
        c="${s:i:1}"
        case "$c" in
            [A-Za-z0-9._~-]) out+="$c" ;;
            *) printf -v c '%%%02X' "'$c"; out+="$c" ;;
        esac
    done
    printf '%s' "$out"
}
# The secrets loaders strip whitespace and one pair of quotes around a value.
envvalue() {
    case "$1" in
        [\"\'[:space:]]*|*[\"\'[:space:]]) printf "'%s'" "$1" ;;
        *) printf '%s' "$1" ;;
    esac
}
case "$PG_HOST" in
    \[*) CONN_HOST="$PG_HOST" ;;
    *:*) CONN_HOST="[$PG_HOST]" ;;
    *) CONN_HOST="$PG_HOST" ;;
esac
tmp="$(mktemp -p /dev/shm 2>/dev/null || mktemp)"
trap 'shred -u "$tmp" 2>/dev/null || rm -f "$tmp"' EXIT
{
  echo "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=postgresql+psycopg2://$(urlquote "$PG_USER"):$(urlquote "$PG_PASS")@${CONN_HOST}:${PG_PORT}/${PG_DB}"
  echo "AIRFLOW__CORE__FERNET_KEY=${FERNET}"
  echo "AIRFLOW__CORE__INTERNAL_API_SECRET_KEY=${APIKEY}"
  echo "AIRFLOW__API__SECRET_KEY=${APIKEY}"
  echo "AIRFLOW__API_AUTH__JWT_SECRET=${JWT}"
  echo "HAMMER_PG_PASSWORD=$(envvalue "$PG_PASS")"
  [ -z "${SLEDGE_PG_DIR:-}" ] || echo "SLEDGE_PG_DIR=${SLEDGE_PG_DIR}"
} > "$tmp"
echo "  choose a passphrase to encrypt your secrets:"
gpg --symmetric --cipher-algo AES256 -o "$SECRETS_FILE" "$tmp"
chmod 600 "$SECRETS_FILE"
shred -u "$tmp" 2>/dev/null || rm -f "$tmp"
echo "  wrote $SECRETS_FILE"
