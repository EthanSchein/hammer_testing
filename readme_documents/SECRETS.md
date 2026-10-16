# Secrets

The Airflow secrets are kept out of git. `airflow.cfg` is committed with its
secret fields blank, and the real values live in a GPG-encrypted file in your
checkout's gitignored `.sledgehammer/` directory that gets loaded into the environment at launch.

This only concerns you if you *run* an Airflow instance. Running `studio`
against the cache DB directly needs only your own Postgres password. Put it in
`HAMMER_PG_PASSWORD`, or in a line of `~/.pgpass` (or of the file `$PGPASSFILE`
names):

    <host>:<port>:sledgehammer_studio:<uid>:<password>

Then run `chmod 600 ~/.pgpass`: libpq ignores a pgpass file that others can
read.

The web UI password is not one of these secrets. The launch keeps it in
`.sledgehammer/airflow-ui-password.json`, readable only by you; see
[CLI_REFERENCE.md](CLI_REFERENCE.md#stack-and-passthrough).

## How it works

The five secret fields in `airflow.cfg` (`sql_alchemy_conn`, `fernet_key`,
`secret_key`, `jwt_secret`, and `internal_api_secret_key`) are blank in the
committed file. The real values are symmetric-encrypted under your passphrase in
`.sledgehammer/airflow-secrets.env.gpg`.

`sledgehammer` with no arguments sources `venv.sh` in your checkout, which
decrypts that file in memory and exports the values, where Airflow reads them in
place of the blank cfg. It then starts `scripts/airflow-standalone-ldap.py`,
which decrypts the file itself only when `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`
isn't set yet (set `SLEDGE_NO_AUTO_SECRETS=1` to leave the decrypt to it).
Either way you type the passphrase once per launch. The values stay in that
launch, not in your shell, and gpg-agent doesn't cache the passphrase (the
scripts decrypt with `--no-symkey-cache`), so the next launch asks again. To
skip that on restarts, run `source ./venv.sh` once in the shell you launch
from: the shell then holds the values, and `sledgehammer` from it doesn't ask.
`sledgehammer run`, `status`, `runs`, the stage names (`sledgehammer par`) and
Airflow commands (`sledgehammer db migrate`) decrypt the same file when that
variable isn't set.

## First-time setup

### Migrating an airflow.cfg that still has the real values

Let the script pull the secrets out, encrypt them, and blank the cfg:

    cd <your hammer checkout>
    source ./venv.sh                        # activates the venv, sets AIRFLOW_HOME
    ./scripts/sledge-secrets-init.py        # pick a passphrase; answer 'y' to blank the cfg
    sledgehammer                            # confirm it boots, then Ctrl-C
    git diff airflow.cfg                    # the secret lines should be blank

`airflow.cfg` is tracked by git and holds your secrets until the script blanks
it, so don't `git add` it until that diff shows blank values.

### A new operator on a fresh checkout

The committed `airflow.cfg` is already blank, so there's nothing to pull out.
Fill in your own values from the template and encrypt them. A separate instance
gets its own keys, so don't reuse anyone else's.

`./scripts/sledge-secrets-create.sh` does all of this for you, and
`scripts/uv_setup.sh` offers to run it. It prompts for your Postgres connection,
generates the keys, and encrypts the lot. To do it by hand instead:

    cp scripts/airflow-secrets.env.template /dev/shm/airflow-secrets.env
    chmod 600 /dev/shm/airflow-secrets.env
    # edit it: your sql_alchemy_conn and HAMMER_PG_PASSWORD, plus fresh keys:
    #   fernet:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    #   others:  python -c "import secrets; print(secrets.token_hex(32))"
    #   give secret_key and internal_api_secret_key the same value, as sledge-secrets-create.sh does
    #   percent-encode special characters in the conn's password, not in HAMMER_PG_PASSWORD
    mkdir -p .sledgehammer && chmod 700 .sledgehammer
    gpg --symmetric --cipher-algo AES256 \
        -o .sledgehammer/airflow-secrets.env.gpg /dev/shm/airflow-secrets.env
    chmod 600 .sledgehammer/airflow-secrets.env.gpg
    shred -u /dev/shm/airflow-secrets.env

## Day to day

    sledgehammer

Run it from any directory: it sources `venv.sh` in the checkout it was installed
from and starts the launcher, so there's nothing to activate first. Run it under
tmux so an SSH drop doesn't orphan the workers.

## Rotating a secret

Decrypt into RAM, edit, re-encrypt:

    T=$(mktemp -p /dev/shm)
    gpg --decrypt .sledgehammer/airflow-secrets.env.gpg > "$T"
    $EDITOR "$T"
    gpg --symmetric --cipher-algo AES256 --yes \
        -o .sledgehammer/airflow-secrets.env.gpg.new "$T"
    mv .sledgehammer/airflow-secrets.env.gpg{.new,}
    shred -u "$T"

For the DB password, change it in both the conn and `HAMMER_PG_PASSWORD`, and
also `ALTER ROLE <you> WITH PASSWORD ...` in Postgres.
Changing `fernet_key` invalidates the connection passwords stored in the Airflow
database. Run `sledgehammer` from a new shell afterwards: one that already
sourced `venv.sh` keeps the old values, and nothing decrypts again while they're
set.

## If you forget the passphrase

There's no backup by design. Recovery means regenerating the secrets. Most are
disposable; only `fernet_key` loses data (the connections stored in the Airflow
DB). The steps and the options for a real recovery path are in
[GPG_RECOVERY_IDEAS.md](GPG_RECOVERY_IDEAS.md).

## What's committed

In git: `airflow.cfg` with blank secrets, the scripts, and these docs. Never in
git (and gitignored): `.sledgehammer/`, any `*.env.gpg`, a decrypted
`airflow-secrets.env`, and your passphrase. git doesn't ignore a decrypted copy
under any other name, so keep those in `/dev/shm` as above.
