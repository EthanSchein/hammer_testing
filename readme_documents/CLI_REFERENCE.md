# SledgeHammer command reference

Every console command the venv installs, what it is for, and the flags that
matter. Two commands are SledgeHammer's own (`sledgehammer`, `studio`); the
rest are Hammer's. SledgeHammer adds `--force`, `--local`, the PD cache and
sub-step resume to `hammer-vlsi`.

Everything here works from a terminal. The web UI is optional: there is no
step in a normal flow that requires it.

| Command | Purpose |
|---|---|
| **`sledgehammer`** | Launch the Airflow stack; generate and register a design's DAG (`sledgehammer buildfile`); run flows from the CLI; Airflow passthrough |
| **`studio`** | Admin + cache CLI for the shared PD database (43 subcommands) |
| `hammer-vlsi` | Hammer's flow driver (syn, par, drc, lvs, build, ...) |
| `get-config` | Print a resolved config key |
| `hammer-generate-mdf` | Generate an MDF memory description from a ReplSeqMem config |
| `hammer-shell-test` | Shell-integration smoke test |
| `readlink-array`, `yaml2json`, `asap7_gds_scale` | Small utilities |


## Getting set up

From a fresh clone, one command builds the environment:

```bash
./scripts/uv_setup.sh
```

It neutralizes conda/mamba/any active venv first (psycopg2 compiles from
source, and a foreign OpenSSL on the linker path gets baked in as an RPATH that
breaks it in every later shell), creates the venv, runs `uv sync --group dev`,
then installs Airflow, asyncpg and the edge3 provider against the official
constraint file, and builds psycopg2 with `--no-binary`.

It also links `sledgehammer` into `~/.local/bin`. Open a new shell and it runs
from any directory, inside a Chipyard tree or not, with nothing sourced.

Which checkout runs depends on where you are. Inside a checkout, a workspace
that holds `hammer/`, or a Chipyard tree with `vlsi/hammer/`, `sledgehammer`
uses that checkout if it has the full stack set up (`.venv/bin/sledgehammer`
and `.venv/bin/airflow`) and is yours: owned by you and not writable by others.
Otherwise, and anywhere else, it uses the checkout that ran setup last, which
the `~/.local/bin` link points at. When setup moves the link, it prints
`was -> <old target>`; when a regular file sits there (a `pip --user`
install, say), setup replaces it with the link and says so. Any copy of `sledgehammer`, including one
pip-installed into a conda env, hands off to that checkout's venv before doing
anything, so the code and Airflow always come from the checkout. Setup warns
about every other `sledgehammer` on your `PATH` or in your conda env, because
an old one may run first without handing off; `type -a sledgehammer` lists
them. `sledgehammer --help` ends with the checkout and python that ran. If you
delete the linked checkout, rerun setup in another one, or remove
`~/.local/bin/sledgehammer`.

The flow commands print a note when `AIRFLOW_HOME` points at a different
checkout, since that checkout's `airflow.cfg` and DAG folder are then used. To
use this checkout's, unset `AIRFLOW_HOME` where it is set (your shell or your
stack env file).
Setup also removes an old `sledgehammer()` shell function from `~/.bashrc` if
it finds one, keeping a backup in `~/.bashrc.sledgehammer.bak`.

The script writes that `PATH` entry to `~/.bashrc` only. If your shell never
reads that file (zsh, csh, or a login shell whose profile does not source
`~/.bashrc`), add `~/.local/bin` to `PATH` yourself.

**Do not run `uv sync` on its own afterwards.** Airflow is installed outside
the lock, so a bare sync uninstalls it. To refresh just this package:

```bash
uv pip install -e . --no-deps
```

Then a database and the schema ([DATABASE_SETUP.md](DATABASE_SETUP.md) for an
existing Postgres, [LOCAL_POSTGRES_SETUP.md](LOCAL_POSTGRES_SETUP.md) to stand
one up yourself). Only the cache database's owner runs `studio init`, once;
on a shared database everyone else skips it:

```bash
source venv.sh && studio init      # database owner only, once
sledgehammer db migrate
```

`venv.sh` is what puts `studio` on `PATH`; setup links only `sledgehammer`
into `~/.local/bin`. `sledgehammer db migrate` is `airflow db migrate` with
the [environment file](#the-environment-file) and the GPG secrets loaded
first, so it reads the same stack settings `studio init` does.

### Running the tests

The default profile installs the dev group, pytest included, into `.venv`.
Activate it first: the driver tests run `hammer-shell-test` by name, so
`.venv/bin` must be on `PATH`.

```bash
source .venv/bin/activate
python -m pytest tests/
```

Not `uv run pytest`: `uv run` first syncs `.venv` to the lock. It does not
remove Airflow, but it reinstalls the doc packages (`myst-parser` and its
markdown dependencies) that setup removes because they conflict with Airflow.

The suite inherits your environment, so run it with `HAMMER_PD_CACHE` unset or
`0` and with `HAMMER_DB_CHECKPOINTS=0`. Otherwise the syn and par tests store
results in, and clear checkpoints from, the cache database that `HAMMER_PG_*`
(or, failing that, Airflow's database connection) points at.

The lab profile (`SLEDGE_LAB=1`) installs no dev tools, so add them first. It
has no Airflow to lose, so the `uv sync` warning does not apply there.
`--inexact` keeps any sibling plugins the script installed outside the lock:

```bash
uv sync --group dev --inexact
```

### The environment file

Write the stack's settings once to `~/.sledgehammer/env.sh` and nothing needs
sourcing again. Every `sledgehammer` command except the bare launch reads it
itself (the flow commands, `buildfile`, and the Airflow passthrough such as
`sledgehammer dags list` or `sledgehammer db migrate`), and so does every
`studio` command:

```bash
mkdir -p ~/.sledgehammer
ln -s /path/to/your/stack_env.sh ~/.sledgehammer/env.sh
```

Discovery order, first hit wins: `$SLEDGE_ENV_FILE`, `~/.sledgehammer/env.sh`,
then a `stack_env.sh` found by walking up from the current directory (so a
workspace can carry its own stack). Anything already exported beats the file,
and when `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` and `HAMMER_PG_HOST` are both
already set, no file is read at all. A command that reads one says
`stack env from <file>`: on stdout for the flow commands and `buildfile`, on
stderr for the passthrough and `studio`.

The bare launch (`sledgehammer`, `sledgehammer standalone`) does not read it:
it sources `venv.sh` and decrypts the GPG secrets instead. Airflow components
you start yourself, such as the tmux lines in
[LOCAL_POSTGRES_SETUP.md](LOCAL_POSTGRES_SETUP.md), still need to source the
file.

That file should set `AIRFLOW_HOME`, `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`,
`AIRFLOW__CORE__DAGS_FOLDER`, `HAMMER_PD_CACHE=1`, and the `HAMMER_PG_*`
connection variables. See LOCAL_POSTGRES_SETUP.md for a complete example.
[ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md) covers the other
variables SledgeHammer reads.

`AIRFLOW__CORE__DAGS_FOLDER` is the one folder the commands agree on, because
they share one resolver. `sledgehammer buildfile` links the DAG it generates
there (unless the project config sets `vlsi.core.airflow_dags_folder`), so
does `studio make-dag` (unless you pass `--dags-folder`), and
`sledgehammer run` looks for it there. You do not need `HAMMER_DAGS_FOLDER`
as well; if you set it, it wins over `AIRFLOW__CORE__DAGS_FOLDER` for all
three. Set the folder with `AIRFLOW__CORE__DAGS_FOLDER`, not with
`AIRFLOW_HOME` alone. The passthrough keeps `AIRFLOW_HOME` on the checkout
even when the file or your shell sets another, so `sledgehammer dags list`
would read the checkout's `dags/` while `run` and the build follow the file's
`AIRFLOW_HOME`.

### Running a design

Legacy generated the per-stage targets first, then ran them:

```bash
cd rocket-vlsi
make buildfile        # hammer build -> hammer.d
make syn
make par
```

SledgeHammer keeps the same two steps, both through `sledgehammer`:

```bash
cd rocket-vlsi
sledgehammer buildfile   # make buildfile underneath: hammer.d AND the DAG
sledgehammer syn
sledgehammer par
```

`sledgehammer buildfile` is the way a DAG is created. You run it from the vlsi
directory, and it runs that directory's `make buildfile` with the venv on
`PATH` (details under
[Generating the DAG](#generating-the-dag-buildfile)). The same Hammer `build`
action emits the Makefile include and the DAG, and registers it, when the
project config sets `vlsi.core.build_system` to `sledgehammer` (or its alias
`airflow`). With `make` it writes only `hammer.d`.

`sledgehammer` builds only when you run `sledgehammer buildfile`. The run and
stage commands never run make, so they never regenerate a DAG you customized.
They find the DAG that builds under, or was generated from, the current
directory, so no flags are needed inside a workspace (see
[Where obj_dir comes from](#where-obj_dir-comes-from)).

Hammer's own `e2e/Makefile` has a `build:` rule and no `buildfile:` rule, so
in `e2e/` the same command runs `make build` by itself. The e2e configs set
`vlsi.core.build_system: make`. To get the DAG too, pass a config that sets it
to `sledgehammer` through `extra=` (extra arguments go to make):

```bash
cd e2e
sledgehammer buildfile extra=<file>
```

If `hammer.d` already exists, delete it first: e2e remakes it only when
`sram_generator-output.json` is newer, so otherwise the `make build`
underneath does nothing. If no DAG exists yet, `sledgehammer buildfile` then says
`the build registered no DAG for this directory` and tells you to check
`vlsi.core.build_system`, even though the real cause is the stale `hammer.d`.

If you have hand-edited a generated DAG, a later `sledgehammer buildfile` will
not silently discard it: the file is copied to
`hammer_dag.py.hand-edited-<timestamp>` and the rebuild says so. For changes
meant to last, prefer driver hooks (`get_extra_hierarchical_par_hooks` and
friends). Those survive regeneration because they live in the driver.

The build registers a DAG by linking it into the dags folder as
`<dag_id>.py`. When that link already points at another build's DAG (two
builds whose obj_dirs share a basename, see
[Reading results](#reading-results)), the build prints a `NOTE` that the
dag_id now builds the new obj_dir, and replaces the link. A regular file in
the link's place is not deleted: the build moves it to
`<dag_id>.py.replaced-<YYYYmmdd-HHMMSS>` and prints a `NOTE` saying so.

The one prerequisite `make` does not have: the Airflow stack must be running
(api-server, scheduler, dag-processor, triggerer). It is a service you start
once per machine, not per run.

## sledgehammer

### Generating the DAG: `buildfile`

```bash
cd rocket-vlsi
sledgehammer buildfile                   # make buildfile, or make build where that is the only rule
sledgehammer buildfile -j 4 VAR=val      # every other argument goes to make
sledgehammer buildfile --target NAME     # run another make target
```

`sledgehammer build` is the same command. It exits if the current directory
has no `Makefile`. Without `--target`, it runs `make buildfile`, or
`make build` when the Makefile has a `build:` rule and no `buildfile:` rule.

No venv needs activating: it puts the venv's `bin` directory first on `PATH`
and sets `VIRTUAL_ENV` for make. It drops conda, miniforge, mamba and pixi
entries from `LD_LIBRARY_PATH` (their `libcrypto` breaks psycopg2); `PATH` is
otherwise unchanged. It reads the [environment file](#the-environment-file),
keeps an `AIRFLOW_HOME` that the file or you set (defaulting to the checkout),
and loads no GPG secrets.

If make fails, it exits with make's status. Otherwise it prints
`registered sledgehammer_<design>_<user> (obj_dir ...)` for each of your DAGs
that builds under, or was generated from, the current directory (every such
DAG, not only the one just built), then the next command,
`next: sledgehammer syn` (with `-t <design>` when it listed more than one).
If it finds none, it prints a hint to check that
`vlsi.core.build_system` is `sledgehammer` and that
`vlsi.core.airflow_dags_folder`, if set, is the folder Airflow reads, and
exits 0.

### Running flows (no GUI)

The flags are Hammer's, so a Makefile switches over by changing the binary.

```bash
# trigger, stream to the terminal, exit with the run's status
sledgehammer run syn par --obj_dir build/ChipTop

# stages compose; these two run in parallel
sledgehammer run drc lvs --obj_dir build/ChipTop

# fire and forget
sledgehammer run syn --obj_dir build/ChipTop --no-wait
```

Stages: `sim_rtl power_rtl syn sim_syn timing_syn formal_syn power_syn par
sim_par timing_par formal_par power_par drc lvs pcb` (dashes accepted). `pcb`
writes the board deliverables (bump footprint, pin map, schematic symbol) from
the configs alone, so it runs once for the whole design, alongside the first stages.

| Flag | Meaning |
|---|---|
| `--obj_dir` | build directory; optional, see [Where obj_dir comes from](#where-obj_dir-comes-from) |
| `-t`, `--top` | the registered DAG to run, by the name it was registered under (the obj_dir basename, see [Reading results](#reading-results)); `--design` is an alias. If `sledgehammer_<name>_<user>.py` is not in the dags folder, the command exits with `no DAG registered for <name>` and tells you to run `sledgehammer buildfile`. It never falls back to the current directory's DAG. Without it, the name comes from obj_dir |
| `--module M` | restrict to these modules, repeatable (hierarchical flows) |
| `--force`, `--redo` | rerun the named stages even if nothing changed: skip the cache lookup, overwrite the cache entry, mark later stages to rerun. Earlier stages the run needs keep their normal check |
| `--forceall`, `--redo-all` | the same for every stage the run executes, earlier ones included |
| `--local` | skip the DB cache pull, run the tool locally |
| `--tools NAME` | build with that tool config instead of the DAG's default; NAME is one of the DAG's Tools choices (the configs-tool/*.yml names `studio make-dag` found) |
| `--from-step` / `--to-step` / `--only-step` | sub-step control. The steps always run: a step-flag run skips the cache lookup and is never stored in the cache |
| `--steps-stage {syn,par}` | which stage the step flags apply to (default `syn`, so a par resume needs `--steps-stage par`) |
| `--workspace`, `--project` | workspace routing, ledger project label |
| `--run-id` | name the run instead of an auto `cli_<epoch>` |
| `--no-wait` | trigger and return immediately |

A generated DAG keeps the behavior of the SledgeHammer that generated it. After
you update SledgeHammer, regenerate your DAGs (`sledgehammer buildfile` from the
vlsi directory, or `studio make-dag`) and restart Airflow, so flags such as
`--forceall` and `--tools` work as described here. `sledgehammer` warns when
`--force` meets a DAG too old to force only the named stages.

A non-default tool builds in the same obj_dir as the default one, so switching
tools reruns the stages and replaces the other tool's results there.

The exit code follows the run, so `sledgehammer run ... && next-step` behaves.
Ctrl-C detaches from the stream; the run keeps going.

### Reading results

```bash
sledgehammer status --design ChipTop     # latest run, per-task states
sledgehammer status --design ChipTop --run-id lvsfinal2_1787205495
sledgehammer runs --design ChipTop -n 5  # recent runs
```

`--design` takes the name the DAG was registered under: the obj_dir basename,
so `ChipTop` here matches the `--obj_dir build/ChipTop` runs above. In
Chipyard that name is long, such as
`chipyard.harness.TestHarness.RocketConfig-ChipTop`, and
`sledgehammer buildfile` prints it as
`registered sledgehammer_<name>_<user> (obj_dir ...)`. Without `--design`,
both commands cover every DAG you own.

Because the dag_id is `sledgehammer_<obj_dir basename>_<user>`, builds whose
obj_dirs share a basename (`build-sky130-cm/gcd` and `build-asap7-cm/gcd`)
share one dag_id, and only the one generated last is registered. The run
commands refuse to run it against the other build's obj_dir (see
[Where obj_dir comes from](#where-obj_dir-comes-from)).

### Stack and passthrough

```bash
sledgehammer                  # launch the stack
sledgehammer standalone       # same
sledgehammer db migrate       # any airflow subcommand, stack env file and secrets loaded
sledgehammer dags list
SLEDGE_DRYRUN=1 sledgehammer  # print what it would run
```

Every form that talks to Airflow loads what it needs to reach the metadata DB,
so nothing has to be exported by hand. What each one loads:

- The bare launch (`sledgehammer`, `sledgehammer standalone`) pins
  `AIRFLOW_HOME` to the checkout, sources `venv.sh` and decrypts the GPG
  secrets. It reads no stack env file.
- The passthrough (`sledgehammer db migrate`, `sledgehammer dags list`, any
  other Airflow subcommand) pins `AIRFLOW_HOME` to the checkout, reads the
  [environment file](#the-environment-file), then decrypts the GPG secrets.
  The decrypt is skipped when the file or your shell already set
  `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`.
- The flow commands (`run`, `status`, `runs`, and the make target names such
  as `syn` or `par-RocketTile`) read the environment file, then the secrets
  (skipped the same way), and *respect* an `AIRFLOW_HOME` that the file or you
  set, defaulting to the checkout. So they compose with an existing stack.
- `buildfile` treats the environment file and `AIRFLOW_HOME` the same way as
  the flow commands, but loads no secrets.

The web UI has one login: you, under your OS user name (`$USER`). The launch
keeps your password in `.sledgehammer/airflow-ui-password.json` in the checkout, readable only by you,
and makes up a random one the first time. It prints where the file is, never
the password. To choose your own, edit the value in that file; the next login
uses it, with no restart. The UI listens on 127.0.0.1 only, so from another
machine use the SSH tunnel command the launch prints.

## studio

When a `studio` command cannot reach the cache database, or psycopg2 or the
database password is missing, it prints one line on stderr,
`studio: database unavailable: <reason>`, and exits 1. If the database does
not answer at all, the connection attempt gives up after
`HAMMER_PG_CONNECT_TIMEOUT` seconds (default 10). Most Postgres errors after
a working connection, such as `permission denied`, get the same prefix, so go
by the reason: [DATABASE_SETUP.md](DATABASE_SETUP.md#common-gotchas) lists the
usual ones. `project-set` and `cache-events-clear` print
`error: ledger unreachable (<reason>)` on stdout instead, and also exit 1.
`time-saved` and `cache-status` are covered in their sections.

### Time saved: `time-saved` (the paper numbers)

```bash
studio time-saved                          # headline totals
studio time-saved --group-by design        # per design
studio time-saved --design ChipTop --since 2026-08-03
studio time-saved --csv tat.csv --group-by project
studio time-saved --cache-only             # exclude legacy-equivalent skips
```

`time-saved` reads the Postgres ledger by default (`--source auto`). When the
database cannot be reached, it reports from the JSONL event files instead,
prints `WARNING: reporting from jsonl files (DB unavailable: <reason>)` on
stderr, and exits 1 only if that leaves no events to report. `--source both`
does the same. `--source db` prints the one-line error above and exits 1.

Parallel-flow savings are measured from Airflow task windows. When the ledger
spans more than one Airflow instance (after a migration or a merge), point at
the others or every run recorded elsewhere scores as sequential:

```bash
export SLEDGE_EXTRA_METADATA_CONNS="postgresql://user:pw@host:5433/airflow_a,postgresql://user:pw@host:5433/airflow_b"
```

### Cache and artifacts

`blob-list` `blob-find` `blob-delete` `blob-reassign` `stage-key` `stage-push`
`stage-pull` `cache-status`

```bash
studio cache-status                     # is the cache and ledger on and reachable
studio blob-list -n 20
studio blob-delete --design gcd --dry-run
```

`cache-status` always prints whether the cache and the ledger are on, going by
`HAMMER_PD_CACHE` and `HAMMER_PD_CACHE_LEDGER` as your shell or the
[environment file](#the-environment-file) sets them. It reads no project
config, so a `vlsi.pd_cache` key in a design's config does not change what it
shows. When it cannot reach the ledger, it shows it as `unreachable (<reason>)`
and exits 1.

Direct `hammer-vlsi` and `make` runs leave the cache **off unless enabled**:
`HAMMER_PD_CACHE=1` or `vlsi.pd_cache.enabled: true`. A direct run that
records nothing usually means this. DAG runs under the `sledgehammer` launcher
are **on by default**: it sets `HAMMER_PD_CACHE=1` when the variable is unset,
and `vlsi.pd_cache.enabled: false` cannot override that because the variable
is checked first. Launch with `HAMMER_PD_CACHE=0 sledgehammer` to turn them
off (the config key then decides). `false`, `no` and `off`, in any case, work
as well as `0`.

`stage-push` and `stage-pull` are the manual pair, keyed on
`master_database.json` alone rather than on the automatic cache's key, so
`stage-pull` finds only what `stage-push` stored. `stage-pull` extracts the
tarball as `--rundir` itself, and refuses (exit 2) an existing `--rundir`
without `--overwrite`, or one whose obj_dir another run has locked. See
[Per stage tarballs](PD_STORE_README.md#per-stage-tarballs).

### Checkpoints

`checkpoints` `checkpoints-push` `checkpoints-fetch` `checkpoints-clear`

Sub-step checkpoints stream to the database while syn and par run, so a killed
task loses at most one push interval. Tune with
`HAMMER_CHECKPOINT_STREAM_SECS` (default 300); `HAMMER_CHECKPOINT_STREAM=0`
turns streaming off.

### Master database

`master-push` `master-pull` `master-list`, plus the older artifact verbs
`list` `get` `put`

### Users and access

`onboard` `grant` `revoke`

### Workspaces

`workspace-list` `workspace-show` `workspace-set` `workspace-unset`

```bash
studio workspace-set alice /scratch/alice/build
```

Only `studio workspace-set` creates workspace rows. Runs never do: an
unregistered user is an error naming the fix, not a silent default.
`studio workspace-show <user> [--name N]` is read-only too. It prints the
root, or, for a missing workspace, prints
`No workspace '<name>' registered for '<user>'. Set one: studio workspace-set <user> /path`
on stderr and exits 1. After `studio workspace-unset`, the user stays
unregistered until a new `studio workspace-set`.

### Notifications

`notify-email` `smtp-setup` `notify-test`

```bash
studio smtp-setup --user berkeley.sledgehammer.studio@gmail.com --password-stdin
studio notify-email lawrencejrhee "a@berkeley.edu, b@gmail.com"
studio notify-test
```

`studio notify-test` prints
`test email sent to <addr>: the SMTP server accepted it, so check the inbox`
only when the send succeeds. When sending fails, it prints
`test email to <addr> failed: <SMTP error>` on stderr and exits 1. It exits 2
when there is no `--to` and no registered address, or when
`SLEDGE_SMTP_USER` or `SLEDGE_SMTP_PASSWORD_FILE` is unset (it tells you to
run `studio smtp-setup --user <sender-address>`). A run's task log likewise
shows `[notify] emailed ...` only when a message was actually sent; otherwise
it says why, such as
`[notify] no SMTP sender configured (SLEDGE_SMTP_* unset); not sending`,
`[notify] FAILED to send ...`, or that no recipient was found.

Completion mail is sent by a task in the DAG, not an Airflow callback:
Airflow 3.1 serializes DAGs without their callbacks, so a DAG-level callback
never fires.

### Design setup and housekeeping

`design-register` `augment` `make-dag` · `init` `reap` `project-set`
`cache-events-clear` `wipe-blobs` `wipe-master` `wipe-artifacts` `wipe-all`

`studio make-dag --driver PATH` names the design's driver script, which every
DAG task runs. It defaults to `<repo>/e2e/example-driver`, the same driver
make uses, and the tasks run from `e2e/` as make's do, so relative
`input_files` such as `src/gcd.v` resolve.

`studio make-dag` does not merge every `*.yml` in the design directory. After
the PDK config (`configs-pdk/<pdk>.yml`) and the tool config, it uses the
design directory's `common.yml`, `<pdk>.yml`, `<pdk>-extras.yml`, `syn.yml`
and `par.yml`, whichever exist, in that order, then each `--conf PATH`
(repeatable) in the order you give them. A relative `--conf` path resolves
against the directory you run `make-dag` from, and one that does not exist
exits 2 before anything is generated. It warns on stderr which design files it
left out (for `gcd` on sky130: `asap7.yml`, the `sim-*.yml` and `power-*.yml`
files, `joules.yml` and `voltus.yml`) and tells you to add any you need with
`--conf`. So a hierarchical config such as
`e2e/configs-design/pass/mock_hier.yml`, or a stage config such as
`sim-rtl.yml` for `sim_rtl`, needs `--conf`, also when you regenerate a DAG.
For `gcd`, `syn.yml` loads after `sky130.yml`, so its 20 ns clock wins over
`sky130.yml`'s 10 ns.

`wipe-blobs [--stage X]` and `wipe-all` also delete the tarballs'
`pd_blob_chunks` rows. Each of them sweeps every chunk row left without a
parent tarball.

```bash
studio init            # create the schema, or update an existing one and its grants
studio reap            # list EDA processes left by dead runs (--kill to end them)
```

## Related documents

Setup lives in its own files: [DATABASE_SETUP.md](DATABASE_SETUP.md) (an
existing Postgres), [LOCAL_POSTGRES_SETUP.md](LOCAL_POSTGRES_SETUP.md)
(standing one up), [WORKSPACE_SETUP.md](WORKSPACE_SETUP.md), [SECRETS.md](SECRETS.md),
[TIME_SAVED_TRACKER.md](TIME_SAVED_TRACKER.md),
[SUBSTEP_RESUME.md](SUBSTEP_RESUME.md).

## Coming from legacy Hammer

Same flags, same target names. The differences are that stages compose in one
command, modules run in parallel, and the bridges between stages are DAG edges
instead of things you type.

### Running a flow

| Legacy Hammer | SledgeHammer |
|---|---|
| `make syn` | `sledgehammer syn` |
| `make par` (chains syn) | `sledgehammer par` |
| `make all` | `sledgehammer run syn par` |
| `make drc` then `make lvs` | `sledgehammer run drc lvs` *(parallel)* |
| `make pcb` | `sledgehammer pcb` |
| `make syn-RocketTile` | `sledgehammer syn-RocketTile` |
| `make par-RocketTile` | `sledgehammer par-RocketTile` |
| `make redo-syn-RocketTile` | `sledgehammer redo-syn-RocketTile` |
| `make buildfile` | `sledgehammer buildfile` *(runs `make buildfile` underneath)* |
| `make hier-par-to-syn-Top` | *(implicit: a DAG edge)* |
| `make par-A; make par-B; ...` (7×, serial) | `sledgehammer par` (all leaves, parallel) |

Raw driver form, if you prefer it over the Makefile:

```bash
# legacy: every config re-passed, one action, blocking
./iris-vlsi -e env.yml -p pdks/techname.yml -p design.yml -p io/iris-bumps.yml \
    -p specs/constr/iris-rockettile.yml ... -p build/IrisVLSITop/inputs.yml \
    --obj_dir build/IrisVLSITop par

# sledgehammer: buildfile once from the vlsi dir, then the DAG remembers the configs
sledgehammer buildfile
sledgehammer par -t IrisVLSITop        # every run, from anywhere
```

### Flags

Identical to `hammer-vlsi` unless noted.

| Flag | Legacy | SledgeHammer |
|---|---|---|
| `-e`, `-p` | yes | n/a (configs are baked in at `sledgehammer buildfile`) |
| `--obj_dir` | yes | optional: read from the registered DAG |
| `-t`, `--top` | yes (the top module) | names the registered DAG: the obj_dir basename, which is not always the top module (`--design` also accepted). With no DAG of that name it exits with `no DAG registered for <name>`, never falling back to the current directory's DAG |
| `--force` | yes | yes (`--redo` also accepted); forces only the named stages |
| `--forceall` | n/a | forces every stage the run executes (`--redo-all` also accepted) |
| `--local` | yes | yes |
| `--tools NAME` | n/a (one tool per build dir) | picks the DAG's Tools choice for this run |
| `--start_before_step` / `--from_step` | yes | yes |
| `--stop_after_step` / `--to_step` | yes | yes |
| `--only_step` | yes | yes |
| `--module M` | n/a (separate target per module) | restrict to modules, repeatable |
| `--workspace`, `--project` | n/a | workspace routing, ledger label |
| `--no-wait`, `--run-id` | n/a | trigger-and-return, name a run |
| `--syn_rundir` etc. | yes | not mirrored: the DAG derives rundirs per module |
| `-v`, `-f`, `-o` | yes | not mirrored: inputs and outputs flow along DAG edges |

### Where obj_dir comes from

Legacy took it from the Makefile, so you had to `cd vlsi` first. A generated
DAG has `OBJ_DIR` baked in, so running one needs neither:

1. `--obj_dir` when given
2. `-t <name>` → read out of the DAG registered under that name. If there is
   none, the command exits with `no DAG registered for <name>`; it never falls
   through to the current directory's DAG
3. bare command (neither `-t` nor `--obj_dir`) → the DAG whose `OBJ_DIR` is
   under the current directory, or whose `WORK_DIR` (the directory it was
   generated from) is the current directory. If more than one of your DAGs
   matches, the command lists them and exits; pick one with `-t <design>`
4. `$OBJ_DIR`
5. `build/<x>`, when it is the only directory under `build/`

`sledgehammer` never runs make to find obj_dir: make remakes an out of date
`hammer.d`, and with it the DAG, before it runs any goal, so even asking it
could regenerate them.

When obj_dir comes from `--obj_dir`, `$OBJ_DIR` or the only `build/<x>`, the
run and stage commands also check it against the registered DAG. If that
obj_dir holds its own `hammer_dag.py` but the DAG registered under the same
name builds a different obj_dir, they exit with
`<dag_id> is registered for <X>, not <Y>`, explain that only the build
generated last is registered (see [Reading results](#reading-results)), and
tell you to regenerate the DAG from `<Y>` with `sledgehammer buildfile`.

### Actions with no legacy equivalent

```bash
sledgehammer status --design Top        # per-task state of the latest run
sledgehammer runs --design Top          # run history
sledgehammer run par --steps-stage par --from_step route_opt_design   # resume mid-stage
studio time-saved                       # what the flow saved vs a make-style rerun
```

Substep resume (a killed par restarts from its last tool-confirmed checkpoint)
and the shared cache (a colleague's identical syn restores in seconds) have no
legacy counterpart at all.
