# SledgeHammer Studio: Postgres-backed cache for Hammer PD runs

A storage layer for Hammer that caches per stage build directories in
Postgres, keyed by a content hash of the inputs that determine each stage's
output. When the same inputs come up again, on the same machine or a
different one, the stored tarball is restored instead of rerunning the
tool. The result is that identical PD work runs once, ever.

This work integrates with the dependency team's per stage fingerprinting
(`vlsi.rtl_fingerprint_sha256`, `<stage>.hooks_fingerprint_sha256`,
`stage_change_check`) so a cache hit on our side equals a "no rerun"
decision on theirs.

## What this gets you today

* Ten tables in Postgres on barney. The cache itself is `master_databases`
  and `pd_blobs` (with `pd_blob_chunks` for tarballs too big for one row),
  plus the older par-input JSON store from the earlier POC
  (`pd_artifacts`). [Tables](#tables) lists the rest.
* A `compute_stage_key` function that hashes the slice of a master_database
  that actually determines a stage's output. Matches the comparison surface
  of the dependency team's `stage_change_check`.
* A CLI (`studio`) covering manual push, pull, listing, and access
  management.
* A cache wrapper around `driver.run_synthesis` and `driver.run_par`
  in `cli_driver.py`. With `HAMMER_PD_CACHE=1`, syn or par will skip the
  tool and untar a stored result if one matches. `--force` (and `--local`)
  skip that lookup, run the tool and overwrite the entry for the key.
* Permission gating through a single Postgres group role
  (`sledgehammer_users`), or direct grants on clusters without it. Members
  can read and write the cache. Anyone with neither can't use the tables.

## Tables

All tables live in the `hammer_poc` schema of the cache database (default
`sledgehammer_studio`; override host/db with `HAMMER_PG_*`, see DATABASE_SETUP.md).
`studio init` creates ten. Three of them are below, core columns
only; `_DDL` in `pd_store.py` has the full definitions, including provenance
columns such as `triggering_user`, `dag_id` and `dag_run_id`.

```sql
CREATE TABLE hammer_poc.pd_artifacts (
    sha256      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    top_module  TEXT,
    data        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE hammer_poc.master_databases (
    design      TEXT PRIMARY KEY,
    db          JSONB NOT NULL,
    owner       TEXT NOT NULL DEFAULT current_user,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE hammer_poc.pd_blobs (
    sha256      TEXT PRIMARY KEY,
    stage       TEXT NOT NULL,
    data        BYTEA NOT NULL,
    size_bytes  BIGINT NOT NULL,
    owner       TEXT NOT NULL DEFAULT current_user,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`pd_artifacts` was the original par-input JSON store. It still works and
hasn't been removed, but the real caching path is `master_databases` +
`pd_blobs`. The `owner` column on the two newer tables exists so that flipping
on Row Level Security later (for the per row "hash not found" semantics
Andre asked about) is a one line policy change.

The other six: `pd_blob_chunks` holds `pd_blobs` tarballs too big for one
`bytea` value, `pd_checkpoints` and `pd_checkpoint_chunks` hold sub-step
checkpoints, `pd_cache_events` is the time-saved ledger, `user_workspaces`
maps each user's named workspaces to a root directory, and `user_notify_email`
holds flow-completion email addresses.

## CLI

Connection settings come from `HAMMER_PG_*` environment variables, then
fall back to Airflow's `sql_alchemy_conn` (`$AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`,
else `airflow.cfg`), then to defaults; [Connection settings](#connection-settings)
has the full order. `studio` lives in the checkout's `.venv`, so source
`./venv.sh` first: it activates the venv, sets `AIRFLOW_HOME` and, at a
terminal, decrypts your secrets file ([SECRETS.md](SECRETS.md)), which holds the
conn and `HAMMER_PG_PASSWORD`. `studio` does not decrypt that file itself.

Every `studio` command does read the stack env file before it runs (the
first of `$SLEDGE_ENV_FILE`, `~/.sledgehammer/env.sh` and a `stack_env.sh` in
or above the current directory; see [CLI_REFERENCE.md](CLI_REFERENCE.md#the-environment-file)).
It takes only the variables you have not exported, skips the file when
`AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` and `HAMMER_PG_HOST` are both set
already, and names the file it read on stderr.

When a command cannot use the database (no password, no `psycopg2`, a server
that is down or does not answer within `HAMMER_PG_CONNECT_TIMEOUT` seconds, or
an error from Postgres such as a missing grant), it prints one line on stderr,
`studio: database unavailable: <reason>`, and exits 1. `cache-status`,
`project-set` and `cache-events-clear` print their own `unreachable (<reason>)`
message on stdout instead, and also exit 1. `time-saved` with `--source auto` (the
default) or `both` warns on stderr and reports from the on-disk JSONL event
files; [CLI_REFERENCE.md](CLI_REFERENCE.md#studio) has the details.

### Setup and access

```bash
studio init              # create schema, tables, indexes, default deny
studio grant <role>      # add a Postgres role to sledgehammer_users
studio revoke <role>     # remove them
```

`init` is idempotent. Run it any number of times; it creates what's missing
and leaves the rest alone. It also reapplies the table and sequence grants,
with their default privileges, to `sledgehammer_users` if the role has since
been created (see the DBA prerequisite below). Run it again, as the role that
created the schema, after you update the checkout: that applies new tables,
columns and grants to an existing schema right away. Many of that role's
ordinary calls also apply them once they see the schema is out of date;
calls from users who only have the grants under [Permissions](#permissions)
cannot.

### Master_database

```bash
studio master-push <design> [--master path]
studio master-pull <design> [--out path]
```

The master_database is keyed by design name (e.g. `gcd`). Latest write
wins. `--master` defaults to `./master_database.json`.

### Per stage tarballs

```bash
studio stage-key   <stage> [--master path]            # the key stage-push and stage-pull use
studio stage-push  <stage> --rundir <path> [--master path]
studio stage-pull  <stage> --rundir <path> [--master path] [--overwrite]
studio blob-list   [--stage <tag>] [-n N]
```

`<stage>` is the stage tag, e.g. `synthesis`, `par`, `drc`, `lvs`.
`stage-push` stores the tarball under `compute_stage_key` over
`master_database.json` alone (`--master`, default `./master_database.json`).
That is not the automatic cache's key, which is computed from the live config
after adding entries and rewriting paths (steps 2 and 3 under
[Automatic cache integration](#automatic-cache-integration)). So `stage-pull`
finds only tarballs that `stage-push` stored from a matching
`master_database.json`, never results the automatic cache stored. `stage-key`
prints that same manual key.

`stage-pull` extracts the tarball as `--rundir` itself, whatever the directory
was called when it was pushed. Before downloading anything, it exits 2 when
`--rundir` exists and you did not pass `--overwrite`, or when the parent
directory of `--rundir` (the `obj_dir`) holds another run's
`.sledgehammer-run.lock`. From a shell, any lock there counts as another
run's, and the error names the lock file to remove if that run is dead. It
exits 1 when no tarball has the key, or when the tarball does not hold exactly
one top-level directory. With `--overwrite`, the old `--rundir` is moved aside and
removed only after the new one is in place.

### Legacy par-input JSON store

```bash
studio list
studio get <sha256>
studio put <path> [--kind <label>]
```

These still exist for the original par-input round trip POC. New work
should use the master_database and stage commands above.

## Automatic cache integration

`cli_driver.py` wraps `driver.run_synthesis` and `driver.run_par` in
`pd_cache.cache_or_run`. Off by default, except that DAG runs under the
`sledgehammer` launcher default to `HAMMER_PD_CACHE=1`: the launcher,
`scripts/airflow-standalone-ldap.py`, sets it when it is unset. Case and
surrounding spaces don't matter: `0`, `false`, `no`, `off` or an empty value
leave the cache off unless the `vlsi.pd_cache.enabled` config key turns it on,
and any other value turns it on. Launch with `HAMMER_PD_CACHE=0 sledgehammer`
(or `off`, `false`, `no`) to opt out; the config key then decides. Turn it on
for direct runs with:

```bash
export HAMMER_PD_CACHE=1
hammer-vlsi syn ...
hammer-vlsi par ...
```

On each run, the wrapper:

1. Reads the live config from `driver.database.get_database_json()`.
2. Makes sure the config carries the RTL fingerprint,
   `vlsi.rtl_fingerprint_sha256`, so RTL edits invalidate the cache.
   `cli_driver.py` stores it before the stage runs. If it is missing, the
   wrapper computes it with `compute_rtl_fingerprint` over
   `synthesis.inputs.input_files`, using `synthesis.inputs.defines`,
   `synthesis.inputs.include_dirs` and `synthesis.inputs.top_module`. Both
   go through `digest_or_bytes` in `hammer/vlsi/rtl_check.py`, which hashes
   the design slang 11.0 elaborates (its AST JSON, minus source locations),
   so comment and whitespace edits don't count. Slang always runs with
   `SYNTHESIS` defined, as Genus and Yosys define it, so an edit inside
   `` `ifdef SYNTHESIS `` counts and an edit inside `` `ifndef SYNTHESIS ``
   (assertions, prints) does not. If slang is missing, is not 11.0, or
   rejects the RTL, it falls back to a hash of the file bytes, prefixed
   `bytes:`, and those edits count again. An empty file list gives the
   sha256 of nothing, without running slang.

   `synthesis.inputs.include_dirs` (default `[]`) lists the directories
   searched for `` `include `` files. The fingerprint passes each to slang
   as `-I` and `--isystem`, Genus gets them as `init_hdl_search_path` and
   Yosys as `-I` on `read_verilog`. Relative entries resolve against the
   run's working directory, like `input_files`; add
   `include_dirs_meta: prependlocal` to resolve them against the config
   file instead. Genus accepts only quoted includes, not `` `include <...> ``.
3. Computes the per stage cache key (`compute_stage_key`). Before hashing,
   it adds the tool's script mode if it has one
   (`vlsi.pd_cache.script_mode.<stage>`) and a hash of the build-dir files
   named under `<stage>.inputs` (`vlsi.pd_cache.input_fingerprint.<stage>`),
   and rewrites paths under `obj_dir` to `<OBJ_DIR>`, so the same design in
   another directory gets the same key.
4. Looks up `pd_blobs`. On hit, untars into the rundir and reads
   `<stage>-output.json` to reconstruct the output dict. On miss, runs the
   tool, then tars the rundir and pushes it for next time.

Failures are non fatal. DB outage, missing files, broken tarballs all log
a warning and fall through to a normal stage run. The one exception is a
hit whose `obj_dir` another run has locked: restoring would replace that
run's live rundir, so the task fails instead (`CacheRestoreContested`).

During an outage, each connection the stage opens to the cache database waits
at most `HAMMER_PG_CONNECT_TIMEOUT` seconds (10 by default) for the server
before it gives up, and a refused connection fails at once. A cache database
that is down can slow a stage by up to that much for each connection the stage
tries.

## How the cache key is built

`compute_stage_key(master_db, stage_tag)` hashes the slice of
master_database that `stage_change_check` would compare. Specifically:

* Keep keys starting with `<stage_tag>.` (e.g. `synthesis.*`).
* Drop keys starting with `<stage_tag>.outputs` (those are outputs of the
  stage, not inputs).
* Keep "global" keys that don't start with any known stage tag (e.g.
  `vlsi.core.technology`, `cadence.cadence_home`, `synopsys.*`).
* Drop any key ending in `.needsToRerun` (run bookkeeping, not an input).
* Drop the run-control keys in `RUN_CONTROL_KEYS` (`hammer/config/config_src.py`):
  `vlsi.pd_cache.enabled`, `ledger_enabled` and `project`, the
  `vlsi.substep_resume` and `vlsi.error_scan` settings, and the four
  `vlsi.core.airflow_*` keys. They choose how a stage runs, not what it
  produces, and the dependency check skips them too, so changing one never
  reruns or re-keys a stage. The two entries the wrapper adds under
  `vlsi.pd_cache` (`script_mode.<stage>` and `input_fingerprint.<stage>`, step
  3 under [Automatic cache integration](#automatic-cache-integration)) are not
  among them and stay in the automatic cache's key.

The full list of known stage tags is `KNOWN_STAGE_TAGS` in `pd_store.py`:
`synthesis, par, drc, lvs, sram_generator, sim, power, formal, timing, pcb`.

Verified against Juhyun's real gcd `master_database.json`:

* Same input gives the same hash across repeated calls (stability).
* Mutating `synthesis.inputs.input_files` changes syn's hash (sensitivity).
* The same mutation doesn't change par's hash (selectivity).

`scripts/verify_stage_key.py` reproduces this check on any
`master_database.json` you point it at.

## Permissions

The default is no access. `init` runs `REVOKE ALL ON SCHEMA hammer_poc
FROM PUBLIC` and revokes table grants too. Access normally flows through
one group role:

```sql
sledgehammer_users          -- NOLOGIN role that owns access
```

Members of this role inherit `USAGE` on the schema,
`SELECT, INSERT, UPDATE, DELETE` on every table and `USAGE, SELECT` on every
sequence, now or later (the DDL uses `ALTER DEFAULT PRIVILEGES` for both, so
new tables and sequences the schema owner adds under `hammer_poc` pick up the
same grants). The sequence grants are what let members add rows to
`pd_cache_events` (the time-saved ledger) and `pd_checkpoints`, whose ids come
from sequences. Onboarding a user is one line:

```bash
studio grant colin
```

That runs `GRANT sledgehammer_users TO colin`. If the cluster has no
`sledgehammer_users` role, `grant` gives `colin` the same table and sequence
grants directly instead. `studio onboard colin` always grants directly, and also
creates the `airflow_colin` metadata database. Offboarding is:

```bash
studio revoke colin
```

`revoke` only removes group membership: it fails when the group role does
not exist, and it leaves direct grants in place.

A user with neither group membership nor direct grants trying to read or
write `pd_blobs` gets a Postgres permission error before any row visibility
logic runs. There is no per row gating yet; that's a future RLS policy on
the `owner` column.

## One time DBA prerequisite

`studio revoke`, and `studio grant` on its group path, only work if the
group role exists and the calling role has `ADMIN OPTION` on it. Without
the role, `grant` falls back to direct grants (see
[Permissions](#permissions)). We can't create the role ourselves
on barney because `lawrencejrhee` does not have `CREATEROLE`. A DBA needs
to run:

```sql
CREATE ROLE sledgehammer_users NOLOGIN;
GRANT sledgehammer_users TO lawrencejrhee WITH ADMIN OPTION;
```

After that, re run `studio init` so the conditional grant block
in the DDL picks up the now existing role and applies the table and sequence
grants to it. Ordinary cache calls do not do this: they rerun the DDL only
when the DDL itself has changed.

## Connection settings

Resolved in order. First hit wins per field.

1. `HAMMER_PG_*` env variables: `HAMMER_PG_HOST`, `HAMMER_PG_PORT`,
   `HAMMER_PG_DB`, `HAMMER_PG_USER`, `HAMMER_PG_PASSWORD`.
2. Airflow's `sql_alchemy_conn`, for host, port, user and password. It is read
   from `$AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` when that holds a Postgres URI,
   else from `airflow.cfg` (`$AIRFLOW_HOME/airflow.cfg`, then `./airflow.cfg`,
   then `~/airflow/airflow.cfg`).
3. Hardcoded defaults: `localhost:5433`, user `$USER`. The database is always
   `HAMMER_PG_DB` or `sledgehammer_studio`, never the one named in the conn.
4. Password only: libpq's `PGPASSWORD`, or its pgpass file (`$PGPASSFILE`, else
   `~/.pgpass`). If nothing supplies a password, no connection is attempted.

Every connection to the cache database waits at most `HAMMER_PG_CONNECT_TIMEOUT`
seconds for the server. Set it to a whole number; unset or empty means 10, and
`0` means no limit. When the database cannot be reached, or psycopg2 or the
password is missing, a `studio` command prints one line on stderr,
`studio: database unavailable: <reason>`, and exits 1; for a missing password
the reason starts `No Postgres password found.` A few commands report it their
own way (see [CLI](#cli)). The automatic cache logs a warning and falls through
to a normal stage run instead (see
[Automatic cache integration](#automatic-cache-integration)).

Before this resolution runs, the `studio` CLI reads the stack env file (see
[CLI](#cli)), which fills in any of these variables you have not exported. DAG
tasks and direct `hammer-vlsi` runs do not read that file.

The conn in your secrets file is enough: `venv.sh` and the launcher load it, so
the cache talks to the same Postgres server as Airflow. The committed
`airflow.cfg` leaves `sql_alchemy_conn` blank. DAG tasks don't see that
conn (Airflow 3 gives them a sqlite placeholder), so after loading the secrets the
launcher copies its host, port and user into `HAMMER_PG_HOST`, `HAMMER_PG_PORT`
and `HAMMER_PG_USER` unless they are already set. Every task inherits them along
with `HAMMER_PG_PASSWORD`.

## Files

| File | What it holds |
|---|---|
| `hammer/vlsi/pd_store.py` | Schema, library, cache key derivation, RTL fingerprint, tar helpers, access management |
| `hammer/vlsi/rtl_check.py` | The slang-based RTL fingerprint behind `compute_rtl_fingerprint` and `vlsi.rtl_fingerprint_sha256` |
| `hammer/vlsi/pd_cache.py` | `cache_or_run` wrapper used by `cli_driver.py` for automatic caching |
| `hammer/vlsi/cli_driver.py` | Synthesis and par actions wrap their run calls in `cache_or_run` |
| `hammer/shell/pd_store_cli.py` | The `studio` CLI |
| `scripts/verify_stage_key.py` | Standalone correctness check for `compute_stage_key` |
| `pyproject.toml` | Registers `studio` as a console script |

## What's verified

* `compute_stage_key` against the real gcd `master_database.json`:
  stable, sensitive to syn changes, selective (par hash unchanged when
  syn inputs change).
* `studio` parser builds and all subcommands wire up.
* `cli_driver.py` still imports with the cache wrapper in place.
* Live round trip of the original par-input JSON store against barney
  (from the earlier POC).

## What's not yet done

* Live `init` on barney with the new tables and permissions.
* End to end demo of the cache layer: `init`, push gcd's master_database,
  push `syn-rundir`, pull both into a clean directory, diff against the
  original.
* `HAMMER_PD_CACHE=1` exercise: run syn cold, run again warm, confirm
  second run is a cache hit. Tweak a config, confirm miss. Revert,
  confirm hit again.
* Cross user permissions test with Colin (push as `lawrencejrhee`, read as
  `colin` after `grant`, confirm denied after `revoke`).
* RLS for per row visibility (the `owner` column is already in place; just
  need to enable RLS and add a policy).

## Open coordination with the dependency team

* Master_database addressing for multi user. Today: keyed by design name,
  latest write wins. May want per (design, user) or per (design, branch)
  later.
* Path rewriting. The master_database and the build directories contain
  absolute paths. The cache works around them on its side: the key rewrites
  paths under `obj_dir` to `<OBJ_DIR>`, and a restore points the rundir's
  top-level JSON files at the new build directory. Other restored files,
  such as the Innovus `post_*` symlinks, still point at the original paths.
  A full fix likely belongs on the dependency team side (relativize before
  `commit_master_database`), but needs confirmation.

## Deferred (Andre flagged for later)

* LDAP wiring in `pg_hba.conf` and per user login roles. DBA work.
* Per row visibility through Row Level Security on the `owner` column.
* Cache key that also covers tool versions and environment, not just
  config and RTL.
* Eviction or storage management once `pd_blobs` grows.
* Migration tooling, async driver, connection pooling.
