#!/usr/bin/env bash
# Create the GPG-encrypted Airflow secrets file from scratch.
# Prompts for the Postgres connection, generates the Airflow keys, and encrypts
# the lot under a passphrase you choose. Run it standalone or via uv_setup.sh.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="$REPO/.sledgehammer"
SECRETS_FILE="${SLEDGE_SECRETS_FILE:-$SECRETS_DIR/airflow-secrets.env.gpg}"
PY="$REPO/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

if [ -f "$SECRETS_FILE" ]; then
    echo "already present: $SECRETS_FILE (delete it first to recreate)"
    exit 0
fi

read -rp "  Postgres user [$USER]: " PG_USER; PG_USER="${PG_USER:-$USER}"
read -rp "  Postgres db [airflow_$USER]: " PG_DB; PG_DB="${PG_DB:-airflow_$USER}"
read -rp "  Postgres host [localhost]: " PG_HOST; PG_HOST="${PG_HOST:-localhost}"
read -rp "  Postgres port [5544]: " PG_PORT; PG_PORT="${PG_PORT:-5544}"
while :; do
    read -rsp "  Postgres password: " PG_PASS; echo
    read -rsp "  confirm password: " PG_PASS2; echo
    [ -n "$PG_PASS" ] && [ "$PG_PASS" = "$PG_PASS2" ] && break
    echo "  empty or mismatched -- try again"
done
FERNET="$("$PY" -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
# secret_key must equal internal_api_secret_key or task auth fails
APIKEY="$("$PY" -c 'import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())')"
JWT="$("$PY" -c 'import secrets,base64;print(base64.b64encode(secrets.token_bytes(32)).decode())')"
mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"
tmp="$(mktemp -p /dev/shm 2>/dev/null || mktemp)"
{
  echo "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=postgresql+psycopg2://${PG_USER}:${PG_PASS}@${PG_HOST}:${PG_PORT}/${PG_DB}"
  echo "AIRFLOW__CORE__FERNET_KEY=${FERNET}"
  echo "AIRFLOW__CORE__INTERNAL_API_SECRET_KEY=${APIKEY}"
  echo "AIRFLOW__API__SECRET_KEY=${APIKEY}"
  echo "AIRFLOW__API_AUTH__JWT_SECRET=${JWT}"
  echo "HAMMER_PG_PASSWORD=${PG_PASS}"
} > "$tmp"
echo "  choose a passphrase to encrypt your secrets:"
gpg --symmetric --cipher-algo AES256 -o "$SECRETS_FILE" "$tmp"
chmod 600 "$SECRETS_FILE"
shred -u "$tmp" 2>/dev/null || rm -f "$tmp"
echo "  wrote $SECRETS_FILE"
