# Setting up SledgeHammer in a workspace

Quick reference for dropping SledgeHammer into any Hammer workspace (gcd-vlsi,
a chipyard tree, etc.). Run from the workspace root.

```bash
# 1. Populate any tech/PDK plugins you want (they are git submodules; empty
#    until initialized). Skip the ones you do not need.
git submodule update --init <plugin-name>        # e.g. hammer-techname-plugin

# 2. Put the SledgeHammer hammer fork in hammer/. If hammer/ already exists,
#    check it first and stop if the check prints anything (uncommitted work).
#    The old hammer/ is moved aside, not deleted. For SSH, clone
#    git@github.com:lawrencejrhee/hammer.git instead.
git -C hammer status --porcelain .                  # only if hammer/ exists
git clone https://github.com/lawrencejrhee/hammer.git hammer.new
[ -d hammer ] && mv hammer hammer.old.$(date +%s)   # keep it, never rm -rf
mv hammer.new hammer

# 3. Build it: one command, no manual pip
./hammer/scripts/uv_setup.sh

# 4. Use it (open a new shell first). The stack stays in the foreground, so
#    run the lines after it in a second shell, from the workspace root.
sledgehammer                             # start the stack, from any directory
cd <vlsi dir> && sledgehammer buildfile  # registers the DAG (see the notes)
sledgehammer syn                         # from anywhere else, add -t <design>
```

Notes:

- `uv_setup.sh` creates `hammer/.venv`, installs hammer + its deps, and
  auto-installs any `hammer-*-plugin*` sibling that is populated (has a
  `pyproject.toml` or `setup.py`). Empty/uninitialized submodules are
  skipped. That is why step 1 comes before step 3.
- It sanitizes the build environment first (strips conda/mamba/venv/LD_PRELOAD),
  so it runs fine with `(base)` active and won't produce a conda-tainted
  psycopg2.
- Skip plugin installs entirely with `SLEDGE_NO_PLUGINS=1 ./hammer/scripts/uv_setup.sh`.
- The DRC/LVS legs need the private mentor plugin (`hammer-mentor-plugins`)
  only when the tool config picks Calibre (`hammer.drc.calibre`,
  `hammer.lvs.calibre`). Pegasus, ICV, KLayout, Magic, OpenROAD and Netgen
  ship in hammer itself, and syn/par never need it.
- Step 2 leaves the old checkout in `hammer.old.<timestamp>`. Its gitignored
  files never show in the status check and stay there, `.sledgehammer/` (your
  encrypted secrets) included. Copy it over before step 3, or `uv_setup.sh`
  prompts for new secrets: `cp -a hammer.old.<timestamp>/.sledgehammer hammer/`.
- `sledgehammer buildfile` runs `make buildfile` with the venv's Python first
  on `PATH`, so you do not activate the venv. Where the Makefile has a `build`
  target and no `buildfile` target, as in Hammer's own `e2e/`, it runs
  `make build` for you. Other arguments (`VAR=value`, `-j 4`) go to make.
- The build registers a DAG only when the project config sets
  `vlsi.core.build_system` to `sledgehammer` (or its alias `airflow`). With
  `make` it writes only `hammer.d`, and `sledgehammer buildfile` reports that
  the build registered no DAG for this directory (unless a DAG from an earlier
  build there is still registered, which it lists instead).
- With no `-t`, `sledgehammer syn` runs the DAG that builds under, or was
  generated from, the current directory. When more than one DAG belongs to it
  (two configs built from one vlsi dir), it stops and asks for `-t`.
- `-t` takes the name the DAG was registered under: the obj_dir basename, which
  is not always the top module. In Chipyard it is a long name such as
  `chipyard.harness.TestHarness.RocketConfig-ChipTop`. `sledgehammer buildfile`
  prints it as `registered sledgehammer_<name>_<user>`. A `-t` name with no
  registered DAG stops with "no DAG registered for <name>".
- Builds whose obj_dirs share a basename (`build-sky130-cm/gcd` and
  `build-asap7-cm/gcd`) share one DAG id, and only the one generated last is
  registered. The build prints a NOTE when it takes the id over from the other.
