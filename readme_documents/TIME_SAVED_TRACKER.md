# PD cache time-saved tracker

The PD cache turns an expensive stage (a Genus or Innovus run that takes minutes
to hours) into a near-instant blob restore whenever the same work has been done
before. The time-saved tracker records how much wall-clock and CPU time that
saved, every time it happens, so you can total it across a whole tapeout and
answer "how much time has the cache saved us so far".

This doc explains what gets recorded, how a record is tied to a design, how to
turn recording on and off, and how to confirm it is tracking correctly.

## What gets recorded

One row per cache decision or resume (per stage run), written to the Postgres
table `hammer_poc.pd_cache_events`. Each row has:

- `ts`: when it happened.
- `stage`: the stage tag. Only `synthesis` and `par` go through the cache and
  sub-step resume today, so those are the only values you will see.
- `outcome`: one of:
  - `MISS_STORE`: the tool actually ran and we stored the result. Records the
    tool runtime (`tool_seconds`, `tool_cpu_seconds`). No savings: this is the
    run that populates the cache. A run with step flags is recorded the same
    way but not stored.
  - `HIT`: restored from cache instead of running. Records `saved_seconds`
    (the original tool runtime minus the restore time, which covers fetching
    the blob from Postgres and untarring it).
  - `SKIP_LOCAL`: the dependency check said skip and the files were already on
    disk. Credited to the dependency check, not the cache.
  - `SKIP_RESTORED`: dependency check said skip, files were missing, restored
    from cache. Credited to the cache.
  - `SKIP_NO_BLOB`: skip with no local files and no cache blob. No savings, and
    a warning that downstream may fail.
  - `RESUME`: a syn or par run picked up from its last tool-confirmed
    checkpoint instead of starting over. Recorded once the resumed run
    succeeds, with the measured time of the skipped steps as `saved_seconds`.
    Credited to sub-step resume, not the cache.
- timing: `saved_seconds`, `tool_seconds`, `restore_seconds`, `store_seconds`
  (tar and upload time on a `MISS_STORE`, not netted out of any savings), plus
  `saved_cpu_seconds` and `tool_cpu_seconds`. CPU is summed across child
  processes, so for a multi-threaded tool like Innovus it is much larger than
  wall-clock.
- provenance: `dag_id`, `dag_run_id`, `design`, `module`, `triggering_user`,
  `owner`, `workspace`. The table also has a `sha256` column for the stage
  cache key, but nothing fills it in yet, so it is blank on every row. `module`
  is the block the stage ran on: the per-module name in hierarchical flows
  (`syn-SubModA` records `module=SubModA`), the top module in flat flows. Group
  or filter on it for per-block stats in a hierarchical tapeout:

  ```bash
  studio time-saved --group-by module
  studio time-saved --module RocketTile
  ```

The report counts savings the same way the per-run summary does:
HIT and SKIP_RESTORED count as "saved by cache", SKIP_LOCAL as a
dependency-check skip (split by the make verdict below), RESUME as "saved by
substep resume", MISS_STORE as tool time that actually ran.

## Attributing savings: SledgeHammer vs legacy hammer

For reporting real statistics, the two savings buckets are not equally
attributable to SledgeHammer, and the report keeps them separate (both in the
per-group columns and in the totals) so you can quote them honestly:

- "Saved by cache" (HIT + SKIP_RESTORED) is the SledgeHammer number. These
  restores need the shared Postgres blob cache, which legacy hammer does not
  have. SKIP_RESTORED belongs here too: the dependency check wanted to skip but
  the local files were gone, so without the cache the stage would have re-run.
- Dependency-check skips (SKIP_LOCAL) are split by the recorded
  `make_would_rerun` flag. At skip time the tracker replays legacy make's
  decision using the same prerequisite list hammer.d gives make (every
  project/env config plus the RTL files, compared by mtime against the stage
  output):
    - `make_would_rerun = true`: legacy make would have rerun the stage (for
      example a config file was rewritten but the settings that matter didn't
      change). The content-aware dependency check saved this; it counts as
      SledgeHammer time, reported as "saved by dep-check (make would rerun)".
    - `make_would_rerun = false`: legacy make would have skipped it too.
      Reported as "legacy-equivalent skips" and given no SledgeHammer credit.
    - missing/unknown (the row has no flag, or the mtime check failed):
      treated as legacy-equivalent, so the SledgeHammer number stays a floor.

The report prints SLEDGEHAMMER TIME SAVED (cache, make-would-rerun dep-check
skips, sub-step resume and parallel flow execution) directly; that is the
paper number. `--cache-only` remains the even stricter cut that drops every
SKIP_LOCAL regardless of flag:

```bash
studio time-saved --project ee290_tapeout               # shows the split + SLEDGEHAMMER total
studio time-saved --project ee290_tapeout --cache-only  # no dep-check skips
```

Every ledger row stores its outcome (and, for skips, the make verdict), so the
split can always be recomputed from the raw data. A skip row without the flag
conservatively falls in the legacy bucket.

## What legacy hammer already saves (the baseline)

Checked against upstream ucb-bar/hammer (this fork's parent repo), which has
exactly three time-saving mechanisms, all local-filesystem and timestamp based:

- Make action skipping. The generated hammer.d turns each action into a file
  target: syn is skipped when syn-output-full.json is newer than every
  prerequisite, and the prerequisites are ALL the config files plus the RTL.
  Touching any config re-runs the action and everything downstream. Pure
  mtime comparison, no content hashing.
- redo-<action> targets, which re-run one action without the dependency
  cascade. Correctness is the user's responsibility.
- Step-level resume inside one action (--from_step / --only_step and friends),
  backed by the genus/innovus write_db checkpoints left in the run dir.

Upstream has no shared cache, no content-based change detection, and no
master_database. A fresh checkout or a wiped build dir re-runs everything.

Mapping that onto the tracker outcomes:

- HIT and SKIP_RESTORED have no legacy equivalent. Legacy re-runs the tool
  whenever outputs are missing; only the shared cache turns that into a
  restore. These are safe to claim.
- SKIP_LOCAL usually has a legacy analog: with the outputs present and up to
  date, make would have skipped the same stage. This is why --cache-only
  excludes it. (The fork's check is content-based, so it also skips
  touched-but-unchanged configs where make would re-run. The default report
  credits those as "saved by dep-check (make would rerun)"; --cache-only drops
  them too.)

## Running a legacy-equivalent baseline (A/B measurement)

Turning the cache off does NOT give you legacy hammer: the fork's dependency
check (stage_change_check against obj_dir/master_database.json) still skips
unchanged stages with the cache disabled. For a true upstream-equivalent run,
bypass both layers:

```bash
# direct CLI: cache off, dependency check bypassed
HAMMER_PD_CACHE=0 ./ee290-vlsi ... --force syn

# via make: redo-* skips make's own timestamp gate, --force skips the dep-check
make redo-syn HAMMER_EXTRA_ARGS='--force'
```

Or set `vlsi.core.build_system: hammer` and run `make buildfile`, which gives
the same baseline from one setting with nothing to pass per run. hammer.d is
the same as for `make`, and every hammer-vlsi action then runs as upstream
Hammer: the tool runs each time make invokes it, with no dependency check or
master_database.json, PD cache, sub-step resume, checkpoint streaming, error
scan, LVS report verdict, ledger events, fingerprinting or Postgres. Each
action forces HAMMER_PD_CACHE, HAMMER_PD_CACHE_LEDGER, HAMMER_SUBSTEP_RESUME,
HAMMER_DB_CHECKPOINTS, HAMMER_CHECKPOINT_STREAM and HAMMER_ERROR_SCAN to 0 (and
the two fingerprint memos off), whatever was exported, overrides the
vlsi.pd_cache, vlsi.substep_resume and vlsi.error_scan enables, and prints one
line saying so. Step flags behave as in upstream Hammer. `make` stays
mtime-driven, so use `make redo-syn` (or touch an input) to rerun a stage whose
output is newer than its inputs. The mode is make-driven: a DAG task refuses to
run in it, and `studio make-dag` will not generate a DAG for it.

Each stage reads the mode from its own input configs, and stages after syn get
them from the stage before (par-input.json, par-output-full.json, ...) plus any
project configs make passes after those (the e2e Makefile's HAMMER_EXTRA_ARGS).
After switching `vlsi.core.build_system` to or from `hammer`, rerun from syn
(`make redo-syn`, then the later stages) so the new value reaches every stage.
Switching back to `make` keeps the master_database.json from the earlier make
runs, so the first make run may skip a stage whose inputs have not changed since
then; pass `--force` to rerun it (with the e2e Makefile, `make redo-syn args=--force`).

With the cache off, cache_or_run degenerates to a plain tool run, and --force
short-circuits the dependency check, so the measured wall time is what legacy
hammer would spend. Time a flow that way, run the same flow with the cache on,
and the difference is SledgeHammer's contribution measured rather than
inferred. Two things to watch: a loaded config can silently re-enable the
cache via vlsi.pd_cache.enabled (true cache-off needs the env var unset or off,
meaning `0`, `false`, `no` or `off` in any case, and that key left at its
default or set to false, `0`, `no` or `off`), and with the cache off and
--force no ledger events are recorded, so time the baseline runs externally.

## How a record is tied to a design

A record is not tied to your current shell directory. It is tied to the
DESIGN_NAME, which is the build directory basename that the make flow derives
from `CONFIG=` and bakes into the DAG when you run `sledgehammer buildfile` from
the vlsi directory (it runs `make buildfile` underneath, or `make build` when
the Makefile has a `build` rule and no `buildfile` rule). For example
`sledgehammer buildfile CONFIG=RocketConfig` produces
`chipyard.harness.TestHarness.RocketConfig-ChipTop`, and that string becomes:

- `dag_id` = `sledgehammer_<DESIGN_NAME>_<user>` (or a custom id if you set
  `vlsi.core.airflow_dag_id` / `studio make-dag --dag-id`), and
- `design` = `<DESIGN_NAME>` on its own.

Only the basename counts, not the path above it. Two of your builds whose build
directories share a basename (for example `build-sky130-cm/gcd` and
`build-asap7-cm/gcd`) get the same default `dag_id` and the same `design`, so
their rows carry the same tags. Only the DAG generated last is registered, and
the build prints a NOTE when it takes the DAG over from the other directory.

These are stamped as `HAMMER_AIRFLOW_*` environment variables at the start of
each DAG task (by `_resolve_workspace_obj_dir`) and read by the cache when it
records the event. A direct `ee290-vlsi ... syn` from the shell, outside Airflow,
has no run context, so those tags are left blank for that row.

It is one shared ledger; you pick the scope at report time with filters (see
below). `dag_id` and `design` are auto-derived, so they are the default way to
tell runs apart. If you want a coarser grouping that you control (several
designs under one tapeout), use the project tag (see below).

## Categorizing runs into a project

`project` is an optional label you assign, separate from the auto-derived
`dag_id` / `design`. Use it to bucket several designs or dags under one tapeout
(for example `ee290_tapeout`). There are three ways to set it on new runs, and a
command to relabel rows you already have.

Set it on new runs (any one of):

```bash
export HAMMER_PD_PROJECT=ee290_tapeout        # shell / Airflow worker env
```
```yaml
vlsi.pd_cache.project: ee290_tapeout           # in the design config yml
```
```text
trigger the DAG with conf {"project": "ee290_tapeout"}   # per run, in Airflow
```
`sledgehammer run <stages> --project ee290_tapeout` sets that trigger conf for
you. Precedence is env var, then trigger conf, then config key. From then on
every event that run records carries that project.

Relabel rows already in the ledger (this is the "categorize a dag into a
project" command):

```bash
studio project-set ee290_tapeout --dag RocketConfig    # all RocketConfig rows
studio project-set ee290_tapeout --design BOOMConfig
studio project-set ee290_tapeout --all                 # every row (use with care)
```

### Triggering and verifying from the Airflow UI

When you trigger the DAG from the web UI, set the project in the run
configuration (the configuration JSON in the trigger form):

```json
{"project": "ee290_tapeout"}
```

To confirm it landed in the right bucket, open the run's `cache_summary_` task
log. The per-run cache summary printed there starts with the run's Design
and Project, for example:

```
  Design:  chipyard.harness.TestHarness.RocketConfig-ChipTop
  Project: ee290_tapeout
```

If it says `Project: (no project set)`, the label did not take (check the conf
JSON). If it shows another name, a `HAMMER_PD_PROJECT` set in the worker
environment won over the conf. You can still fix it after the fact with
`studio project-set`. For the running total across runs, use
`studio time-saved --project ee290_tapeout` from a shell.

`studio project-set` refuses to relabel the whole ledger without `--all`, and
prompts unless you pass `--yes`. Filters: `--all`, `--dag`, `--design`,
`--stage`, `--user`, `--after`, `--before`. Then report by project:

```bash
studio time-saved --group-by project
studio time-saved --project ee290_tapeout --group-by design
```

## Turning recording on and off

Recording is on by default. It gates only the durable database rows; the cache
itself and the per-run summary that the `cache_summary_` task prints are not
affected.

```bash
export HAMMER_PD_CACHE_LEDGER=0     # turn recording OFF (also: false / no / off)
export HAMMER_PD_CACHE_LEDGER=1     # turn it back ON (or just unset the variable)
```

- Config-file form (per design): `vlsi.pd_cache.ledger_enabled: false`. It
  stops the cache's rows only: sub-step `RESUME` rows go by the environment
  variable alone.
- For DAG runs, set the variable in the Airflow worker environment (for example
  in `venv.sh`) so it applies to every triggered run.
- The switch only affects new rows. Turning it off does not delete anything, and
  turning it back on resumes appending.

Almost every event comes from the cache, so the cache has to be
on too: `HAMMER_PD_CACHE=1` (or `vlsi.pd_cache.enabled: true`). When you start
Airflow with `sledgehammer`, the launcher turns it on unless you launch with
`HAMMER_PD_CACHE=0`. With the cache off the only events are sub-step `RESUME`
rows.

## Confirming it is tracking correctly

1. Check the switches and that the table is reachable:

   ```bash
   studio cache-status
   ```

   You want `ON` on the `PD cache` and `Time-saved ledger` lines, and an
   `N event row(s)` count on the `Durable ledger` line. Like every `studio`
   command, it first reads the stack env file (`$SLEDGE_ENV_FILE`, else
   `~/.sledgehammer/env.sh`, else a `stack_env.sh` found walking up from the
   current directory) and names the file it read on stderr. What you exported
   in your shell wins over the file, and the file is skipped when your shell
   already sets both `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` and `HAMMER_PG_HOST`.
   So `PD cache` shows `ON` when you exported `HAMMER_PD_CACHE=1` or the stack
   env file sets it (the example in
   [LOCAL_POSTGRES_SETUP.md](LOCAL_POSTGRES_SETUP.md) does). Otherwise it shows
   `off`, even when the launcher or a design's `vlsi.pd_cache.enabled` turns
   the cache on for DAG runs: the command sees neither. Likewise the
   `Time-saved ledger` line reads only `HAMMER_PD_CACHE_LEDGER`, so it shows
   `ON` for a design whose config sets `vlsi.pd_cache.ledger_enabled: false`.

   If the `Durable ledger` line says `unreachable (...)`, the command exits 1,
   so a script can test for it, and the text in parentheses names the cause:

   - `No Postgres password found`: the password is not loaded. Source `venv.sh`
     so the secrets are decrypted, or set `HAMMER_PG_PASSWORD` in your shell or
     in the stack env file.
   - `timeout expired` or `Connection refused`: the host or port is wrong, or
     the server is down. A host that does not answer gives `timeout expired`
     after `HAMMER_PG_CONNECT_TIMEOUT` seconds (default 10). Any other server
     message, such as `password authentication failed`, names its own cause.
   - `psycopg2 is not installed`: the venv lacks the Postgres driver.

2. Note the current row count, run a stage once, then check again. The first run
   of a given stage and config is a `MISS_STORE`: the row count goes up by one
   and the report shows the runtime under "Tool time executed". A single
   cold run shows zero saved, which is expected: it populates the cache rather
   than reusing it. Ledger writes never fail a run, so a refused write shows
   only as a count that does not move. If that happens for a teammate in the
   `sledgehammer_users` group, have the account that owns the `hammer_poc`
   schema (usually whoever first ran `studio init`) run `studio init` again
   from a current checkout, so the group gets the grants it needs to add
   ledger rows.

3. Run the same stage again with the config unchanged, in a fresh build dir
   (another workspace, or after wiping the design's build dir). Now it is a
   `HIT`, and `studio time-saved` shows a non-zero "saved by cache". This is the
   real proof that tracking works end to end. A plain rerun in the same build
   dir is a `SKIP_LOCAL` instead: the dependency check skips it first.

4. Scope to your design and confirm the numbers look right:

   ```bash
   studio time-saved --dag RocketConfig
   ```

A worked example: two HITs, one dependency-check skip, and one miss. That
reports 1h29m50s saved by cache, 1h saved by the dependency check, 2h29m50s
total wall time (and a much larger CPU figure), at a 67 percent hit rate. If
your repeat runs are hitting, the saved totals climb.

## Reading the report

```bash
studio time-saved                       # everything in the ledger, by stage
python scripts/report_time_saved.py     # same report, standalone script
```

Flags (both forms):

- `--group-by stage|dag|design|project|module|run|none`
- `--dag <substr>` scope to one design's dag (matches dag_id), `--design <substr>`,
  `--project <substr>`, `--module <substr>`, `--stage <name>`, `--user <name>`
- `--since` / `--until` accept epoch seconds or `YYYY-MM-DD` (bound a campaign)
- `--source auto|db|jsonl|both` (default auto: the durable table, falling back to
  the on-disk JSONL if the database is unreachable or empty)

When the database cannot be read, `auto` and `both` report from the JSONL files
alone, and the two forms then behave differently:

- `studio time-saved` warns
  `WARNING: reporting from jsonl files (DB unavailable: ...)` on stderr, and
  exits 1 if that leaves no events to report. With `--source db` it stops with
  `studio: database unavailable: <reason>` and exits 1.
- The standalone script prints no warning and exits 0. The database error shows
  only in the report header, `(source: jsonl files (DB unavailable: ...))`, and
  not at all when no events are left (`(no cache events found)`). With
  `--source db` it exits 1 with `Could not read cache events: ...`.

Examples:

```bash
studio time-saved --group-by design                  # one line per design
studio time-saved --dag RocketConfig --group-by run  # per run within one dag
studio time-saved --user lawrence --since 2026-06-01  # one person, one window
```

## The paper's CSV (turnaround-time breakdown)

`--csv` writes the TAT table directly: one row per group plus TOTAL, with
fourteen numeric columns in wall-clock and compute (CPU) seconds: the four
savings categories, their totals, then legacy skips and tool time that ran.
The first column is named after `--group-by` (`scope` with `-g none`). Only
`studio time-saved` has `--csv`; the standalone script does not:

```bash
studio time-saved -g project --csv tat.csv     # one row per tapeout project
studio time-saved --project ee290 --csv -      # single project, to stdout
```

```
project,dep_management_wall_s,dep_management_cpu_s,caching_wall_s,caching_cpu_s,checkpointing_wall_s,checkpointing_cpu_s,parallel_wall_s,parallel_cpu_s,total_wall_saved_s,total_cpu_saved_s,legacy_skip_wall_s,legacy_skip_cpu_s,ran_wall_s,ran_cpu_s
```

The mapping is the same attribution the report uses: dep management counts
only skips legacy make would have rerun (make_would_rerun=True; dep-legacy
skips get no credit), caching counts blob restores, checkpointing counts
sub-step RESUME events, and parallel counts the wall masked by running module
tasks concurrently. Totals are the sum of the four, i.e. SLEDGEHAMMER TIME
SAVED. `legacy_skip_*` holds the dep-legacy skips, which stay out of the
totals, and `ran_*` the tool time that actually ran. Checkpointing compute is
a floor (resume CPU comes from the CPU stamps the checkpoint streamer writes,
and a resume without them counts zero CPU); parallel compute is zero by
definition: concurrency overlaps work, it does not remove it. All the usual
filters compose with `--csv` (`--project`, `--since`/`--until`, `--stage`,
`--cache-only`, ...).

Parallel flow execution needs no extra collection. Per run, the report reads
each task's start and end from Airflow's `task_instance` table (successful and
failed tasks of a minute or more, from the metadata DB plus any listed in
`SLEDGE_EXTRA_METADATA_CONNS`), and falls back to the ledger's own event
windows when Airflow has fewer than two for that run. Sequential-equivalent
time is the sum of those task durations, actual time is the run's wall span
(earliest start to latest end), and the difference is the time masked by
task-level parallelism. Top-level integration runs after the leaves, so its
window does not overlap and cancels out: no leaf-vs-top bookkeeping needed. A
flat (single-module) run can still report some, since drc, lvs, sim, power,
formal and timing run side by side after par. The parallel figure counts every
task of each matching run, so `--stage` and `--module` do not narrow it.

## Resetting

```bash
studio cache-events-clear --all --yes                # wipe the whole ledger
studio cache-events-clear --dag RocketConfig         # just one dag's rows
studio cache-events-clear --stage par --before 2026-06-01
```

It refuses to wipe everything without `--all`, and prompts for confirmation
unless you pass `--yes`. Filters: `--all`, `--dag`, `--design`, `--stage`,
`--user`, `--after`, `--before`.

## Where the code and data live

The tracker code is `hammer/vlsi/time_tracking.py`: event recording, the
ledger and project switches, the per-run summary the `cache_summary_` task
prints, and the cross-run aggregation behind `studio time-saved`. The cache
wrapper (`pd_cache.py`) only measures the tool run and calls into it;
`cli_driver.py` records the `RESUME` events; the Postgres table plumbing is in
`pd_store.py`; the build-system generator only adds the `cache_summary_` task
that prints the summary.

Data:

- `hammer_poc.pd_cache_events`: the durable ledger this tracker reads. Survives
  across runs and machines. This is the source of truth for a tapeout total.
- `hammer_poc.pd_blobs`: the cache itself, keyed by the stage cache key in its
  `sha256` column; each blob carries the `duration_seconds` / `cpu_seconds`
  that a HIT credits as saved. Tarballs over 128 MiB keep their bytes in
  `hammer_poc.pd_blob_chunks`.
- `$AIRFLOW_HOME/cache_events/<run_id>.jsonl`: a per-run scratch log that feeds
  the `cache_summary_` task's one-run summary and is deleted right after. Useful
  for the current run only; do not rely on it for history.

## Gotchas

- Zero saved on a fresh design is normal. Savings appear on re-runs once blobs
  exist and the config still matches.
- Changing a setting the stage reads (its own `<stage>.*` keys or any key
  outside a stage namespace), the elaborated RTL, the library collateral, or an
  upstream output it consumes shifts the stage cache key, so the next run is a
  fresh `MISS_STORE` rather than a HIT. That is correct: different inputs,
  different work. Another stage's settings and the build dir's path are not
  part of the key. Neither are the SledgeHammer keys that choose how a stage
  runs rather than what it produces (`vlsi.pd_cache.enabled`,
  `vlsi.pd_cache.ledger_enabled`, `vlsi.pd_cache.project`,
  `vlsi.substep_resume.*`, `vlsi.error_scan.*` and the `vlsi.core.airflow_*`
  keys), so setting a project label or turning the ledger off never reruns or
  re-keys a stage.
- A row can have `dag_id` but a blank `design`. Group or filter by `dag` to
  include such rows, since dag_id already carries the design name.
- The report needs the Postgres password to read the durable table. Without it,
  or with the database down, `--source auto` falls back to whatever JSONL files
  are still on disk, and `studio time-saved` warns on stderr that it is
  reporting from jsonl files. Each run deletes its file after its summary, so
  usually none are left: the report is `(no cache events found)` and the
  command exits 1. With `--source db` it prints one
  `studio: database unavailable: <reason>` line and exits 1.
