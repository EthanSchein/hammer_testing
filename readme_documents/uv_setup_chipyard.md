> **HISTORICAL (March 2026 manual walkthrough).** This is the original by-hand Chipyard
> setup, kept for reference. Parts of it no longer match the code (DAGs are now generated
> per design, and the stack starts with `sledgehammer`). To set up Chipyard today, follow
> [setup-sledgehammer-chipyard.md](setup-sledgehammer-chipyard.md), which runs
> `./scripts/uv_setup.sh`, and see [CLI_REFERENCE.md](CLI_REFERENCE.md) for the commands.

# Part 2: Adding Chipyard to Your Hammer + Airflow + PostgreSQL Setup

> **Prerequisite**: Complete **all of [UV_SETUP.md](UV_SETUP.md)** first (Parts 1 through Step 7). You should have:
> - Hammer cloned and installed with uv
> - A working virtual environment (`.venv/`) with `hammer-vlsi`, `psycopg2`, and Airflow 3.1.0 (today `./scripts/uv_setup.sh` also installs `asyncpg` and the edge3 Airflow provider)
> - A PostgreSQL database created and connected
> - `airflow standalone` running successfully on `http://localhost:8080` (today you start the stack with `sledgehammer` instead)
>
> This document adds Chipyard on top of that and configures the Sledgehammer DAGs so you can run real VLSI flows (GCD, RocketTile) through the Airflow UI.

## Table of Contents

- [Overview](#overview)
- [Step 1: Clone Chipyard](#step-1-clone-chipyard)
- [Step 2: Chipyard Build Setup (Optional)](#step-2-chipyard-build-setup-optional)
  - [2.1 Install Miniforge](#21-install-miniforge-required-for-build-setupsh)
  - [2.2 Activate Conda](#22-activate-conda)
  - [2.3 Run Chipyard Build Setup](#23-run-chipyard-build-setup)
  - [2.4 Install Hammer PDK Plugins (Optional)](#24-install-hammer-pdk-plugins-optional)
  - [2.5 When You Don't Need Any of This](#25-when-you-dont-need-any-of-this)
  - [2.6 After Build: Go Back to the uv Virtual Environment](#26-after-build-go-back-to-the-uv-virtual-environment)
- [Step 3: Modify Hammer Source Files for Airflow Compatibility](#step-3-modify-hammer-source-files-for-airflow-compatibility)
  - [3.1 cli_driver.py: Leave sys.exit() in Place](#31-cli_driverpy-leave-sysexit-in-place)
  - [3.2 hammer_vlsi.py: Replace with Sledgehammer DAG Definitions](#32-hammer_vlsipy-replace-with-sledgehammer-dag-definitions)
  - [3.3 genus/__init__.py: Comment Out QRC Tech File](#33-genusinitpy-comment-out-qrc-tech-file)
- [Step 4: Update e2e Config Files with Absolute Paths](#step-4-update-e2e-config-files-with-absolute-paths)
  - [4.1 common.yml](#41-commonyml)
  - [4.2 sim-rtl.yml](#42-sim-rtlyml)
  - [4.3 Create syn.yml](#43-create-synyml)
  - [4.4 Create par.yml](#44-create-paryml)
- [Step 5: Install libnsl.so.1 Locally (RHEL 9, Required for Cadence Tools)](#step-5-install-libnsl-locally-rhel-9-required-for-cadence-tools)
- [Step 6: Update Airflow Configuration for DAGs](#step-6-update-airflow-configuration-for-dags)
  - [6.1 Point dags_folder to hammer/shell](#61-point-dags_folder-to-hammershell)
  - [6.2 Add Security Keys (CRITICAL)](#62-add-security-keys-critical)
  - [6.3 JWT Secret for API Authentication (ALSO CRITICAL)](#63-jwt-secret-for-api-authentication-also-critical)
  - [6.4 Verify Configuration](#64-verify-configuration)
  - [6.5 Run DB Migrate](#65-run-db-migrate)
- [Step 7: Verify DAG Discovery](#step-7-verify-dag-discovery)
- [Available DAGs](#available-dags)
- [Usage](#usage)
- [Relaunching (After Initial Setup)](#relaunching-after-initial-setup)
- [Clearing / Resetting a DAG Run](#clearing--resetting-a-dag-run)
- [Troubleshooting](#troubleshooting)
- [All Modifications Summary](#all-modifications-summary)
- [Directory Structure](#directory-structure)

---

## Overview

After completing UV_SETUP.md, your directory looks like this:

```
/bwrcq/home/<username>/hammer_uv/           # AIRFLOW_HOME
├── airflow.cfg                             # Airflow config (PostgreSQL, LocalExecutor)
├── pyproject.toml                          # PEP 621 (converted from Poetry for uv)
├── uv.lock
├── .venv/                                  # Virtual environment with Hammer + Airflow + psycopg2
├── venv.sh                                 # Convenience activation script
├── hammer/                                 # Hammer Python package
├── e2e/                                    # End-to-end test configs
└── dags/                                   # Empty placeholder (today: the dags folder)
```

We're going to:
1. **Clone Chipyard** into the hammer directory
2. **Modify 2 Hammer source files** so they work inside Airflow workers
3. **Create/update e2e config files** with absolute paths and synthesis/PAR configs
4. **Install libnsl.so.1 locally** (RHEL 9 requirement for Cadence tools)
5. **Update airflow.cfg** to point `dags_folder` at `hammer/shell/` and add security keys (today `airflow.cfg` sets neither, see [Step 6](#step-6-update-airflow-configuration-for-dags))
6. **Verify** the DAGs show up and can be triggered

---

## Step 1: Clone Chipyard

```bash
cd /bwrcq/home/<username>/hammer_uv
source venv.sh

git clone git@github.com:ucb-bar/chipyard.git chipyard
```

Add `chipyard/` to `.gitignore` (it's a separate git repository; today's `.gitignore` already lists it):

```bash
echo "chipyard/" >> .gitignore
```

---

## Step 2: Chipyard Build Setup (Optional)

Chipyard's `build-setup.sh` requires **conda** to set up its environment. Since the uv-based setup does not install conda, you need to install a local Miniforge first. This is a one-time step: conda is only needed for chipyard's build process, not for daily Airflow use.

### 2.1 Install Miniforge (Required for build-setup.sh)

```bash
cd /bwrcq/home/<username>/hammer_uv

wget -O Miniforge3.sh \
  "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"

bash Miniforge3.sh -b -p "$(pwd)/conda"

rm Miniforge3.sh
```

### 2.2 Activate Conda

```bash
cd /bwrcq/home/<username>/hammer_uv

eval "$($(pwd)/conda/bin/conda shell.bash hook)"
conda activate

conda --version
# Should show: conda 24.x.x
```

> **Important**: Make sure all conda-related settings in `~/.bashrc` are **commented out** before this step.

### 2.3 Run Chipyard Build Setup

```bash
cd /bwrcq/home/<username>/hammer_uv/chipyard

source /tools/C/ee290-sp25/bwrc-env.sh

# Skip FireSim steps 6-9 (not needed for VLSI flows)
./build-setup.sh riscv-tools -s 6 -s 7 -s 8 -s 9
```

**Total time**: 30-60 minutes depending on network and CPU.

### 2.4 Install Hammer PDK Plugins (Optional)

If you need commercial PDK support (e.g., for `Sledgehammer_demo_rocket` with techname):

```bash
cd /bwrcq/home/<username>/hammer_uv/chipyard/vlsi

git submodule add git@bwrcrepo.eecs.berkeley.edu:techname/hammer-techname-plugin.git
pip install -e hammer-techname-plugin

git submodule add git@bwrcrepo.eecs.berkeley.edu:techname/hammer-techname-plugin.git
pip install -e hammer-techname-plugin
```

### 2.5 When You Don't Need Any of This

If you **only** need the GCD demo (`Sledgehammer_demo_gcd`):
- **Skip Steps 2.1 to 2.4 entirely**
- The GCD DAG uses Hammer's built-in `e2e/` test configs with the Sky130 PDK
- It does not require a built chipyard, conda, or PDK plugins
- The chipyard clone from Step 1 is sufficient

### 2.6 After Build: Go Back to the uv Virtual Environment

```bash
conda deactivate
cd /bwrcq/home/<username>/hammer_uv
source venv.sh
```

**For daily use, you never need conda again.** The `source venv.sh` command is all you need.

> **Today**: you do not source `venv.sh` either. `sledgehammer` sources it when it starts the
> stack, and `sledgehammer buildfile` puts the venv's python first on `PATH` for the `make` it
> runs. A Chipyard design still needs conda: source Chipyard's `env.sh` in each new shell, then
> run `sledgehammer buildfile` from `chipyard/vlsi` (see
> [setup-sledgehammer-chipyard.md](setup-sledgehammer-chipyard.md)).

---

## Step 3: Modify Hammer Source Files for Airflow Compatibility

Two Hammer source files need modification. Without these changes, tasks will crash the Airflow worker process or fail to execute.

### 3.1 cli_driver.py: Leave sys.exit() in Place

Do not change the `sys.exit()` calls at the end of `CLIDriver.main()`. They give a failed stage a nonzero exit code, which make and the Airflow tasks rely on to stop the flow. Each DAG task runs its action in a separate process, so the exit cannot take down the Airflow worker.

### 3.2 hammer_vlsi.py: Replace with Sledgehammer DAG Definitions

> **Today**: skip this step. `hammer/shell/hammer_vlsi.py` is the `hammer-vlsi` entry point
> again, plus `run_cli_driver()`, the libnsl block and helpers that generated DAGs import. The
> two demo DAGs moved to `hammer/shell/airflow_example_dags.py`, and nothing imports them. Each
> design now gets its own DAG: with `vlsi.core.build_system: sledgehammer`, run
> `sledgehammer buildfile` from the vlsi directory (no venv to activate). Underneath it runs
> `make buildfile`, or `make build` in `e2e/`, whose Makefile has a `build` rule and no
> `buildfile` rule. The e2e configs set `vlsi.core.build_system: make`, so in `e2e/` put the
> `sledgehammer` value in a YAML file and run `sledgehammer buildfile extra=<file>` (see
> [CLI_REFERENCE.md](CLI_REFERENCE.md)). The build writes `hammer_dag.py` into the design's
> `OBJ_DIR` and links it into the dags folder as `sledgehammer_<design>_<user>.py`, where
> `<design>` is the last directory of `OBJ_DIR`, and `sledgehammer buildfile` then prints the
> DAGs registered for that directory. Two builds whose `OBJ_DIR`s end in the same name
> (`build-sky130-cm/gcd` and `build-asap7-cm/gcd`) share one DAG id, so only the one generated
> last is registered, and the build prints a NOTE when it takes the id over.

**File**: `hammer/shell/hammer_vlsi.py`

**Problem**: The stock file is a 9-line script that just calls `CLIDriver().main()`. We need to replace it entirely with the Sledgehammer DAG definitions.

The replacement file defines:

| Component | Purpose |
|-----------|---------|
| `LD_LIBRARY_PATH` setup | Module-level code that adds `~/libnsl_local/usr/lib64` to `LD_LIBRARY_PATH` so Cadence tools can find `libnsl.so.1`. Required because Airflow worker subprocesses don't inherit the parent shell's environment. |
| `run_cli_driver()` | Safety wrapper: catches `SystemExit` from `CLIDriver().main()` so it doesn't kill the Airflow worker. Only re-raises for non-zero exit codes. |
| `get_param()` | Helper to read DAG params from `conf` (runtime override) or `params` (DAG defaults). Fixes the issue where triggering without explicit config causes all tasks to skip. |
| `AIRFlow` class | GCD demo config: absolute paths to `e2e/` configs, methods for `build()`, `sim_rtl()`, `syn()`, `par()`, `clean()`. |
| `AIRFlow_rocket` class | RocketTile demo config: same pattern, adds `sram_generator()` and `syn_to_par()` steps. |
| `Sledgehammer_demo_gcd` DAG | Branching task flow for GCD design. |
| `Sledgehammer_demo_rocket` DAG | Branching task flow for RocketTile design. |

The `LD_LIBRARY_PATH` block at the top of the file (right after `import json`) looks like:

```python
# RHEL 9 workaround: Cadence tools (Genus, Innovus) need libnsl.so.1
_libnsl_path = os.path.expanduser("~/libnsl_local/usr/lib64")
if os.path.isfile(os.path.join(_libnsl_path, "libnsl.so.1")):
    _ld = os.environ.get("LD_LIBRARY_PATH", "")
    if _libnsl_path not in _ld:
        os.environ["LD_LIBRARY_PATH"] = f"{_libnsl_path}:{_ld}" if _ld else _libnsl_path
```

> **CRITICAL**: This must be in the DAG file itself, not just in `venv.sh`. Airflow spawns worker processes that do not inherit `LD_LIBRARY_PATH` from the parent shell. Without this, Genus and Innovus will fail with `libnsl.so.1: cannot open shared object file` even if `venv.sh` exports the variable.

**How to apply**: Copy the modified `hammer_vlsi.py` from the reference setup and update the absolute paths:

```bash
# If using a reference copy:
cp <a reference checkout>/hammer/shell/hammer_vlsi.py \
   /bwrcq/home/<username>/hammer_uv/hammer/shell/hammer_vlsi.py
```

Then update the absolute paths in the `AIRFlow` and `AIRFlow_rocket` classes:

```python
# In AIRFlow.__init__() (line ~66):
self.vlsi_dir = '/bwrcq/home/<username>/hammer_uv/e2e'   # ← your path

# In AIRFlow_rocket.__init__() (line ~507):
self.vlsi_dir = '/bwrcq/home/<username>/hammer_uv/e2e'   # ← your path
self.specs_abs = '/bwrcq/home/<username>/hammer_uv/specs' # ← your path
```

> **WARNING (run_cli_driver() recursion trap)**: The `run_cli_driver()` wrapper calls `CLIDriver().main()` internally. If you use find-and-replace-all to replace `CLIDriver().main()` with `run_cli_driver()` in the DAG task methods, do NOT also replace the one inside `run_cli_driver()` itself. That causes infinite recursion (`RecursionError: maximum recursion depth exceeded`).

#### Key design decisions in the DAG

- **`clean` is a pass-through**: Always flows to `build_decider` afterward, not a dead end.
- **`exit_` does not call `sys.exit(0)`**: Uses `trigger_rule=TriggerRule.NONE_FAILED` and just prints.
- **`sim_rtl` and `syn/par` are mutually exclusive**: The `sim_or_syn_decide` branch checks `sim_rtl` first. If `sim_rtl=True` (default), it takes the simulation path and skips synthesis/PAR entirely. To run syn/par, trigger with `{"sim_rtl": false}`.
- **All param defaults are `True`** (except `clean` which defaults to `False`), so triggering without config runs all enabled steps.

### 3.3 genus/__init__.py: Comment Out QRC Tech File

> **Today**: skip this step. The QRC block in `hammer/synthesis/genus/__init__.py` is active
> again, and it only sets `qrc_tech_file` when the technology supplies QRC files, so a PDK
> without them gets no QRC command.

**File**: `hammer/synthesis/genus/__init__.py` (around line 231)

**Problem**: The QRC tech file setting can cause Genus to fail if QRC files are not available for the PDK. For Sky130 (which has no QRC), this is harmless but unnecessary. Commenting it out prevents potential failures.

**Change** (comment out 3 lines):

```diff
-        if len(qrc_files) > 0:
-            verbose_append("set_db qrc_tech_file {{ {files} }}".format(
-                files=qrc_files[0]
-            ))
+        # Commented out QRC tech file setting: Sky130 does not have QRC files
+        # if len(qrc_files) > 0:
+        #     verbose_append("set_db qrc_tech_file {{ {files} }}".format(
+        #         files=qrc_files[0]
+        #     ))
```

> **Note**: This is a belt-and-suspenders measure. The `syn.yml` config also sets `use_qrc: false` and `qrc_files: []`, though the Genus plugin reads neither key. If you're using a PDK that supports QRC (e.g., commercial PDKs), you may need to re-enable these lines.

---

## Step 4: Update e2e Config Files with Absolute Paths

Airflow workers do not execute from the `e2e/` directory, so all source file references must use **absolute paths**. Additionally, the GCD demo needs `syn.yml` and `par.yml` config files that don't exist in stock Hammer.

> **Today**: skip this step. A generated DAG runs each stage from the directory you ran the
> build in, so relative paths resolve, and the gcd `common.yml` and `sim-rtl.yml` keep their
> relative paths. `syn.yml` and `par.yml` are already in `e2e/configs-design/gcd/`.

### 4.1 common.yml

**File**: `e2e/configs-design/gcd/common.yml`

Change the `input_files` from relative to absolute:

```diff
 synthesis.inputs:
   top_module: "gcd"
-  input_files: ["src/gcd.v"]
+  input_files:
+    - "/bwrcq/home/<username>/hammer_uv/e2e/src/gcd.v"
```

### 4.2 sim-rtl.yml

**File**: `e2e/configs-design/gcd/sim-rtl.yml`

```diff
 sim.inputs:
   level: "rtl"
-  input_files: ["src/gcd.v", "src/gcd_tb.v"]
+  input_files:
+    - "/bwrcq/home/<username>/hammer_uv/e2e/src/gcd.v"
+    - "/bwrcq/home/<username>/hammer_uv/e2e/src/gcd_tb.v"
```

### 4.3 Create syn.yml

**File**: `e2e/configs-design/gcd/syn.yml` (does not exist in stock Hammer, so create it)

This file defines synthesis constraints (clock period, input delays, Genus settings):

```yaml
# Specify Global Variables
clockPeriod: &CLK_PERIOD "20.0ns"
clockPeriodby5: &CLK_PERIOD_BY_5 "4.0" # used for pin delays, update accordingly
verilogSrc: &VERILOG_SRC
  - "/bwrcq/home/<username>/hammer_uv/e2e/src/gcd.v"


# Specify clock signals
vlsi.inputs.clocks: [
  {name: "clk", period: *CLK_PERIOD, uncertainty: "0.1ns"}
]

# Input delays match INPUT_DELAY parameter in riscv_test_harness.v
vlsi.inputs.delays: [
  {name: "mem*", clock: "clk", direction: "input", delay: *CLK_PERIOD_BY_5}
]

# Synthesis Constraints
synthesis.inputs:
  top_module: "gcd"
  input_files: *VERILOG_SRC

# Genus-specific settings
synthesis.genus:
  # Disable QRC
  use_qrc: false
  # Set empty QRC files to prevent the tool from looking for them
  qrc_files: []
  # Basic tool configuration
  generate_reports: true
  generate_final_netlist: true
  timing_driven: true
  clock_gating_mode: "empty"

# Technology settings
technology.sky130:
  synthesis_tool: "genus"
```

### 4.4 Create par.yml

**File**: `e2e/configs-design/gcd/par.yml` (does not exist in stock Hammer, so create it)

```yaml
# Place and Route configuration for GCD design
# PAR input is dynamically generated by syn_to_par() from synthesis outputs.
# This file contains any additional PAR-specific overrides.

# Placement Constraints (if needed beyond what's in sky130.yml)
# vlsi.inputs.placement_constraints: []
```

---

## Step 5: Install libnsl Locally (RHEL 9, Required for Cadence Tools)

RHEL 9 removed `libnsl.so.1`, which Cadence tools (Genus, Innovus) depend on. Without it, synthesis and PAR tasks fail with:

```
genus: error while loading shared libraries: libnsl.so.1: cannot open shared object file
```

Install the library locally (no root required):

```bash
# Download the libnsl RPM
mkdir -p /tmp/libnsl_build
cd /tmp/libnsl_build
dnf download libnsl

# Extract to a local directory
mkdir -p ~/libnsl_local
rpm2cpio libnsl-*x86_64.rpm | (cd ~/libnsl_local && cpio -idmv)

# Verify
ls ~/libnsl_local/usr/lib64/libnsl.so.1
# Should show the file

# Clean up
cd ~
rm -rf /tmp/libnsl_build
```

> **Note**: The Sky130 PDK files that ship with Hammer work as-is for the GCD demo. Do **NOT** copy Sky130 files from other reference setups: they may be from a different Hammer version with incompatible APIs (e.g., importing `TechJSON`, which this Hammer renamed to `TechConfig`).

---

## Step 6: Update Airflow Configuration for DAGs

### 6.1 Point dags_folder to hammer/shell

Edit `airflow.cfg`:

```ini
[core]
dags_folder = /bwrcq/home/<username>/hammer_uv/hammer/shell
```

> **This is the #1 reason DAGs don't show up.** If `dags_folder` points to the wrong directory, `airflow dags list` returns nothing. Verify with: `airflow config get-value core dags_folder`

> **Today**: skip this. The committed `airflow.cfg` leaves `dags_folder` unset, so Airflow
> uses `$AIRFLOW_HOME/dags`, and `sledgehammer` pins `AIRFLOW_HOME` to the checkout.
> `sledgehammer buildfile` links each generated DAG into that `dags/` folder by default, and
> `sledgehammer run` looks for it in the same place.

### 6.2 Add Security Keys (CRITICAL)

> **Today**: skip 6.2 and 6.3. The committed `airflow.cfg` ships these keys and the database
> connection blank. `./scripts/uv_setup.sh` offers to run `./scripts/sledge-secrets-create.sh`,
> which generates the four keys into the GPG-encrypted `.sledgehammer/airflow-secrets.env.gpg`
> as `AIRFLOW__*` variables, and `sledgehammer` loads that file before it starts Airflow.

Without these, the scheduler cannot communicate with the API server and **all tasks will fail** with:

```
ServerResponseError: Invalid auth token: Signature verification failed
```

Generate keys:

```bash
# Generate Fernet key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Generate secret key
python -c "import secrets; import base64; print(base64.b64encode(secrets.token_bytes(16)).decode())"
```

Set them in `airflow.cfg`:

```ini
[core]
fernet_key = <your_generated_fernet_key>
internal_api_secret_key = <your_generated_secret_key>

[api]
secret_key = <same_secret_key_as_internal_api_secret_key>
```

> **IMPORTANT**: `internal_api_secret_key` and `secret_key` **must be the same value**.

### 6.3 JWT Secret for API Authentication (ALSO CRITICAL)

Airflow 3.x uses JWT tokens for internal component authentication (scheduler <-> API server <-> executor). Even if the other keys are set, tasks will fail with `Invalid auth token` if `jwt_secret` is missing.

```bash
python -c "import secrets; import base64; print(base64.b64encode(secrets.token_bytes(16)).decode())"
```

Add this section to `airflow.cfg`:

```ini
[api_auth]
jwt_secret = <your_generated_jwt_secret>
jwt_expiration_time = 86400
jwt_cli_expiration_time = 3600
```

> **Why a separate key?** The `secret_key` handles session cookies/CSRF. The `jwt_secret` specifically signs JWT tokens exchanged between internal components. If it's missing, Airflow generates a random one per process, so the API server rejects the scheduler's tokens.

### 6.4 Verify Configuration

```bash
source venv.sh

airflow config get-value core dags_folder
# Should show: /bwrcq/home/<username>/hammer_uv/hammer/shell (today: <checkout>/dags)

airflow config get-value database sql_alchemy_conn
airflow config get-value core executor
# Should show: LocalExecutor

airflow config get-value core fernet_key
airflow config get-value api base_url
```

### 6.5 Run DB Migrate

```bash
source venv.sh
airflow db migrate
```

In Airflow 3.x, `airflow db init` was removed entirely. Use `airflow db migrate` instead: it's idempotent and safe to run any number of times.

---

## Step 7: Verify DAG Discovery

```bash
source venv.sh

# Reserialize DAGs (forces Airflow to re-scan the dags_folder)
airflow dags reserialize

# List DAGs and look for the Sledgehammer DAGs
airflow dags list | grep Sledgehammer
# Should show:
# Sledgehammer_demo_gcd
# Sledgehammer_demo_rocket

# Check for import errors (should be empty)
airflow dags list-import-errors
# Should show: No data found
```

**If DAGs don't appear:**

1. **Verify `dags_folder`**: `airflow config get-value core dags_folder` must point to `hammer/shell/` (today `dags/` in the checkout)
2. **Check import errors**: `airflow dags list-import-errors`
3. **Test the import manually**: `python3 hammer/shell/hammer_vlsi.py`
4. **Verify Hammer is importable**: `python3 -c "from hammer.vlsi import CLIDriver; print('OK')"`
5. **Verify editable install**: `hammer-shell-test` should print "hammer-shell appears to be on the path"

Now start Airflow:

```bash
source venv.sh
airflow standalone
```

> **Today**: start the stack with `sledgehammer` instead. It pins `AIRFLOW_HOME` to the
> checkout, loads the encrypted secrets and starts Airflow, with you as its one login. The two
> demo DAGs no longer load: `sledgehammer dags list` shows your generated
> `sledgehammer_<design>_<user>` DAGs.

---

## Available DAGs

> **Today**: Airflow does not load these two demo DAGs. They live in
> `hammer/shell/airflow_example_dags.py`, outside the dags folder (see
> [3.2](#32-hammer_vlsipy-replace-with-sledgehammer-dag-definitions)). Their params have changed
> too: that file has more stages, and every param defaults to `False`.

### Sledgehammer_demo_gcd

GCD (Greatest Common Divisor) design workflow.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `clean` | boolean | `False` | Clean the build directory |
| `build` | boolean | `True` | Run the build step |
| `sim_rtl` | boolean | `True` | Run RTL simulation |
| `syn` | boolean | `True` | Run logic synthesis |
| `par` | boolean | `True` | Run place and route |

**Default config**: design=`gcd`, PDK=`sky130`, tools=`cm`, env=`bwrc`

### Sledgehammer_demo_rocket

RocketTile design workflow (includes SRAM generation step).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `clean` | boolean | `False` | Clean the build directory |
| `build` | boolean | `True` | Run the build step |
| `sim_rtl` | boolean | `True` | Run RTL simulation |
| `sram_generator` | boolean | `True` | Generate SRAM macros |
| `syn` | boolean | `True` | Run logic synthesis |
| `par` | boolean | `True` | Run place and route |

**Default config**: design=`example`, PDK=`sky130`, tools=`cm`, env=`example`. Override with the `design`/`pdk`/`tools`/`env` environment variables, set to your site's names.

### DAG Task Flows

**GCD:**

```
start → clean → build_decider ──→ build → sim_or_syn_decide ──→ sim_rtl → exit_
                     │                            │
                     │                            └──→ syn_decider → syn → par_decider → par → exit_
                     │
                     └──→ exit_ (if no steps enabled)
```

**Rocket** (adds SRAM + syn-to-par steps):

```
start → clean → build_decider ──→ build → sim_or_syn_decide ──→ sim_rtl → exit_
                     │                            │
                     │                            └──→ sram_decider → sram_generator → syn_decider
                     │                                                    │
                     │                                    syn_decider → syn → par_decider → syn_to_par → par → exit_
                     │
                     └──→ exit_ (if no steps enabled)
```

### IMPORTANT: sim_rtl and syn/par are mutually exclusive

The `sim_or_syn_decide` branch checks `sim_rtl` **first**. If `sim_rtl=True` (default), it takes the simulation path and **skips** synthesis/PAR. To run synthesis and PAR:

```bash
airflow dags trigger Sledgehammer_demo_gcd --conf '{"sim_rtl": false}'
```

---

## Usage

> **Today**: run a generated DAG's stages with `sledgehammer` instead. From the vlsi directory
> you ran `sledgehammer buildfile` in, `sledgehammer syn par` triggers those stages and streams
> the run; add `-t <design>` to run from any directory, or when more than one of your DAGs comes
> from that directory. `sledgehammer status --design <design>` shows the latest run. See
> [CLI_REFERENCE.md](CLI_REFERENCE.md).

### Triggering DAGs via CLI

```bash
source venv.sh

# GCD: Full default flow (build + sim_rtl)
airflow dags trigger Sledgehammer_demo_gcd

# GCD: Build + Synthesis + PAR (skip sim)
airflow dags trigger Sledgehammer_demo_gcd --conf '{"sim_rtl": false}'

# GCD: Build only
airflow dags trigger Sledgehammer_demo_gcd --conf '{"sim_rtl": false, "syn": false, "par": false}'

# GCD: Full flow with clean
airflow dags trigger Sledgehammer_demo_gcd --conf '{"clean": true}'

# Rocket: Build + SRAM + Synthesis
airflow dags trigger Sledgehammer_demo_rocket --conf '{"sim_rtl": false}'
```

### Triggering via Web UI

1. Open browser to `http://localhost:8080` (today `sledgehammer` prints the URL, port 8081 by default)
2. Find your DAG in the list
3. Unpause the DAG (toggle the switch)
4. Click the **Play** button (▶)
5. (Optional) Enter JSON config: `{"sim_rtl": false, "syn": true, "par": true}`
6. Click "Trigger"

### Checking Status

```bash
source venv.sh

airflow dags list
airflow dags list-import-errors
airflow tasks states-for-dag-run Sledgehammer_demo_gcd "<run_id>"
```

---

## Relaunching (After Initial Setup)

```bash
cd /bwrcq/home/<username>/hammer_uv
source venv.sh
airflow standalone

# Password in: simple_auth_manager_passwords.json.generated
```

> **Today**: run `sledgehammer` instead, from any directory, and log in as yourself with the
> password in `.sledgehammer/airflow-ui-password.json`. See [CLI_REFERENCE.md](CLI_REFERENCE.md).

---

## Clearing / Resetting a DAG Run

### Clear all runs for a DAG

```bash
source venv.sh
airflow tasks clear Sledgehammer_demo_gcd --yes
```

### Delete all DAG run history (fresh start)

```bash
airflow dags delete Sledgehammer_demo_gcd --yes
# DAG re-imports automatically on next scheduler scan
```

### Via Web UI

1. Go to the DAG's Grid view
2. Click on the specific DAG run
3. Click "Clear" to reset all tasks in that run

---

## Troubleshooting

### DAGs not appearing

**Symptom**: `airflow dags list | grep Sledgehammer` returns nothing.

**Fix**:
```bash
# 1. Check where Airflow is looking
airflow config get-value core dags_folder
# Must be: /bwrcq/home/<username>/hammer_uv/hammer/shell (today: <checkout>/dags)

# 2. Check for import errors
airflow dags list-import-errors

# 3. Test import manually
python3 hammer/shell/hammer_vlsi.py

# 4. Verify hammer is importable
python3 -c "from hammer.vlsi import CLIDriver; print('OK')"

# 5. Force re-scan
airflow dags reserialize
```

### Tasks fail with "Invalid auth token: Signature verification failed"

Check these 4 keys in `airflow.cfg` (in order; today they live in the secrets file, see [6.2](#62-add-security-keys-critical)):
1. `[core] fernet_key`: set?
2. `[core] internal_api_secret_key`: set?
3. `[api] secret_key`: **matches** `internal_api_secret_key`?
4. `[api_auth] jwt_secret`: set? (most commonly missed)

### Tasks fail with "No module named 'hammer'"

The editable install `.pth` file is stale:

```bash
cd /bwrcq/home/<username>/hammer_uv
uv pip install -e .
hammer-shell-test
# Should print: "hammer-shell appears to be on the path"
```

### Tasks crash with RecursionError

The `run_cli_driver()` wrapper is calling itself instead of `CLIDriver().main()`. Ensure the wrapper function body calls `CLIDriver().main()`, NOT `run_cli_driver()`.

### Tasks always skip (only start, clean, exit_ run)

1. **Param defaults are `False`**: change to `True` in the DAG definition
2. **Code reads `dag_run.conf.get()` directly**: use `get_param()` helper instead
3. **`clean` routes to `exit_`**: restructure so `clean` flows to `build_decider`

### syn/par tasks skipped even though set to True

**Expected behavior** in the March demo DAGs. `sim_or_syn_decide` treats `sim_rtl` and `syn/par` as mutually exclusive (a generated DAG has no such branch and runs every stage you select). Trigger with `{"sim_rtl": false}`.

### genus: error while loading shared libraries: libnsl.so.1

**Cause**: RHEL 9 removed `libnsl.so.1`. Cadence Genus and Innovus depend on it.

**Fix**: Install `libnsl.so.1` locally (see [Step 5](#step-5-install-libnsl-locally-rhel-9-required-for-cadence-tools)) AND ensure the `LD_LIBRARY_PATH` is set in `hammer_vlsi.py` (see [Step 3.2](#32-hammer_vlsipy-replace-with-sledgehammer-dag-definitions)).

Setting `LD_LIBRARY_PATH` only in `venv.sh` or the parent shell is **not enough**: Airflow worker subprocesses don't inherit it. The path must be set in the DAG file itself.

> **Today**: only Step 5 is needed. `sledgehammer` sources `venv.sh`, which adds the libnsl
> path, before it starts Airflow, and each generated DAG imports `hammer_vlsi.py`, which still
> sets it, before it runs a stage.

### airflow db init doesn't work

Airflow 3.x removed `db init`. Use `airflow db migrate` instead.

### Port already in use

```bash
ss -tlnp | grep <port>
kill -9 <PID>
# Or pick a different port and update airflow.cfg [api] base_url
```

> **Today**: `sledgehammer` takes the `[api] port` from `airflow.cfg` (8081) and moves to the
> next free port when that one is busy. Set `SLEDGE_PORT=<n>` to require one port.

---

## All Modifications Summary

### Files Modified from Stock Hammer

| # | File | Change | Why |
|---|------|--------|-----|
| 1 | `hammer/shell/hammer_vlsi.py` | Replaced 9-line script with ~1090-line Sledgehammer DAG definitions including `LD_LIBRARY_PATH` setup for libnsl | Airflow DAG file with libnsl workaround, `run_cli_driver()` wrapper, `get_param()` helper, `AIRFlow`/`AIRFlow_rocket` classes, two DAG definitions |
| 2 | `hammer/synthesis/genus/__init__.py` | Commented out 3 lines (~231-233): QRC tech file setting | Workaround for QRC loading issue on RHEL 9 |
| 3 | `e2e/configs-design/gcd/common.yml` | `"src/gcd.v"` → absolute path | Airflow workers don't run from `e2e/` |
| 4 | `e2e/configs-design/gcd/sim-rtl.yml` | `"src/gcd.v"`, `"src/gcd_tb.v"` → absolute paths | Airflow workers don't run from `e2e/` |

### Files Created (not in stock Hammer)

| # | File | Purpose |
|---|------|---------|
| 1 | `e2e/configs-design/gcd/syn.yml` | Synthesis constraints: clock period (20ns), input delays, Genus settings, QRC disabled |
| 2 | `e2e/configs-design/gcd/par.yml` | PAR config placeholder (PAR input is generated dynamically by `syn_to_par()`) |
| 3 | `pyproject.toml` | Converted from Poetry to PEP 621 for uv (done in UV_SETUP.md) |
| 4 | `airflow.cfg` | dags_folder, security keys, PostgreSQL connection (done in UV_SETUP.md + Step 6). Today it is committed with `dags_folder` unset and the keys and connection blank (see [Step 6](#step-6-update-airflow-configuration-for-dags)) |
| 5 | `.gitignore` | Added chipyard/, airflow.cfg, logs/, wheels/, .venv/ (today `airflow.cfg` is tracked, not ignored) |
| 6 | `venv.sh` | Convenience activation script with libnsl `LD_LIBRARY_PATH` (done in UV_SETUP.md + Step 5) |

### Files NOT Modified by This Guide

| File | Note |
|------|------|
| `hammer/technology/sky130/__init__.py` | Sky130 PDK plugin, changed by the sky130 port (September 2026): do NOT replace with copies from other setups |
| `hammer/technology/sky130/sram-cache.json` | SRAM cache, changed by the sky130 port: do NOT replace |
| `hammer/technology/sky130/sram_compiler/__init__.py` | SRAM compiler, changed by the sky130 port: do NOT replace |
| `e2e/configs-design/gcd/sky130.yml` | PDK-specific design config (now differs from stock Hammer) |
| `e2e/configs-env/bwrc-env.yml` | BWRC environment config (now differs from stock Hammer) |
| `e2e/configs-pdk/sky130.yml` | Sky130 PDK config |
| `e2e/configs-tool/cm.yml` | Commercial tools config (now differs from stock Hammer) |

### External Dependencies Installed (not in the repo)

| Item | Location | Purpose |
|------|----------|---------|
| `libnsl.so.1` | `~/libnsl_local/usr/lib64/` | RHEL 9 workaround: Cadence tools need this library. Extracted from RPM without root access. |

---

## Directory Structure

After completing both UV_SETUP.md and this guide:

```
hammer_uv/                                # AIRFLOW_HOME
├── airflow.cfg                           # Airflow configuration
├── pyproject.toml                        # PEP 621 project config (for uv)
├── uv.lock                              # uv lockfile
├── .venv/                               # Virtual environment
├── venv.sh                              # Convenience activation script
├── chipyard/                            # Chipyard repository (cloned in Step 1)
├── logs/                                # Airflow task logs
├── simple_auth_manager_passwords...     # Auto-generated admin password
├── hammer/                              # Hammer Python package
│   ├── shell/
│   │   └── hammer_vlsi.py              # ★ DAG definitions (Sledgehammer_demo_gcd, _rocket; now in airflow_example_dags.py)
│   ├── vlsi/
│   │   └── cli_driver.py              # CLIDriver
│   ├── synthesis/
│   │   └── genus/
│   │       └── __init__.py            # ★ Genus plugin (modified: QRC workaround)
│   └── technology/
│       └── sky130/                    # Sky130 PDK (now changed by the sky130 port)
│           ├── __init__.py
│           ├── sram-cache.json
│           └── sram_compiler/
│               └── __init__.py
└── e2e/                                 # End-to-end configs
    ├── configs-env/
    │   └── bwrc-env.yml
    ├── configs-pdk/
    │   └── sky130.yml
    ├── configs-tool/
    │   └── cm.yml
    ├── configs-design/
    │   └── gcd/
    │       ├── common.yml             # ★ Design config (absolute paths)
    │       ├── sim-rtl.yml            # ★ Simulation config (absolute paths)
    │       ├── syn.yml                # ★ Synthesis config (CREATED, not in stock)
    │       ├── par.yml                # ★ PAR config (CREATED, not in stock)
    │       └── sky130.yml
    └── src/
        ├── gcd.v                      # GCD design source
        └── gcd_tb.v                   # GCD testbench
```

Files marked with ★ were modified or created from the stock Hammer repository.

> **IMPORTANT**: Do NOT copy Sky130 PDK files (`__init__.py`, `sram-cache.json`, `sram_compiler/__init__.py`) from other reference setups. They may import APIs (like `TechJSON`, which this Hammer renamed to `TechConfig`) that don't exist here, causing `ImportError` at runtime.

---

## Best Practices

1. **All 4 security keys**: Always set `fernet_key`, `internal_api_secret_key`, `secret_key`, AND `jwt_secret`
2. **Port consistency**: `[api] base_url` must match the actual port Airflow runs on (today `sledgehammer` sets it for you)
3. **Source BWRC env**: Always source the EDA tool environment before starting Airflow
4. **Set AIRFLOW_HOME**: Every terminal that runs `airflow` commands must have `AIRFLOW_HOME` set
5. **No `sys.exit()` in tasks**: Never use `sys.exit()` in Airflow task code. Always use `return` or raise exceptions
6. **Use absolute paths in YAML configs**: Airflow workers don't run from the `e2e/` directory (today relative paths work, see [Step 4](#step-4-update-e2e-config-files-with-absolute-paths))
7. **Understand `sim_rtl` vs `syn/par` exclusivity**: The DAG branches, so you can run sim OR syn/par, not both in one run (demo DAGs only; a generated DAG runs every stage you select)
8. **Use `airflow db migrate` freely**: It's idempotent and safe to run anytime
9. **Verify editable installs after moving repos**: Re-run `uv pip install -e .` if you move the directory
10. **Wrap `CLIDriver().main()`**: Always use the `run_cli_driver()` wrapper, never call `CLIDriver().main()` directly from task code

---

## References

- [UV_SETUP.md](UV_SETUP.md) (Part 1: Hammer + uv + Airflow + PostgreSQL installation)
- [Chipyard Documentation](https://chipyard.readthedocs.io/)
- [Hammer VLSI Documentation](https://hammer-vlsi.readthedocs.io/)
- [Apache Airflow 3.x Documentation](https://airflow.apache.org/docs/)
- [uv Documentation](https://docs.astral.sh/uv/)

---

**Author**: Lawrence Rhee
**Last Updated**: March 31, 2026
**Airflow Version**: 3.1.0
**uv Version**: 0.10.8+
**PostgreSQL Version**: 13.22 (on server)
**Python Version**: 3.11.9
