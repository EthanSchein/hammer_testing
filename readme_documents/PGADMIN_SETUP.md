# Setting up pgAdmin to browse the cache DB

pgAdmin is a web-based Postgres GUI. You run it once on any host that can
reach your Postgres server (no Docker needed), tunnel the port to your
laptop, and browse the cache tables in a browser.

Step 3 below (`config_local.py`) is only needed on shared hosts where
pgAdmin's default storage path isn't writable (`bwrcix-3` is one). If
you're on your own machine with normal write access, skip it.

## What you need

- A Python env where you can `pip install`. Conda, a venv, system Python,
  whatever, as long as you can write to `<env>/lib/.../site-packages/`.
- A host that can reach your Postgres server. For BWRC users, this is
  `bwrcix-3` reaching `barney`.
- Your Postgres role + password. The cache uses Postgres role auth, so the
  password is the one provisioned for your role, not your
  EECS password. If you've run Airflow against the cache, this password is
  the `HAMMER_PG_PASSWORD` in your encrypted secrets file
  (`.sledgehammer/airflow-secrets.env.gpg`, see [SECRETS.md](SECRETS.md)).

## Setup on the host (one-time)

### 1. Install pgadmin4

```bash
pip install pgadmin4
```

This pulls in a lot of dependencies (Flask, SQLAlchemy, paramiko, etc.) and
takes a minute or two.

### 2. Find where pip installed it

```bash
pip show pgadmin4 | grep Location
```

You'll get something like:
```
Location: /bwrcq/home/<you>/miniforge3/lib/python3.13/site-packages
```

The pgadmin4 package itself sits at `<Location>/pgadmin4/`. Keep that path
handy for the next step.

### 3. Redirect pgAdmin's storage path (only needed if the default isn't writable)

Out of the box, `pgadmin4` writes its SQLite DB and logs to
`/var/lib/pgadmin/`. On shared servers like `bwrcix-3` that path isn't
writable for normal users, so pgAdmin dies on startup with a permission
error.

The fix is a small `config_local.py` next to the installed package that
redirects every state path to your home directory. Create it at
`<Location>/pgadmin4/config_local.py`:

```python
import os
HOME = os.path.expanduser("~/.pgadmin")
DATA_DIR = HOME
SQLITE_PATH = os.path.join(HOME, "pgadmin4.db")
LOG_FILE = os.path.join(HOME, "pgadmin4.log")
SESSION_DB_PATH = os.path.join(HOME, "sessions")
STORAGE_DIR = os.path.join(HOME, "storage")
AZURE_CREDENTIAL_CACHE_DIR = os.path.join(HOME, "azurecredentialcache")
KERBEROS_CCACHE_DIR = os.path.join(HOME, "krbccache")
SERVER_MODE = False
```

`SERVER_MODE = False` runs pgAdmin in single-user mode: no per-user login
page, just one admin account. That's what you want for a personal browser
into your own DB.

### 4. First launch

In a tmux session (so the server survives logout):

```bash
tmux new -s pgadmin
pgadmin4
```

First time around, it asks for an admin email and password. This is
pgAdmin's own login, not anything to do with Postgres. Pick whatever:

- Email: `team@sledgehammer.local`
- Password: `sledgehammer`

You'll then see something like:

```
Starting pgAdmin 4. Please navigate to http://127.0.0.1:5050 in your browser.
```

`Ctrl-b d` detaches the tmux session and pgAdmin keeps running in the
background. Re-attach later with `tmux attach -t pgadmin`. To kill it for
real, attach back in and hit `Ctrl-c`.

## Connecting from your laptop

pgAdmin is now listening on port 5050 of the host. From your laptop, open
an SSH tunnel so `localhost:5050` in your browser hits the right place.

### Generic (host directly reachable)

```bash
ssh -L 5050:localhost:5050 <your_uid>@<the-host>
```

Leave that terminal open. In your browser: `http://localhost:5050/`.

### BWRC (going through the gateway)

`bwrcix-3` isn't reachable from outside the BWRC network. Use the
`bwrcrdsl-1` jump host:

```bash
ssh -A \
    -L 5050:localhost:5050 \
    -J <your_uid>@bwrcrdsl-1.eecs.berkeley.edu \
    <your_uid>@bwrcix-3.eecs.berkeley.edu
```

Same browser URL: `http://localhost:5050/`.

If you also want Airflow's 8081 tunneled in the same session, stack
another `-L`:

```bash
ssh -A \
    -L 8081:localhost:8081 \
    -L 5050:localhost:5050 \
    -J <your_uid>@bwrcrdsl-1.eecs.berkeley.edu \
    <your_uid>@bwrcix-3.eecs.berkeley.edu
```

One terminal, both ports forwarded. `localhost:8081` is Airflow,
`localhost:5050` is pgAdmin.

## Adding the Postgres server in pgAdmin

In the browser, log in with the admin email/password you set in step 4.

In the **Object Explorer** tree on the left, right-click `Servers` →
`Register` → `Server...`.

### General tab

- **Name**: anything human-readable, e.g. `SledgeHammer cache`

### Connection tab

For BWRC team deployment (cache on `barney`):

| Field | Value |
|---|---|
| Host name/address | `barney.eecs.berkeley.edu` |
| Port | `5433` |
| Maintenance database | `sledgehammer_studio` |
| Username | your Postgres role (usually your EECS UID) |
| Password | the password for that Postgres role (your `HAMMER_PG_PASSWORD`) |
| Save password? | ✓ (so you don't retype every time) |

For your own deployment: the host, port and database `studio init` ran
against (see [DATABASE_SETUP.md](DATABASE_SETUP.md)). The Postgres role is
whatever you used to create the DB; the password is the one you set on that
role.

Click **Save**. The server shows up in the tree on the left, and it sticks
around across pgAdmin restarts (the registration lives in
`~/.pgadmin/pgadmin4.db`).

## Browse the cache

Expand the tree: `Servers` → `SledgeHammer cache` → `Databases` →
`sledgehammer_studio` → `Schemas` → `hammer_poc` → `Tables`.

Nine tables. The cache itself is `pd_blobs` (stage tarballs, with oversized
ones split into `pd_blob_chunks`), `master_databases`, and `pd_artifacts`
(par-input JSON plus the `master_databases` write history). The rest are
`pd_checkpoints` and `pd_checkpoint_chunks` (sub-step resume),
`pd_cache_events` (the time-saved ledger), `user_workspaces` and
`user_notify_email`.

Right-click any table → **View/Edit Data** → **All Rows** → grid pops up.

## Starter queries

Top menu: **Tools → Query Tool**. Useful starters:

```sql
-- What's in the cache right now?
SELECT stage, design, owner, triggering_user, dag_id,
       pg_size_pretty(size_bytes::bigint) AS size,
       created_at
FROM hammer_poc.pd_blobs
ORDER BY created_at DESC;

-- Who has registered workspaces?
SELECT username, workspace_name, workspace_root, updated_at
FROM hammer_poc.user_workspaces;

-- Cache size by design
SELECT design, count(*) AS rows,
       pg_size_pretty(sum(size_bytes)::bigint) AS total_size
FROM hammer_poc.pd_blobs
GROUP BY design
ORDER BY sum(size_bytes) DESC;

-- All par-input artifacts with their origin DAG
SELECT sha256, top_module, design, dag_id, triggering_user, created_at
FROM hammer_poc.pd_artifacts
WHERE kind = 'par-input'
ORDER BY created_at DESC;
```

## Common gotchas

**`pgadmin4` dies on startup with `PermissionError: /var/lib/pgadmin/...`**
Either step 3 was skipped, or the `config_local.py` ended up in the wrong
place. It needs to be at `<pip Location>/pgadmin4/config_local.py` (use
`pip show pgadmin4` to confirm the Location).

**`localhost:5050` times out in your browser.**
SSH tunnel is dead, or pgAdmin stopped running on the host. Re-open the
tunnel, then `tmux attach -t pgadmin` on the host to check it's still up.

**pgAdmin login screen rejects your email/password.**
That screen wants pgAdmin's own admin login (the one you set on first
launch), not anything Postgres. The Postgres credentials come later, when
you register the server.

**`FATAL: password authentication failed for user X`** when registering.
Wrong Postgres role password. Same one as your `HAMMER_PG_PASSWORD`.

**`permission denied for schema hammer_poc`** when you open a table.
You don't have access to the cache schema yet. Someone with admin on the
`sledgehammer_users` role (or, on a cluster without that role, the schema
owner) needs to run `studio grant <your_role>` on the host.

## Stopping pgAdmin

Attach back into the tmux session and stop it:

```bash
tmux attach -t pgadmin
# inside the session:
Ctrl-c    # stops pgadmin4
exit      # closes the shell and the tmux session
```

Or kill the session from outside in one shot:

```bash
tmux kill-session -t pgadmin
```

If the `sledgehammer` launcher started pgAdmin for you, there's no tmux
session to attach to, and it keeps running after Airflow stops. Stop it
with:

```bash
pkill -f pgadmin4
```
