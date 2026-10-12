# PD cache time-saved tracker

The PD cache turns an expensive stage (a Genus or Innovus run that takes minutes
to hours) into a near-instant blob restore whenever the same work has been done
before. The time-saved tracker records how much wall-clock and CPU time that
saved, every time it happens, so you can total it across a whole tapeout and
answer "how much time has the cache saved us so far".

This doc explains what gets recorded, how a record is tied to a design, how to
turn recording on and off, and how to confirm it is tracking correctly.

## What gets recorded

One row per cache decision (per stage run), written to the Postgres table
`hammer_poc.pd_cache_events`. Each row has:

- `ts` - when it happened.
- `stage` - synthesis, par, drc, lvs, sram_generator, sim, power, formal, timing, pcb.
- `outcome` - one of:
  - `MISS_STORE`: the tool actually ran and we stored the result. Records the
    tool runtime (`tool_seconds`, `tool_cpu_seconds`). No savings: this is the
    run that populates the cache.
  - `HIT`: restored from cache instead of running. Records `saved_seconds`
    (the original tool runtime minus the few seconds it took to untar).
  - `SKIP_LOCAL`: the dependency check said skip and the files were already on
    disk. Credited to the dependency check, not the cache.
  - `SKIP_RESTORED`: dependency check said skip, files were missing, restored
    from cache. Credited to the cache.
  - `SKIP_NO_BLOB`: skip with no local files and no cache blob. No savings, and
    a warning that downstream may fail.
- timing: `saved_seconds`, `tool_seconds`, `restore_seconds`, plus the
  `*_cpu_seconds` variants. CPU is summed across child processes, so for a
  multi-threaded tool like Innovus it is much larger than wall-clock.
- provenance: `dag_id`, `dag_run_id`, `design`, `module`, `triggering_user`,
  `owner`, `workspace`, `sha256` (the stage cache key). `module` is the block
  the stage ran on: the per-module name in hierarchical flows (`syn-SubModA`
  records `module=SubModA`), the top module in flat flows. Group or filter on
  it for per-block stats in a hierarchical tapeout:

  ```bash
  studio time-saved --group-by module
  studio time-saved --module RocketTile
  ```

The report counts savings the same way the per-run summary does:
HIT and SKIP_RESTORED count as "saved by cache", SKIP_LOCAL as "saved by
dependency check", MISS_STORE as "time that actually ran".

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
    - missing/unknown (rows recorded before this flag existed, or the mtime
      check failed): treated as legacy-equivalent, so the SledgeHammer number
      stays a floor.

The report prints SLEDGEHAMMER TIME SAVED (cache plus make-would-rerun
dep-check skips) directly; that is the paper number. `--cache-only` remains
the even stricter cut that drops every SKIP_LOCAL regardless of flag:

```bash
studio time-saved --project ee290_tapeout               # shows the split + SLEDGEHAMMER total
studio time-saved --project ee290_tapeout --cache-only  # cache restores only
```

Every ledger row stores its outcome (and, for skips, the make verdict), so the
split can always be recomputed from the raw data. Rows recorded before the
flag existed conservatively fall in the legacy bucket.

## What legacy hammer already saves (the baseline)

Checked against upstream ucb-bar/hammer (this repo's origin remote), which has
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
  touched-but-unchanged configs where make would re-run; we conservatively do
  not claim those either.)

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
cache via vlsi.pd_cache.enabled (true cache-off needs the env var unset or 0
and that key absent), and with the cache off no ledger events are recorded, so
time the baseline runs externally.

## How a record is tied to a design

A record is not tied to your current shell directory. It is tied to the
DESIGN_NAME, which is the build directory basename that the make flow derives
from `CONFIG=` and bakes into the DAG when you run `make buildfile`. For example
`make CONFIG=RocketConfig buildfile` produces
`chipyard.harness.TestHarness.RocketConfig-ChipTop`, and that string becomes:

- `dag_id` = `sledgehammer_<DESIGN_NAME>_<user>` (or a custom id if you set
  `vlsi.core.airflow_dag_id` / `make-dag --dag-id`), and
- `design` = `<DESIGN_NAME>` on its own.

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
Precedence is env var, then trigger conf, then config key. From then on every
event that run records carries that project.

Relabel rows already in the ledger (this is the "categorize a dag into a
project" command):

```bash
studio project-set ee290_tapeout --dag RocketConfig    # all RocketConfig rows
studio project-set ee290_tapeout --design BOOMConfig
studio project-set ee290_tapeout --all                 # every row (use with care)
```

### Triggering and verifying from the Airflow UI

When you trigger the DAG from the web UI, set the project in the run
configuration (the "Trigger DAG w/ config" JSON box):

```json
{"project": "ee290_tapeout"}
```

To confirm it landed in the right bucket, open the run's `exit_` task log. The
per-run cache summary printed there now starts with the run's Design and
Project, for example:

```
  Design:  chipyard.harness.TestHarness.RocketConfig-ChipTop
  Project: ee290_tapeout
```

If it says `Project: (no project set)`, the label did not take (check the conf
JSON, or that a config key / env var did not override it). You can still fix it
after the fact with `studio project-set`. For the running total across runs, use
`studio time-saved --project ee290_tapeout` from a shell.

It refuses to relabel the whole ledger without `--all`, and prompts unless you
pass `--yes`. Filters: `--all`, `--dag`, `--design`, `--stage`, `--user`,
`--after`, `--before`. Then report by project:

```bash
studio time-saved --group-by project
studio time-saved --project ee290_tapeout --group-by design
```

## Turning recording on and off

Recording is on by default. It gates only the durable database rows; the cache
itself and the per-run summary that the `exit_` task prints are not affected.

```bash
export HAMMER_PD_CACHE_LEDGER=0     # turn recording OFF (also: false / no / off)
export HAMMER_PD_CACHE_LEDGER=1     # turn it back ON (or just unset the variable)
```

- Config-file form (per design): `vlsi.pd_cache.ledger_enabled: false`.
- For DAG runs, set the variable in the Airflow worker environment (for example
  in `venv.sh`) so it applies to every triggered run.
- The switch only affects new rows. Turning it off does not delete anything, and
  turning it back on resumes appending.

The tracker only sees events the cache produces, so the cache has to be
on too: `HAMMER_PD_CACHE=1` (or `vlsi.pd_cache.enabled: true`). With the cache
off there are no events at all.

## Confirming it is tracking correctly

1. Check the switches and that the table is reachable:

   ```bash
   studio cache-status
   ```

   You want to see `PD cache: ON`, `Time-saved ledger: ON`, and a
   `Durable ledger: N event row(s)` line. If it says the ledger is unreachable,
   the Postgres password is not loaded (source `venv.sh` so the secrets are
   decrypted, or set `HAMMER_PG_PASSWORD`).

2. Note the current row count, run a stage once, then check again. The first run
   of a given stage and config is a `MISS_STORE`: the row count goes up by one
   and the report shows the runtime under "time that actually ran". A single
   cold run shows zero saved, which is expected: it populates the cache rather
   than reusing it.

3. Run the same stage again with the config unchanged. Now it is a `HIT`, and
   `studio time-saved` shows a non-zero "saved by cache". This is the real proof
   that tracking works end to end.

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

- `--group-by stage|dag|design|project|run|none`
- `--dag <substr>` scope to one design's dag (matches dag_id), `--design <substr>`,
  `--project <substr>`, `--stage <name>`, `--user <name>`
- `--since` / `--until` accept epoch seconds or `YYYY-MM-DD` (bound a campaign)
- `--source auto|db|jsonl|both` (default auto: the durable table, falling back to
  the on-disk JSONL if the database is unreachable)

Examples:

```bash
studio time-saved --group-by design                  # one line per design
studio time-saved --dag RocketConfig --group-by run  # per run within a project
studio time-saved --user lawrence --since 2026-06-01  # one person, one window
```

## The paper's CSV (turnaround-time breakdown)

`--csv` writes the TAT table directly: one row per group plus TOTAL, with
eight numeric columns covering the three savings categories in wall-clock
and compute (CPU) seconds:

```bash
studio time-saved -g project --csv tat.csv     # one row per tapeout project
studio time-saved --project ee290 --csv -      # single project, to stdout
```

```
project,dep_management_wall_s,dep_management_cpu_s,caching_wall_s,caching_cpu_s,checkpointing_wall_s,checkpointing_cpu_s,total_wall_saved_s,total_cpu_saved_s
```

The mapping is the same attribution the report uses: dep management counts
only skips legacy make would have rerun (make_would_rerun=True; dep-legacy
skips get no credit), caching counts blob restores, checkpointing counts
sub-step RESUME events, and parallel counts the wall masked by running module
tasks concurrently. Totals are the sum of the four, i.e. SLEDGEHAMMER TIME
SAVED. Checkpointing compute is a floor (resume savings are wall-clock only,
unknown CPU counts as zero); parallel compute is zero by definition --
concurrency overlaps work, it does not remove it. All the usual filters
compose with `--csv` (`--project`, `--since`/`--until`, `--stage`,
`--cache-only`, ...).

Parallel flow execution is computed per run straight from the ledger, no
extra collection: sequential-equivalent time is the sum of every task's wall
duration, actual time is the run's wall span (earliest start to latest end),
and the difference is the time masked by task-level parallelism. Top-level
integration runs after the leaves, so its window does not overlap and cancels
out -- no leaf-vs-top bookkeeping needed. A flat (single-module) run has no
overlap and reports zero.

## Resetting

```bash
studio cache-events-clear --all --yes                # wipe the whole ledger
studio cache-events-clear --dag RocketConfig         # just one project's rows
studio cache-events-clear --stage par --before 2026-06-01
```

It refuses to wipe everything without `--all`, and prompts for confirmation
unless you pass `--yes`. Filters: `--all`, `--dag`, `--design`, `--stage`,
`--user`, `--after`, `--before`.

## Where the code and data live

The tracker code is `hammer/vlsi/time_tracking.py`: event recording, the
ledger and project switches, the per-run summary the `exit_` task prints, and
the cross-run aggregation behind `studio time-saved`. The cache wrapper
(`pd_cache.py`) only measures the tool run and calls into it; the Postgres
table plumbing is in `pd_store.py`; the build-system generator has just the
three-line `exit_` summary hook.

Data:

- `hammer_poc.pd_cache_events` - the durable ledger this tracker reads. Survives
  across runs and machines. This is the source of truth for a tapeout total.
- `hammer_poc.pd_blobs` - the cache itself; each blob carries the
  `duration_seconds` / `cpu_seconds` that a HIT credits as saved.
- `$AIRFLOW_HOME/cache_events/<run_id>.jsonl` - a per-run scratch log that feeds
  the `exit_` task's one-run summary and is deleted right after. Useful for the
  current run only; do not rely on it for history.

## Gotchas

- Zero saved on a fresh design is normal. Savings appear on re-runs once blobs
  exist and the config still matches.
- Changing the config shifts the stage cache key, so the next run is a fresh
  `MISS_STORE` rather than a HIT. That is correct: different inputs, different work.
- Older rows (before the design tag was wired in) have `dag_id` but a blank
  `design`. Group or filter by `dag` to include them, since dag_id already
  carries the design name.
- The report needs the Postgres password to read the durable table. Without it,
  `--source auto` falls back to whatever JSONL files are still on disk.
