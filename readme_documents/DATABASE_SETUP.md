# Setting up SledgeHammer Studio on your own Postgres

Quick guide for getting the cache and permissions running against any Postgres cluster. About 15 minutes if Postgres is already installed.

## What you need

- A Postgres cluster you control (laptop, lab server, RDS, conda Postgres, whatever). You need to be able to `CREATE DATABASE` and `CREATE ROLE` on it. Don't have one? [LOCAL_POSTGRES_SETUP.md](LOCAL_POSTGRES_SETUP.md) walks through standing one up in your own directory, no root needed, and covers the Airflow side too.
- This repo, on the `master` branch.
- A working venv (we use `.venv/` at the repo root).

## Setup

### 1. Clone and activate the venv

```bash
git clone https://github.com/lawrencejrhee/hammer.git
cd hammer
git checkout master
source ./venv.sh
```

`venv.sh` activates `.venv` and sets `AIRFLOW_HOME` to the repo root. If `.venv/` doesn't exist yet, run `./scripts/uv_setup.sh` before sourcing. A plain `uv sync` or `pip install -e .` also works if you add the `cache` extra (`uv sync --extra cache`, `pip install -e '.[cache]'`): without `psycopg2`, `studio` can't reach Postgres and stops with `studio: database unavailable: psycopg2 is not installed ...`.

### 2. Point at your Postgres

The library reads each connection setting from `HAMMER_PG_*` env vars first, then from Airflow's `sql_alchemy_conn` (`$AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` when it holds a Postgres URI, else `airflow.cfg`), then falls back to `localhost:5433` and user `$USER`. The database name is the exception: it comes only from `HAMMER_PG_DB` and defaults to `sledgehammer_studio`. The password can also come from libpq's `PGPASSWORD` or `~/.pgpass` ([SECRETS.md](SECRETS.md)). If Airflow already talks to this Postgres, its conn is enough. For a fresh setup, or to point the cache at a different server or user than Airflow's, env vars are easiest:

```bash
export HAMMER_PG_HOST=your-postgres-host
export HAMMER_PG_PORT=5432
export HAMMER_PG_USER=your-username
export HAMMER_PG_PASSWORD="$(cat ~/.sledgehammer/pgpw)"
export HAMMER_PG_DB=sledgehammer_studio
```

Keep the password itself out of your shell history and out of any file you source: write it to a mode-600 file (`~/.sledgehammer/pgpw` above) and read it back, or leave `HAMMER_PG_PASSWORD` unset and put it in `~/.pgpass` ([SECRETS.md](SECRETS.md)).

You don't have to export these in every shell. `studio` and the `sledgehammer` commands (`run`, `buildfile`, `status`, `runs`, the stage names, the Airflow passthrough) read a stack env file themselves, so put the lines above in `~/.sledgehammer/env.sh` once. They also find `$SLEDGE_ENV_FILE`, or a `stack_env.sh` in or above the current directory ([CLI_REFERENCE.md](CLI_REFERENCE.md#the-environment-file)), and anything you export yourself wins over the file. The launch itself (`sledgehammer` with no subcommand, or `sledgehammer standalone`) doesn't read it, so for the cache inside DAG runs, export the values (or `source ~/.sledgehammer/env.sh`) in the shell you launch the stack from.

If you're on barney specifically, put the right `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` and `HAMMER_PG_PASSWORD` in the GPG secrets file instead ([SECRETS.md](SECRETS.md)); `source ./venv.sh` exports them. Don't put the conn in `airflow.cfg`: it's committed, with its secret fields blank.

### 3. Create the cache database

Connect as a superuser (or anyone with `CREATEDB`) and run:

```sql
CREATE DATABASE sledgehammer_studio OWNER your_username;
```

If you're using your own cluster you're already a superuser, so this works without help. On a shared cluster like barney, your account just needs `CREATEDB` (most do).

### 4. Create the group role

This is the cluster-level role that gates cache access. One-time setup at the cluster level by anyone with `CREATEROLE` or superuser:

```sql
CREATE ROLE sledgehammer_users NOLOGIN;
GRANT sledgehammer_users TO your_username WITH ADMIN OPTION;
```

The `WITH ADMIN OPTION` part is what lets you add and remove members later without going back to the cluster admin.

### 5. Initialize the schema

```bash
studio init
```

This creates the `hammer_poc` schema and its nine tables (the artifact cache, the stage-blob cache and its overflow chunks, master databases, checkpoints and their overflow chunks, the cache-event ledger, workspaces, and notification addresses), plus indexes, and applies default-deny on the schema, with table and sequence grants to `sledgehammer_users`. Idempotent, so re-running it later is fine. Re-run it after you update the checkout, so an existing schema picks up new tables, columns and grants: your teammates' runs can't apply them.

### 6. Verify

```bash
studio blob-list
```

Prints `(no blobs)` on a fresh install. If it can't use the database, `studio` prints one line on stderr instead, `studio: database unavailable: <reason>`, and exits 1. The reason says what's wrong ([Common gotchas](#common-gotchas)). A server that doesn't answer at all takes `HAMMER_PG_CONNECT_TIMEOUT` seconds (10 by default) to produce that line.

## Onboarding teammates

For each person who should have cache access:

```bash
studio grant their_postgres_username
```

That adds them to `sledgehammer_users`. They can now read and write the cache, the time-saved ledger and the sub-step checkpoints.

To remove someone:

```bash
studio revoke their_postgres_username
```

Anyone outside the group is refused by Postgres itself: the `hammer_poc` schema is default-denied, so a non-member's queries fail with `permission denied for schema hammer_poc` (the exact symptom listed in the gotchas below).

## Running with the cache

DAG runs under the `sledgehammer` launcher have the cache on by default (launch with `HAMMER_PD_CACHE=0 sledgehammer` to turn it off). With the stack up, register your design's DAG once from its vlsi directory with `sledgehammer buildfile`, then run a stage; there's no cache variable to set ([CLI_REFERENCE.md](CLI_REFERENCE.md#running-a-design)):

```bash
sledgehammer buildfile
sledgehammer syn
```

First run, you'll see in the logs:

```
PD cache MISS for synthesis (sha256=...). Running stage.
PD cache STORE synthesis (sha256=..., bytes=..., tool runtime=..., CPU=...).
```

A run with the same inputs in a fresh build directory (a teammate's, or yours after a clean):

```
PD cache HIT for synthesis (sha256=...). Restored .../syn-rundir, skipping run. Saved ~... wall (...).
```

The second run completes in seconds instead of however long Genus takes.

A direct `hammer-vlsi` run, outside the DAG, leaves the cache off unless you set `HAMMER_PD_CACHE=1`. Setting `vlsi.pd_cache.enabled: true` in the design's config turns it on for every run, even under `HAMMER_PD_CACHE=0`.

## Testing the permission model

There's a runnable demo at `scripts/demo_auth.sh`. It spins up a throwaway local Postgres, creates `sledgehammer_users` plus three test users (`lawrencejrhee`, `colin`, `juhyun`), and walks through the grant/revoke flow, checking with pass/fail output that a member can read `hammer_poc` and a non-member can't. It doesn't test writes. Useful sanity check any time you want to confirm the group still gates access:

```bash
./scripts/demo_auth.sh
```

Needs `initdb`, `pg_ctl`, `psql` on your `PATH`. Conda's `postgresql` package gives you all three (`conda install -n base postgresql`).

## Common gotchas

**`hammer-shell does not appear to be on the path` from a `hammer-vlsi` or `make` run.** Every `hammer-vlsi` run looks for `hammer-shell-test` on `PATH`, and your `.venv/bin` isn't there. `sledgehammer buildfile` puts the venv's bin directory first for the build, and the `sledgehammer` launch sources `venv.sh` for its DAG runs, so you hit this when you run `hammer-vlsi` or `make` yourself, or start Airflow some other way. Source `venv.sh` first, and if `which hammer-shell-test` still comes up empty, prepend `.venv/bin` to `PATH` by hand. The BWRC env script can shove conda's bin in front of yours.

**`studio` prints `studio: database unavailable: ...`.** Most `studio` commands report this way, one line on stderr and exit status 1, when they can't use Postgres. The rest of the line says why:

- `No Postgres password found ...`: no password resolved. Check `HAMMER_PG_PASSWORD`, Airflow's `sql_alchemy_conn` and `~/.pgpass`.
- `psycopg2 is not installed ...`: install the `cache` extra, or rerun `./scripts/uv_setup.sh` (step 1).
- anything containing `Connection refused`: nothing is listening at that host and port. Check that Postgres is running and that the host and port are right.
- anything containing `password authentication failed`: the user or the password is wrong.
- anything containing `timeout expired`: the server didn't answer within `HAMMER_PG_CONNECT_TIMEOUT` seconds (10 by default). Check the host, and any VPN or firewall between you and it.
- anything containing `database "..." does not exist`: create it (step 3), or fix `HAMMER_PG_DB`.

`studio` puts the same prefix on other Postgres errors too, so `studio: database unavailable: permission denied for schema hammer_poc` means the connection worked: see the next entries. If `studio` printed `[studio] stack env from ...` on stderr, check the `HAMMER_PG_*` values in that file too.

**`permission denied for schema hammer_poc` when you try to read or write.** You aren't in `sledgehammer_users`. Have someone with admin on the role run `studio grant your_username`. If you already are, the schema was most likely initialized before the role existed: have whoever ran `studio init` run it again, which gives the role its grants.

**`studio init` stops with `studio: database unavailable: permission denied for ...`.** `studio init` creates and updates the schema, so only the role that first ran it (the schema's owner) or a superuser can run it. Teammates never need to: `studio grant` gives them access, and the owner's `studio init` applies updates for everyone.

**A teammate's runs add no rows to the time-saved ledger, or a `syn` or `par` run logs `Checkpoint push skipped: permission denied for sequence pd_checkpoints_id_seq`.** `sledgehammer_users` lacks the sequence grants those inserts need. Cache hits and stores still work. Have whoever ran `studio init` in step 5 run it again, which grants them; the teammate's own runs can't.

**`studio grant` fails with "must have admin option on role" (Postgres 16 and later: "permission denied to grant role").** You don't have `ADMIN OPTION` on `sledgehammer_users`. Cluster admin needs to grant it with `GRANT sledgehammer_users TO you WITH ADMIN OPTION`.

**`permission denied to create database`.** You don't have `CREATEDB`. Either someone grants it (`ALTER ROLE you CREATEDB`) or they create the database for you.

## What's on git, what isn't

The code on git includes the schema definition (the DDL lives inside `hammer/vlsi/pd_store.py`) and the CLI that applies it. When someone clones the repo and runs `studio init`, the DDL recreates the schema in whichever Postgres they point at.

What isn't on git: the actual data in the tables, the running Postgres server, passwords (`airflow.cfg` is committed with them blank; the real values live in the gitignored `.sledgehammer/airflow-secrets.env.gpg`). Each deployment runs its own database with its own state. The repo is the recipe; the database is whatever you build with it.

If you ever want to move data between deployments, that's a `pg_dump | pg_restore` thing, not a git thing.

## What comes after the basics

Once the cache is running, the usual next asks:

- LDAP authentication instead of Postgres passwords. Tie cache access to your institutional login. `PD_STORE_README.md` lists it as deferred DBA work: LDAP in `pg_hba.conf` plus per-user login roles.
- Per-project group roles. Today there's one `sledgehammer_users`. You could split into `project_gcd`, `project_rocket`, etc., with Row Level Security on the `owner` column for per-row visibility.
- A dedicated host account so the cache database isn't tied to any one person.

None of these block getting started. They're evolutions of the same model.
