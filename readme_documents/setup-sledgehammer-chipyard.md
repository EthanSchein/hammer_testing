# Setting up SledgeHammer inside Chipyard (Sp26 Tapeout)

The SledgeHammer variant of the Sp26 Tapeout Chipyard setup. It mirrors the official
course instructions but swaps the two "install hammer into conda" steps for the uv-based
SledgeHammer flow. If you just want vanilla hammer, follow the course README instead. Use
this if you need the SledgeHammer Airflow/Postgres flow.

## Keep in mind

- Don't work under `/home`. Use your `/bwrcq` (or `/scratch`) area.
- Always use the compute servers, not the login servers.
- Source `env.sh` before running anything, in every new shell. The `sledgehammer` commands
  need no activated venv. Source the venv as well only for the course's `make` targets
  (step 5).
- Clean up your VLSI runs regularly so the shared disk doesn't fill up.

## The one big difference: two Python environments

The course installs hammer and the tech plugin **into the conda env** with `pip install -e`.
SledgeHammer can't do that. It needs Airflow plus a source-built psycopg2, which live in a
uv venv. So you run **two separate Python environments that never merge**, layered at run time:

| | Owns | Python | Managed by |
|---|---|---|---|
| **Chipyard** `.conda-env` | EDA/JVM toolchain (gcc, openjdk, sbt, verilator, riscv-tools, firtool) plus `make` | **3.10.14** (pinned `<3.11`) | conda / `build-setup.sh` |
| **SledgeHammer** `vlsi/hammer/.venv` | `hammer-vlsi`, the techname plugin, Airflow plus Postgres | **3.11.15** | uv / `uv_setup.sh` |

They install independently (the uv setup needs nothing from conda), but the VLSI `make`
flow needs both on PATH at once: conda for the tools, the venv so `python3` is hammer's.
This works because Chipyard's build-time Python is stdlib glue. Only `ee290-vlsi` needs
hammer, and it gets the venv. `sledgehammer buildfile` does this layering for you: run it
with `env.sh` sourced and, for the `make` it runs, it puts the venv's `python3` first on
PATH and drops conda entries from `LD_LIBRARY_PATH`, so you don't activate the venv
yourself.

## Setup

Assume you start from the repo root (`cd chipyard`) and conda is active (you see `(base)`).

### 1. Clone and fix permissions

```bash
git clone <chipyard-repo> chipyard
# group-own to techname and make new files inherit the group
chown -R ${USER}:techname chipyard
find chipyard -type d -exec chmod g+s {} +
cd chipyard
```

### 2. Build the conda environment and toolchain

Same as the course: lean conda, skip the FireSim/FireMarshal steps (6 through 9).

```bash
./build-setup.sh --use-lean-conda -s 6 -s 7 -s 8 -s 9   # takes a while; creates .conda-env (Python 3.10.14)
source ./env.sh
```

`build-setup.sh` aborts if `.conda-env` already exists, so delete it or add `-s 1` to skip
the conda step when re-running.

### 3. Install hammer the SledgeHammer way (replaces the course's `pip install -e hammer`)

Clone the **SledgeHammer fork** into `vlsi/hammer` (not `ucb-bar/hammer`), then build its
uv environment. Do **not** `pip install -e hammer` into conda.

If `vlsi/hammer` already exists, run `git -C vlsi/hammer status --porcelain .` first. If it
prints anything, stop: that is uncommitted work, so commit or save it. The steps below clone
into `hammer.new` and move the old `hammer` aside instead of deleting it. For SSH, clone
`git@github.com:juhyundo/hammer_dep.git` instead of the HTTPS URL.

The check does not list gitignored files, such as your encrypted secrets in `.sledgehammer/`.
To keep them, uncomment the `cp` line below, and `uv_setup.sh` skips its secrets prompt. A
new set would get a new Fernet key, which cannot read what your Airflow DB already stored.
Delete `hammer.old.<stamp>` yourself once you no longer need it.

```bash
cd vlsi
git clone <sledgehammer-fork> hammer.new  # e.g. depteam = https://github.com/juhyundo/hammer_dep.git
old=hammer.old.$(date +%s)
[ -d hammer ] && mv hammer "$old"
mv hammer.new hammer
# cp -a "$old"/.sledgehammer hammer/      # keep your existing secrets
cd hammer
./scripts/uv_setup.sh                     # run from the hammer repo root
cd ../..
```

`uv_setup.sh` installs uv, sets up the local libraries for the source builds (see the
psycopg2 note), creates the uv venv at Python 3.11, installs hammer editable, then Airflow
3.1.0 plus the real psycopg2, links `sledgehammer` into
`~/.local/bin`, and prompts to create the encrypted Postgres secrets.

### 4. techname plugin, into the uv venv (replaces the course's `pip install -e .`)

Init the submodule as usual, but install it into the **venv**, not conda. Do **not** use
the course's `pip install -e .` here (that targets conda's 3.10).

```bash
git submodule update --init --recursive vlsi/hammer-techname-plugin
uv pip install -e vlsi/hammer-techname-plugin \
  --python vlsi/hammer/.venv/bin/python --no-deps
```

`hammer` is a namespace package, so this makes `hammer.techname` importable under the venv
with no PYTHONPATH. Re-running `uv_setup.sh` also reinstalls it: the script installs any
populated `hammer-*-plugin` directory next to `vlsi/hammer`.

### 5. Activate both environments for `make`

You need this only for the course's `make` targets and `ee290-vlsi`, which run hammer
directly. The `sledgehammer` commands (see [Daily use](#daily-use)) need no activated venv.
Conda first, venv on top so `python3` is the hammer venv:

```bash
source env.sh                  # conda: 3.10 toolchain
source vlsi/hammer/venv.sh     # uv venv: python3 = hammer 3.11, BWRC tools, secrets (GPG prompt)
which python3                  # sanity check: .../vlsi/hammer/.venv/bin/python3
```

`venv.sh` finds its checkout from its own path, so you can source it from any directory: it
activates `vlsi/hammer/.venv` and sets `AIRFLOW_HOME` to `vlsi/hammer`. If `which python3`
shows the conda Python, you sourced in the wrong order, so re-run
`source vlsi/hammer/venv.sh` last.

`venv.sh` also drops conda entries from `LD_LIBRARY_PATH` and prints the ones it removed,
because conda's `libcrypto.so.3` breaks the system libldap that psycopg2 loads. Conda's
tools stay on `PATH`.

### 6. Smoke tests (same as the course)

```bash
# VCS RTL simulation (chipyard flow; conda is enough, but the layered env is fine too)
cd sims/vcs && make CONFIG=RocketConfig && cd ../..

# synthesis input verilog (needs the venv)
cd vlsi && make CONFIG=RocketConfig verilog

# synthesis (needs the venv)
make CONFIG=RocketConfig syn
```

The two `vlsi` targets are the course's `make` flow, so they need both environments from
step 5. Through SledgeHammer you register the DAG with `sledgehammer buildfile` and run syn
with `sledgehammer syn` instead, with no venv to activate (see [Daily use](#daily-use)).

PAR won't run on `RocketConfig` because the top-level IOs don't match. That's expected;
it's a smoke test, not a tapeout config.

## Tapeout configs

| Config | Use for |
|---|---|
| `EE290SimConfig` | block-level IP HW/SW dev. No full-chip peripherals, faster sim, supports LOADMEM. |
| `EE290TapeoutConfig` | the real VLSI runs. Full peripherals and physical cells. |
| `EE290IOConfig` | top-level PAR debugging (RDL, IO ring). Minimal, runs faster. |

For RTL integration, change `EE290SimConfig` so it propagates to the tapeout config but
not the IO config. (The smoke tests above use `RocketConfig`; real runs use the EE290 ones.)

## Updating the techname plugin

The plugin is under active development. The TAs will ask you to pull it, and it's the
first thing to try on tool errors during syn/PAR. For SledgeHammer, refresh it and
re-install into the venv:

```bash
cd vlsi/hammer-techname-plugin
git checkout master && git pull
cd ../..
uv pip install -e vlsi/hammer-techname-plugin --python vlsi/hammer/.venv/bin/python --no-deps
```

## psycopg2 needs pg_config (it's not a pip-vs-uv thing)

`psycopg2` (the source build, not `psycopg2-binary`) is a C extension that links libpq, so
it needs `pg_config` and the libpq headers. BWRC has runtime libpq but not `libpq-devel`,
and you have no root, so `pip install psycopg2` fails with "pg_config executable not found".
`uv pip install` would fail the same way. `uv_setup.sh` fixes it without root: it runs
`dnf download` on the `libpq-devel` and `libpq` RPMs, extracts them with `rpm2cpio | cpio`
into `~/pg_local`, and puts `pg_config` on PATH. The same pattern provides `libnsl` (Cadence
on RHEL 9). Let `uv_setup.sh` handle it. Don't fall back
to `psycopg2-binary`, whose bundled libpq/libssl clashes with the system's.

By-hand ordering, if you ever need it: `uv sync` does not install psycopg2 and removes
anything outside the lockfile, so build the real psycopg2 after `uv sync` and Airflow, with
`pg_config` on PATH and `--no-binary psycopg2 --reinstall`. Don't run
`dnf download --resolve libpq-devel` (it skips libpq and leaves a dangling `libpq.so`, so the
link fails with `cannot find -lpq`).

## Don't merge the two Pythons

This is why we skip the course's pip-into-conda steps. Installing hammer into the conda env
would target Python 3.10 and bump conda's pydantic to satisfy hammer, which risks breaking
other Chipyard tooling. Hammer and the plugin stay in the uv venv (3.11); conda only
supplies the tool binaries. Running `ee290-vlsi` under conda's Python gives `No module named
'hammer.vlsi'`, so the venv has to be active when you run the `make` targets yourself.
`sledgehammer buildfile` puts it first on PATH for you.

## Daily use

```bash
cd <chipyard>
source env.sh                                     # conda toolchain for the make below
cd vlsi
sledgehammer buildfile CONFIG=EE290TapeoutConfig  # runs make buildfile, registers the DAG
sledgehammer syn                                  # then sledgehammer par
```

`sledgehammer buildfile` passes `CONFIG=` and any other `VAR=val` arguments to make, then
prints each registered DAG that comes from `vlsi/` and the command to run next.
`sledgehammer syn` triggers the DAG on the running stack, so start the stack first with a
bare `sledgehammer` in another shell (it stays in the foreground). Run from `vlsi/`, it
finds the DAG generated there with no flags, and it loads the DB secrets itself, so it may
ask for your GPG passphrase.

Re-source `env.sh` in every new shell. For the course's `make` targets instead
(`make CONFIG=EE290TapeoutConfig syn`), also source `vlsi/hammer/venv.sh` after it, as in
step 5.

## Useful tips

- Pass `OBJ_DIR=<build_dir>` to `sledgehammer buildfile` for separate build dirs under
  `vlsi/` (parallel runs). Each build dir registers its own DAG, named after its basename,
  so give them different basenames. Two build dirs with the same basename share one DAG,
  only the one generated last is registered, and a run with `--obj_dir` set to the other one
  refuses until you regenerate the DAG from it.
- Once more than one DAG comes from `vlsi/` (a second `CONFIG` or `OBJ_DIR`), a bare
  `sledgehammer syn` stops and asks for `-t <name>`. The name is the build dir's basename,
  which `sledgehammer buildfile` prints as `registered sledgehammer_<name>_<user>`.
- `LOADMEM=1` loads the simulation binary faster (with `EE290SimConfig`).

## Gotchas / troubleshooting

- **`No module named 'hammer.vlsi'`** from a `make` target you ran yourself: venv not
  active, or conda's `python3` is shadowing. Re-source `venv.sh` last and check
  `which python3`.
- **`No module named 'hammer.techname'`**: plugin not installed in the venv (step 4), or a
  rebuild dropped it. Re-run the step-4 `uv pip install -e`.
- **syn hangs at "global incremental optimization"**: genus super-threading wedges at high
  thread counts. `design.yml` sets `vlsi.core.max_threads: 8` for this; drop to 1 if needed.
- **`sledgehammer buildfile` says the build registered no DAG for this directory**: usually
  the build wrote `hammer.d` but no `hammer_dag.py`. It needs `vlsi.core.build_system:
  sledgehammer` to win the config merge (it loads after `tools.yml`). With that key it writes
  both files. If an earlier build already wrote the obj_dir's `hammer.d`, delete it before
  you rerun, or make can skip the rule and regenerate nothing. If you set
  `vlsi.core.airflow_dags_folder`, it must be the folder Airflow reads. When another of your
  DAGs already comes from `vlsi/`, it lists that DAG as `registered` instead of printing
  this message, so check the name.
- **Never run a bare `uv sync`** in the hammer venv: it resets to the lockfile and wipes the
  Airflow stack (installed outside the lock). Refresh the editable install with
  `uv pip install -e . --force-reinstall --no-deps`, or restore Airflow via `uv_setup.sh`.
- **Secrets** live GPG-encrypted in `<hammer>/.sledgehammer/airflow-secrets.env.gpg`.
  `venv.sh` decrypts and loads them on source, and the `sledgehammer` flow commands decrypt
  them themselves when the environment lacks them. `airflow.cfg` ships with a blank
  connection.
- **The PD cache** lives in Postgres (`sledgehammer_studio.hammer_poc`), separate from the
  build dir and the Airflow metadata DB. Clearing the build dir or deleting DAG runs does
  not touch cached blobs.

---

**Environments:** Chipyard conda Python 3.10.14, SledgeHammer uv venv Python 3.11.15,
Airflow 3.1.0, uv 0.11.x.
**Updated:** 2026-10-13.
