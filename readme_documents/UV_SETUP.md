> **HISTORICAL (March 2026 manual walkthrough).** This is the original by-hand setup, kept for reference. Parts of it no longer match the code: `sledgehammer buildfile` now generates one DAG per build directory name and user and links it into the dags folder Airflow reads; `airflow.cfg` is tracked in git with its secrets blank; and the stack starts with `sledgehammer`. Today `./scripts/uv_setup.sh` does the environment setup. For the current guides, see [WORKSPACE_SETUP.md](WORKSPACE_SETUP.md) and [CLI_REFERENCE.md](CLI_REFERENCE.md). The **Today** notes below mark the steps that changed most.

# Chipyard + SledgeHammer Setup with uv (PostgreSQL + Airflow)

This document provides a **complete from-scratch guide** for setting up **Chipyard** with **Hammer (Sledgehammer)** VLSI framework, orchestrated by **Apache Airflow 3.x** with a **PostgreSQL** database backend, all managed by **[uv](https://github.com/astral-sh/uv)** instead of Poetry/Conda.

**What this setup provides:**
- **Chipyard**: RISC-V SoC design framework and build system
- **Hammer (Sledgehammer)**: VLSI flow driver that runs synthesis, place-and-route, simulation, etc.
- **Airflow 3.x**: Orchestrates the VLSI flow steps as DAGs (Directed Acyclic Graphs)
- **PostgreSQL backend**: Production-grade database for Airflow enabling parallel execution
- **uv**: Fast, modern Python package manager replacing pip/poetry/conda for Hammer deps

**Why uv instead of Poetry/Conda?**
- **10-100x faster** than pip/poetry for dependency resolution and installation
- **Single tool**: replaces pip, poetry, virtualenv, and pyenv
- **Simple installation**: one `curl` command, no Python bootstrap needed
- **Standard format**: uses PEP 621 `pyproject.toml` (no proprietary `[tool.poetry]` format)
- **Source builds just work**: `--no-binary` flag compiles C extensions from source cleanly

## Table of Contents

- [Prerequisites](#prerequisites)
- [Part 1: Hammer + Airflow + PostgreSQL Setup](#part-1-hammer--airflow--postgresql-setup)
  - [Step 0: Install Python (If Not Already Installed)](#step-0-install-python-if-not-already-installed)
  - [Step 0.3: Set Up GitHub SSH Access](#step-03-set-up-github-ssh-access)
  - [Step 0.4: Create Working Directory](#step-04-create-working-directory)
  - [Step 0.5: Clone Hammer Repository](#step-05-clone-hammer-repository)
  - [Step 1: Install uv](#step-1-install-uv)
  - [Step 1.5: Set Up pg\_config for psycopg2](#step-15-set-up-pg_config-for-psycopg2)
  - [Step 2: Create Virtual Environment and Install Dependencies](#step-2-create-virtual-environment-and-install-dependencies)
  - [Step 3: Install Apache Airflow](#step-3-install-apache-airflow)
  - [Step 4: Set Up PostgreSQL Connection](#step-4-set-up-postgresql-connection)
  - [Step 5: Configure Airflow](#step-5-configure-airflow)
  - [Step 6: Initialize Database](#step-6-initialize-database)
  - [Step 7: Start Airflow](#step-7-start-airflow)
- [Part 2: Chipyard Integration](#part-2-chipyard-integration)
  - [Step 8: Clone Chipyard](#step-8-clone-chipyard)
  - [Step 9: Chipyard Build Setup (Optional)](#step-9-chipyard-build-setup-optional)
  - [Step 10: Modify Hammer Files for Airflow Compatibility](#step-10-modify-hammer-files-for-airflow-compatibility)
  - [Step 11: Install libnsl Locally (RHEL 9)](#step-11-install-libnsl-locally-rhel-9-required-for-cadence-tools)
  - [Step 12: Update Airflow Configuration for DAGs](#step-12-update-airflow-configuration-for-dags)
  - [Step 13: Verify DAG Discovery](#step-13-verify-dag-discovery)
- [Available DAGs](#available-dags)
- [Usage](#usage)
- [Daily Usage](#daily-usage)
- [Relaunching (After Initial Setup)](#relaunching-after-initial-setup)
- [Clearing / Resetting a DAG Run](#clearing--resetting-a-dag-run)
- [uv Cheat Sheet (vs Poetry)](#uv-cheat-sheet-vs-poetry)
- [Verification](#verification)
- [Troubleshooting](#troubleshooting)
- [All Modifications Summary](#all-modifications-summary)
- [Directory Structure](#directory-structure)

---

## Prerequisites

1. **SSH Access to BWRC Machines**:
   - **Login Server**: `bwrcrdsl-1.eecs.berkeley.edu` (for initial connection)
   - **Compute Node**: You should be on a BWRC compute node (e.g., `bwrcix-3`, `bwrcix-4`, etc.)
   ```bash
   # First, SSH to login server
   ssh <username>@bwrcrdsl-1.eecs.berkeley.edu
   
   # Then SSH to any available compute node
   ssh <username>@bwrcix-3.eecs.berkeley.edu
   ```
   
   **Note**: Airflow should run on compute nodes, not the login server.

2. **GitHub Account** with SSH access configured (see Step 0.3)

3. **PostgreSQL Server**: Access to `barney.eecs.berkeley.edu:5433`
   
   **If the PostgreSQL server is down**, contact:
   - **Email**: anne_young@berkeley.edu
   - **System Admins**: bwrc-sysadmins@lists.eecs.berkeley.edu

4. **Database**: A PostgreSQL database must exist (e.g., `airflow_<username>`). You can verify this using:

```bash
psql -h barney.eecs.berkeley.edu -p 5433 -U <username> -l
```

5. **Python 3.10-3.13**: Required for Airflow 3.1.0

---

## Part 1: Hammer + Airflow + PostgreSQL Setup

### Step 0: Install Python (If Not Already Installed)

If Python 3.10-3.13 is not already available on your system, build from source:

```bash
cd ~
wget https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz
tar -xzf Python-3.11.9.tgz
cd Python-3.11.9

# Configure and build (this may take 10-30 minutes)
./configure --prefix=$HOME/python311 --enable-optimizations --with-ensurepip=yes
make -j$(nproc)
make install

# Clean up (optional, saves ~380MB)
cd ~
rm -rf Python-3.11.9 Python-3.11.9.tgz

# Add to PATH
echo 'export PATH="$HOME/python311/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# Verify
python3 --version
# Should show: Python 3.11.9
```

### Step 0.3: Set Up GitHub SSH Access

1. **Generate SSH Key** (if you don't already have one):
   ```bash
   ssh-keygen -t ed25519 -C "your_email@berkeley.edu"
   ```

2. **Start SSH Agent and Add Key**:
   ```bash
   eval "$(ssh-agent -s)"
   ssh-add ~/.ssh/id_ed25519
   ```

3. **Copy Your Public Key**:
   ```bash
   cat ~/.ssh/id_ed25519.pub
   ```

4. **Add SSH Key to GitHub**: Go to GitHub → Settings → SSH and GPG keys → New SSH key → Paste key

5. **Verify**:
   ```bash
   ssh -T git@github.com
   # Should show: Hi <username>! You've successfully authenticated...
   ```

### Step 0.4: Create Working Directory

```bash
mkdir -p /bwrcq/C/<username>
cd /bwrcq/C/<username>
```

**Note**: Replace `<username>` with your actual EECS username. Your home directory may be at `/bwrcq/home/<username>` or `/bwrcq/C/<username>`.

### Step 0.5: Clone Hammer Repository

```bash
cd /bwrcq/C/<username>
git clone git@github.com:ucb-bar/hammer.git
cd hammer
```

> **Today**: clone the SledgeHammer fork, `git@github.com:lawrencejrhee/hammer.git`, instead. Stock Hammer has none of the SledgeHammer code. See [WORKSPACE_SETUP.md](WORKSPACE_SETUP.md).

### Step 1: Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh

# Add to PATH (add to ~/.bashrc for persistence)
export PATH="$HOME/.local/bin:$PATH"

# Verify
uv --version
# Should show: uv 0.10.x or later
```

That's it: no Python bootstrap, no pip, no wheel needed.

### Step 1.5: Set Up pg_config for psycopg2

`psycopg2` (the real PostgreSQL adapter, **not** `psycopg2-binary`) must be compiled from C source. This requires `pg_config` and the PostgreSQL development headers, which are **not** installed system-wide on BWRC servers. We extract them locally from the `libpq-devel` RPM (no root access needed).

```bash
# Create a temp directory and download the RPMs explicitly. We do NOT
# use `--resolve` here: BWRC servers already have libpq installed
# system-wide, and `--resolve` would skip downloading it (it only fetches
# deps that aren't already present). That leaves us with libpq-devel but
# no libpq.so.5 to symlink to, and the psycopg2 build fails at link time.
mkdir -p /tmp/pg_build_$$
cd /tmp/pg_build_$$
dnf download libpq-devel
dnf download libpq

# Extract the devel RPM (contains pg_config + headers)
mkdir -p ~/pg_local
rpm2cpio libpq-devel-*x86_64.rpm | (cd ~/pg_local && cpio -idmv)

# Extract the libpq RPM (contains libpq.so shared libraries)
rpm2cpio libpq-[0-9]*x86_64.rpm | (cd ~/pg_local && cpio -idmv)

# Create the linker symlink
ln -sf libpq.so.5 ~/pg_local/usr/lib64/libpq.so

# Go back home BEFORE cleaning up (avoids "could not identify current directory" error)
cd ~
rm -rf /tmp/pg_build_$$

# Verify pg_config works
export PATH="$HOME/pg_local/usr/bin:$PATH"
pg_config --version
# Should show: PostgreSQL 13.x

pg_config --includedir
# Should show: /bwrcq/home/<username>/pg_local/usr/include

pg_config --libdir
# Should show: /bwrcq/home/<username>/pg_local/usr/lib64
```

**Make pg_config persistent** (add to `~/.bashrc`):

```bash
echo 'export PATH="$HOME/pg_local/usr/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

Now `uv` (and pip) can build `psycopg2` from source whenever needed.

### Step 2: Create Virtual Environment and Install Dependencies

> **Today**: skip the conversion. This repo's `pyproject.toml` is already PEP 621 and also defines the `sledgehammer` and `studio` commands, which the block below lacks, so do not replace it. `psycopg2` and `asyncpg` are no longer base dependencies, so the two import checks below fail after a bare `uv sync`. `./scripts/uv_setup.sh` installs both.

First, you need to convert the `pyproject.toml` from Poetry format to standard PEP 621 format. Replace the contents of `pyproject.toml` with:

```toml
[project]
name = "hammer-vlsi"
version = "0.0.0"
description = "Hammer is a physical design framework that wraps around vendor specific technologies and tools to provide a single API to create ASICs."
authors = [
    {name = "Colin Schmidt", email = "colin.schmidt@sifive.com"},
    {name = "Edward Wang", email = "edward.c.wang@compdigitec.com"},
    {name = "John Wright", email = "johnwright@eecs.berkeley.edu"},
]
maintainers = [
    {name = "Harrison Liew", email = "harrisonliew@berkeley.edu"},
    {name = "Daniel Grubb", email = "dpgrubb@berkeley.edu"},
]
readme = "README.md"
requires-python = ">=3.9"
dependencies = [
    "pydantic>=2.12.2,<3",
    "PyYAML>=6.0,<7",
    "ruamel.yaml>=0.17.21,<0.18",
    "networkx>=3.0,<4",
    "numpy>=1.23.0,<2",
    "sphinxcontrib-mermaid>=0.8.1,<0.9",
    "psycopg2>=2.9.11,<3",
    "asyncpg>=0.30.0,<1",
]

[project.optional-dependencies]
asap7-gdstk = ["gdstk>=0.9.0,<0.10"]
asap7 = ["gdspy==1.4"]

[project.scripts]
hammer-generate-mdf = "hammer.shell.hammer_generate_mdf:main"
get-config = "hammer.shell.get_config:main"
hammer-vlsi = "hammer.shell.hammer_vlsi:main"
hammer-shell-test = "hammer.shell.hammer_shell_test:main"
readlink-array = "hammer.shell.readlink_array:main"
yaml2json = "hammer.shell.yaml2json:main"
asap7_gds_scale = "hammer.technology.asap7.gds_scale:main"

[project.urls]
Repository = "https://github.com/ucb-bar/hammer"

[dependency-groups]
dev = [
    "pytest>=7.1,<8",
    "pyright>=1.1,<2",
    "types-PyYAML>=6.0.0,<7",
    "tox>=3.25.1,<4",
    "Sphinx>=5.1.1,<6",
    "sphinx-rtd-theme>=1.0.0,<2",
    "myst-parser>=0.18.0,<0.19",
    "sphinx-jsonschema>=1.19.1,<2",
    "sphinx-rtd-size>=0.2.0,<0.3",
    "pylint>=2.15,<3",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["hammer"]

[tool.tox]
legacy_tox_ini = """
[tox]
envlist = py39,py310
isolated_build = True

[testenv]
deps = pytest
commands =
  pytest tests/ {posargs}
"""

[tool.pylint.format]
max-line-length = 120

[tool.pylint.reports]
output-format = "text"
reports = true

[tool.pylint."messages control"]
disable = ["no-else-return", "len-as-condition"]

[tool.pyright]
exclude = ["doc/**", "e2e/**", "test_scripts/**"]
include = ["hammer", "tests"]
reportMissingImports = true
typeCheckingMode = "standard"
```

**Key differences from Poetry format**:
- `[tool.poetry]` → `[project]` (PEP 621 standard)
- `[tool.poetry.dependencies]` → `[project.dependencies]` (PEP 508 specifiers)
- `[tool.poetry.scripts]` → `[project.scripts]`
- `[tool.poetry.dev-dependencies]` → `[dependency-groups]` dev
- Build backend: `poetry-core` → `hatchling`

**Important**: Make sure you completed [Step 1.5](#step-15-set-up-pg_config-for-psycopg2) first: `pg_config` must be on your `PATH` so `psycopg2` can compile from source.

Now create the venv and install:

```bash
# Ensure pg_config is on PATH
export PATH="$HOME/pg_local/usr/bin:$HOME/.local/bin:$PATH"
pg_config --version   # Should show: PostgreSQL 13.x

# Pin Python version
uv python pin 3.11

# Create virtual environment using your Python
uv venv --python $HOME/python311/bin/python3

# Lock dependencies (psycopg2 will be built from source)
uv lock

# Install everything (project + dev dependencies)
uv sync --group dev

# Verify
source .venv/bin/activate
hammer-vlsi -h
python3 -c "import psycopg2; print('psycopg2:', psycopg2.__version__)"
python3 -c "import asyncpg; print('asyncpg:', asyncpg.__version__)"
```

**Note**: `uv sync` resolved and installed all 67+ packages in under 1 minute. Poetry typically takes 5-15 minutes for the same operation. `psycopg2` is compiled from C source during install (~20 seconds).

### Step 3: Install Apache Airflow

Apache Airflow is installed separately (not in `pyproject.toml`) using Airflow's constraint file for version compatibility:

```bash
# Make sure you're in the hammer directory with venv activated
cd /bwrcq/C/<username>/hammer
source .venv/bin/activate
export AIRFLOW_HOME=$(pwd)
export PATH="$HOME/pg_local/usr/bin:$HOME/.local/bin:$PATH"

# Define versions
AIRFLOW_VERSION=3.1.0
PYTHON_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
CONSTRAINT_URL="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

# Remove doc packages that conflict with Airflow
pip uninstall -y myst-parser mdit-py-plugins markdown-it-py 2>/dev/null || true

# Install Airflow with constraints (uv pip is ~10x faster than pip)
uv pip install "apache-airflow==${AIRFLOW_VERSION}" --constraint "${CONSTRAINT_URL}"

# Reinstall psycopg2 from source (Airflow may have swapped it for psycopg2-binary)
uv pip install psycopg2==2.9.11 --no-binary psycopg2 --reinstall

# Verify
airflow version
# Should show: 3.1.0

python3 -c "import psycopg2; print('psycopg2:', psycopg2.__version__)"
# Should show: psycopg2: 2.9.11 (dt dec pq3 ext lo64)
```

> **Today**: `./scripts/uv_setup.sh` also installs `asyncpg` and the edge3 provider, so the packages above are no longer enough on their own.

### Step 4: Set Up PostgreSQL Connection

#### 4.1 Get PostgreSQL Password

1. Check your @berkeley.edu email for a message from the server administrator
2. Click the link and retrieve your PostgreSQL password

#### 4.2 Generate airflow.cfg

```bash
cd /bwrcq/C/<username>/hammer
source .venv/bin/activate
export AIRFLOW_HOME=$(pwd)

# This creates airflow.cfg
airflow config list > /dev/null 2>&1

# Verify it was created
ls -la airflow.cfg
```

#### 4.3 Update airflow.cfg

Edit `airflow.cfg` and update the `[database]` section:

```ini
[database]
sql_alchemy_conn = postgresql+psycopg2://<username>:YOUR_PASSWORD@barney.eecs.berkeley.edu:5433/<database>
sql_alchemy_pool_size = 10
sql_alchemy_max_overflow = 20
sql_alchemy_engine_args = {"pool_pre_ping": true}
```

Replace `<username>` with your EECS username and `YOUR_PASSWORD` with your actual PostgreSQL password.

> **Today**: `airflow.cfg` is tracked in git and ships with `sql_alchemy_conn` blank, so do not put your password in it. `./scripts/uv_setup.sh` (or `./scripts/sledge-secrets-create.sh`) stores the connection string in a GPG-encrypted file under `.sledgehammer/`. Run the check below as `sledgehammer db check`, which loads it first. See [SECRETS.md](SECRETS.md).

#### 4.4 Test Connection

```bash
airflow db check
# Should show: Connection successful.
```

### Step 5: Configure Airflow

Edit `airflow.cfg` with these critical settings:

```ini
[core]
executor = LocalExecutor
parallelism = 30
max_active_tasks_per_dag = 30
load_examples = False    # off by default so Airflow's built-in example DAGs don't clutter the list
# dags_folder is optional: the launcher pins AIRFLOW_HOME, so Airflow reads this
# checkout's dags/ folder. Set it only to use another directory; the build and
# sledgehammer follow it.

[api]
# Uncomment and set base_url (required for task execution in Airflow 3.x)
base_url = http://localhost:8080

[scheduler]
task_queued_timeout = 1200.0
job_heartbeat_sec = 10
scheduler_heartbeat_sec = 10
```

> **Today**: the tracked `airflow.cfg` already has these settings, except that `[api]` serves on port 8081 (`base_url = http://localhost:8081`) and `[scheduler] job_heartbeat_sec` is 5. The `dags/` folder is in the repo too, so there is nothing to edit or create here.

Create the DAGs folder:

```bash
mkdir -p dags
```

Verify configuration:

```bash
airflow config get-value database sql_alchemy_conn
airflow config get-value core executor
airflow config get-value core dags_folder
```

> **Defaults the launcher handles for you**
>
> - **DAGs folder**: you don't need to set `dags_folder` by hand. `scripts/airflow-standalone-ldap.py` pins `AIRFLOW_HOME` to the checkout at launch, so Airflow's own default, `$AIRFLOW_HOME/dags`, is the checkout's `dags/` folder no matter where you start it from, and the build links each generated DAG into that same folder. To use another folder, set `dags_folder` in `airflow.cfg`, or export `AIRFLOW__CORE__DAGS_FOLDER` in the shell you launch and build from; nothing else is needed. The build's linker, `sledgehammer run` and `studio make-dag` look the folder up the way Airflow does: `$AIRFLOW__CORE__DAGS_FOLDER`, then `[core] dags_folder` in `$AIRFLOW_CONFIG` or `$AIRFLOW_HOME/airflow.cfg`, then `$AIRFLOW_HOME/dags`, where `AIRFLOW_HOME` defaults to the checkout. They check `HAMMER_DAGS_FOLDER` before all three, and Airflow never reads it, so leave it unset unless it names the same folder. Even with it unset, your DAGs go missing when the build and the running stack see different `AIRFLOW_HOME`, `AIRFLOW_CONFIG` or `AIRFLOW__CORE__DAGS_FOLDER` values.
> - **Example DAGs**: `load_examples = False` ships in `airflow.cfg`, so Airflow's built-in example DAGs stay out of the list. Flip it back to `True` if you want them.
>
> Both need `AIRFLOW_HOME` to be the checkout. `sledgehammer` runs `scripts/airflow-standalone-ldap.py`, which pins it there and decrypts your secrets. Run any other `airflow` command through `sledgehammer` (for example `sledgehammer config get-value core dags_folder`): it pins `AIRFLOW_HOME` to the checkout, reads your [stack env file](CLI_REFERENCE.md#the-environment-file) if you have one, then decrypts your secrets. A bare `airflow` command gets that `AIRFLOW_HOME` and your secrets only after `source venv.sh` from the checkout root.

### Step 6: Initialize Database

```bash
cd /bwrcq/C/<username>/hammer
source .venv/bin/activate
export AIRFLOW_HOME=$(pwd)

# Run migrations
airflow db migrate

# Verify
airflow db check
airflow db check-migrations
```

> **Today**: the first `sledgehammer` launch runs the migrations for you. To run them by hand, use `sledgehammer db migrate`, which loads your secrets first.

### Step 7: Start Airflow

#### Option A: Standalone (Recommended)

```bash
cd /bwrcq/C/<username>/hammer
source .venv/bin/activate
export AIRFLOW_HOME=$(pwd)

airflow standalone
```

> **Today**: run `sledgehammer` instead, from any directory (`./scripts/uv_setup.sh` puts it on your `PATH`). It sources `venv.sh` in the checkout, decrypts your secrets and starts the stack. You log in as yourself (your OS user name), with the password the launch keeps in `.sledgehammer/airflow-ui-password.json`. When the stack is ready it prints the web UI URL and an SSH tunnel command. See [CLI_REFERENCE.md](CLI_REFERENCE.md).

Wait for `Airflow is ready`. The auto-generated admin password is in `simple_auth_manager_passwords.json.generated`.

**Note**: In Airflow 3.x, the `airflow users create` CLI command has been removed. `airflow standalone` creates the admin user automatically.

#### Option B: Separate Processes

**Terminal 1 (Scheduler):**
```bash
cd /bwrcq/C/<username>/hammer
source .venv/bin/activate
export AIRFLOW_HOME=$(pwd)
airflow scheduler
```

**Terminal 2 (API Server):**
```bash
cd /bwrcq/C/<username>/hammer
source .venv/bin/activate
export AIRFLOW_HOME=$(pwd)
airflow api-server --port 8080
```

**Note**: In Airflow 3.x, the web UI is served by `airflow api-server`, not `airflow webserver`. Airflow 3 also needs `airflow dag-processor` (it parses the DAG files) and `airflow triggerer`, each in its own terminal like the two above. `airflow standalone` starts all four.

#### Access the UI

**From Remote Desktop (NoMachine/X2Go):**
- URL: http://localhost:8080
- Username: `admin`
- Password: See `simple_auth_manager_passwords.json.generated`

**Via SSH Port Forwarding (from your laptop):**
```bash
ssh -A -L 8080:localhost:8080 -J <username>@bwrcrdsl-1.eecs.berkeley.edu <username>@<compute_node>.eecs.berkeley.edu
```
Then open http://localhost:8080 in your browser.

> **Today**: `sledgehammer` serves on the `[api] port` from `airflow.cfg` (8081 in the tracked file), or the next free port if that one is busy, and prints the URL and the tunnel command to use. You log in as yourself, not as `admin`, with the password in `.sledgehammer/airflow-ui-password.json`.

---

## Part 2: Chipyard Integration

This section sets up Chipyard and configures Hammer's Airflow DAGs for the Sledgehammer VLSI flow. After completing Part 1, you have Hammer + Airflow + PostgreSQL working. Now we add:

- Chipyard (cloned into the hammer directory)
- Hammer source modifications for Airflow compatibility
- libnsl.so.1 for Cadence tools (RHEL 9 workaround)
- Config files for synthesis and PAR
- Sledgehammer DAGs (`Sledgehammer_demo_gcd`, `Sledgehammer_demo_rocket`)

### Step 8: Clone Chipyard

```bash
cd /bwrcq/C/<username>/hammer
git clone git@github.com:ucb-bar/chipyard.git chipyard
```

Add `chipyard/` to `.gitignore` (it's a separate git repository):

```bash
echo "chipyard/" >> .gitignore
```

> **Today**: the tracked `.gitignore` already lists `chipyard/`, so skip the `echo`.

### Step 9: Chipyard Build Setup (Optional)

> **Note**: Chipyard's `build-setup.sh` initializes submodules, builds the RISC-V toolchain, and sets up the conda environment. This takes **30+ minutes** and requires **15GB+ disk space**. It is **only needed for chipyard-based designs** (e.g., RocketTile). The GCD demo uses Hammer's built-in `e2e/` test configs and does **not** require a built chipyard.

If you need to build chipyard-based designs:

```bash
cd /bwrcq/C/<username>/hammer/chipyard

# Source BWRC environment for EDA tool paths
source /tools/C/ee290-sp25/bwrc-env.sh

# Run build setup (skip FireSim steps 6-9)
./build-setup.sh riscv-tools -s 6 -s 7 -s 8 -s 9
```

For just the GCD demo and Sledgehammer DAGs, **skip this step**. The chipyard clone is sufficient.

### Step 10: Modify Hammer Files for Airflow Compatibility

Several Hammer source files need modification to work correctly within Airflow. These changes are **required**: without them, tasks will crash the Airflow worker process or fail to execute.

> **Today**: skip this step; the repo needs none of these edits. `hammer_vlsi.py` no longer holds the DAGs (see 10.2). Genus sets a QRC tech file only when the PDK lists one, and Sky130 lists none. Generated DAGs run each step from the directory you ran the build in, so the relative paths in `common.yml` and `sim-rtl.yml` work. `syn.yml` and `par.yml` are already in the repo.

#### 10.1 Leave `sys.exit()` in `cli_driver.py`

Do not change the `sys.exit()` calls at the end of `CLIDriver.main()`. They give a failed stage a nonzero exit code, which make and the Airflow tasks rely on to stop the flow. Each DAG task runs its action in a separate process, so the exit cannot take down the Airflow worker.

#### 10.2 Replace `hammer_vlsi.py` with DAG definitions

> **Today**: skip this step. `hammer/shell/hammer_vlsi.py` is the `hammer-vlsi` entry point again, plus `run_cli_driver()`, the libnsl block and the helpers that generated DAGs import. The two demo DAGs moved to `hammer/shell/airflow_example_dags.py`, and nothing imports them. You generate a design's DAG with `sledgehammer buildfile`, run from the vlsi directory (or `e2e/`). It runs the build action through `make buildfile` (`make build` in `e2e/`, which has no `buildfile` rule), and with `vlsi.core.build_system: sledgehammer` that action writes `hammer_dag.py` into the design's `OBJ_DIR`; `_link_dag_into_dags_folder` in `hammer/vlsi/hammer_build_systems.py` then links it into the dags folder as `sledgehammer_<design>_<user>.py`. `<design>` is the `OBJ_DIR` basename, so two builds that share it (for example `build-sky130-cm/gcd` and `build-asap7-cm/gcd`) share one DAG, and the linker prints a NOTE when it replaces the other build's link.

**File**: `hammer/shell/hammer_vlsi.py`

The stock `hammer_vlsi.py` is a 9-line script that just calls `CLIDriver().main()`. Replace it entirely with the Sledgehammer DAG definitions that define the `Sledgehammer_demo_gcd` and `Sledgehammer_demo_rocket` DAGs.

The modified file includes:
- **`LD_LIBRARY_PATH` setup**: Module-level code that adds `~/libnsl_local/usr/lib64` to `LD_LIBRARY_PATH` so Cadence tools can find `libnsl.so.1`. This **must** be in the DAG file: Airflow worker subprocesses don't inherit the parent shell's environment.
- **`run_cli_driver()` wrapper**: Catches `SystemExit` from `CLIDriver().main()` to prevent killing the Airflow worker
- **`get_param()` helper**: Reads DAG parameters from `conf` (runtime) or `params` (defaults), fixing the issue where triggering without config skips all tasks
- **`AIRFlow` class**: GCD demo configuration with absolute paths to `e2e/` configs
- **`AIRFlow_rocket` class**: RocketTile demo configuration
- **Two DAG definitions**: `Sledgehammer_demo_gcd` and `Sledgehammer_demo_rocket` with branching task flows

**Important**: Update the absolute paths in the `AIRFlow` and `AIRFlow_rocket` classes to match your installation:

```python
# In AIRFlow.__init__():
self.vlsi_dir = '/bwrcq/C/<username>/hammer/e2e'  # ← your path

# In AIRFlow_rocket.__init__():
self.vlsi_dir = '/bwrcq/C/<username>/hammer/e2e'   # ← your path
self.specs_abs = '/bwrcq/C/<username>/hammer/specs' # ← your path
```

> **Tip**: You can copy the modified `hammer_vlsi.py` from a reference setup (e.g., `<a reference checkout>/hammer/shell/hammer_vlsi.py`) and only update the absolute paths.

#### 10.3 Comment out QRC tech file in Genus

**File**: `hammer/synthesis/genus/__init__.py` (around line 231)

**Problem**: The QRC tech file setting can cause Genus to fail if QRC files are not available for the PDK (Sky130 has no QRC). Commenting it out prevents failures during synthesis.

**Fix**: Comment out lines ~231 to 233:

```python
# Commented out QRC tech file setting: may cause issues if QRC is needed in the future
# if len(qrc_files) > 0:
#     verbose_append("set_db qrc_tech_file {{ {files} }}".format(
#         files=qrc_files[0]
#     ))
```

#### 10.4 Update e2e config files with absolute paths

Airflow workers do not execute from the `e2e/` directory, so all source file references must use **absolute paths**.

**File**: `e2e/configs-design/gcd/common.yml`

```yaml
synthesis.inputs:
  top_module: "gcd"
  input_files:
    - "/bwrcq/C/<username>/hammer/e2e/src/gcd.v"
```

**File**: `e2e/configs-design/gcd/sim-rtl.yml`

```yaml
sim.inputs:
  level: "rtl"
  input_files:
    - "/bwrcq/C/<username>/hammer/e2e/src/gcd.v"
    - "/bwrcq/C/<username>/hammer/e2e/src/gcd_tb.v"
```

#### 10.5 Create syn.yml and par.yml for GCD

The GCD demo needs synthesis and PAR config files that don't exist in stock Hammer.

**File**: `e2e/configs-design/gcd/syn.yml` (create this file)

```yaml
# Specify Global Variables
clockPeriod: &CLK_PERIOD "20.0ns"
clockPeriodby5: &CLK_PERIOD_BY_5 "4.0"
verilogSrc: &VERILOG_SRC
  - "/bwrcq/C/<username>/hammer/e2e/src/gcd.v"

vlsi.inputs.clocks: [
  {name: "clk", period: *CLK_PERIOD, uncertainty: "0.1ns"}
]

vlsi.inputs.delays: [
  {name: "mem*", clock: "clk", direction: "input", delay: *CLK_PERIOD_BY_5}
]

synthesis.inputs:
  top_module: "gcd"
  input_files: *VERILOG_SRC

synthesis.genus:
  use_qrc: false
  qrc_files: []
  generate_reports: true
  generate_final_netlist: true
  timing_driven: true
  clock_gating_mode: "empty"

technology.sky130:
  synthesis_tool: "genus"
```

**File**: `e2e/configs-design/gcd/par.yml` (create this file)

```yaml
# Place and Route configuration for GCD design
# PAR input is dynamically generated by syn_to_par() from synthesis outputs.
```

### Step 11: Install libnsl Locally (RHEL 9, Required for Cadence Tools)

RHEL 9 removed `libnsl.so.1`, which Cadence tools (Genus, Innovus) depend on. Without it, synthesis and PAR fail with `genus: error while loading shared libraries: libnsl.so.1`.

```bash
mkdir -p /tmp/libnsl_build
cd /tmp/libnsl_build
dnf download libnsl

mkdir -p ~/libnsl_local
rpm2cpio libnsl-*x86_64.rpm | (cd ~/libnsl_local && cpio -idmv)

# Verify
ls ~/libnsl_local/usr/lib64/libnsl.so.1

cd ~
rm -rf /tmp/libnsl_build
```

> **IMPORTANT**: Do **NOT** copy Sky130 PDK files (`__init__.py`, `sram-cache.json`, `sram_compiler/__init__.py`) from other reference setups. They may be from a different Hammer version with incompatible APIs, causing `ImportError` at runtime. The repo's own Sky130 files work as-is for the GCD demo. Since the sky130 port (September 2026) they are no longer the stock files, but they still come with the repo, so keep them.

### Step 12: Update Airflow Configuration for DAGs

Now that the DAGs are in `hammer/shell/hammer_vlsi.py`, update `airflow.cfg`:

#### 12.1 Point `dags_folder` to hammer/shell

```ini
[core]
dags_folder = /bwrcq/C/<username>/hammer/hammer/shell
```

> **Today**: skip this and leave `dags_folder` unset. The build looks the dags folder up the way Airflow does and links each generated DAG there. With `dags_folder` unset, that is Airflow's default, `$AIRFLOW_HOME/dags`, which is the checkout's `dags/` folder because `sledgehammer` runs Airflow with the checkout as `AIRFLOW_HOME`. If you do set `dags_folder` (or export `AIRFLOW__CORE__DAGS_FOLDER`), the build and `sledgehammer` follow it without `HAMMER_DAGS_FOLDER`. See the note in [Step 5](#step-5-configure-airflow).

#### 12.2 Add Security Keys (CRITICAL for Airflow 3.x)

Without these, the scheduler cannot communicate with the API server and **all tasks will fail** with `Invalid auth token: Signature verification failed`.

Generate keys:

```bash
# Generate Fernet key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Generate secret key
python -c "import secrets; import base64; print(base64.b64encode(secrets.token_bytes(16)).decode())"

# Generate JWT secret
python -c "import secrets; import base64; print(base64.b64encode(secrets.token_bytes(16)).decode())"
```

Add to `airflow.cfg`:

```ini
[core]
fernet_key = <your_generated_fernet_key>
internal_api_secret_key = <your_generated_secret_key>

[api]
secret_key = <same_as_internal_api_secret_key>  # MUST match internal_api_secret_key

[api_auth]
jwt_secret = <your_generated_jwt_secret>
```

> **CRITICAL**: `internal_api_secret_key` and `secret_key` **must be the same value**. If they don't match, tasks will crash immediately with signature verification errors.

> **Today**: these keys go in the GPG-encrypted secrets file too, not in the tracked `airflow.cfg` (see the note in [Step 4.3](#43-update-airflowcfg)).

#### 12.3 Run DB Migrate

```bash
source venv.sh
airflow db migrate
```

### Step 13: Verify DAG Discovery

```bash
source venv.sh

# Reserialize DAGs
airflow dags reserialize

# List DAGs
airflow dags list | grep Sledgehammer
# Should show:
# Sledgehammer_demo_gcd
# Sledgehammer_demo_rocket

# Check for import errors
airflow dags list-import-errors
# Should show: No data found
```

> **Today**: the demo DAGs are not loaded (see [Step 10.2](#102-replace-hammer_vlsipy-with-dag-definitions)), so this `grep` finds nothing. Look for your own design's DAG instead: `sledgehammer dags list | grep sledgehammer_`.

---

## Available DAGs

> **Today**: these two demo DAGs sit in `hammer/shell/airflow_example_dags.py` and are not loaded (see [Step 10.2](#102-replace-hammer_vlsipy-with-dag-definitions)). Each design gets its own `sledgehammer_<design>_<user>` DAG, which you generate with `sledgehammer buildfile` from the design's vlsi directory and run with `sledgehammer <stage>`; see [CLI_REFERENCE.md](CLI_REFERENCE.md).

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

**Default config**: design=`example`, PDK=`sky130`, tools=`cm`, env=`example`. Override with the `design`/`pdk`/`tools`/`env` environment variables, set to your site's names

### DAG Task Flow

```
start → clean → build_decider ──→ build → sim_or_syn_decide ──→ sim_rtl → exit_
                     │                            │
                     │                            └──→ syn_decider → syn → par_decider → par → exit_
                     │
                     └──→ exit_ (if no steps enabled)
```

The Rocket DAG adds `sram_decider → sram_generator` between `sim_or_syn_decide` and `syn_decider`.

### IMPORTANT: `sim_rtl` and `syn/par` are mutually exclusive

The `sim_or_syn_decide` branch checks `sim_rtl` **first**. If `sim_rtl=True`, it takes the simulation path and **skips** synthesis/PAR. To run synthesis and PAR, set `sim_rtl=False`.

---

## Usage

> **Today**: these commands target the demo DAGs. Run your own design's DAG with `sledgehammer <stage>` or `sledgehammer run <stages> -t <design>`, and check it with `sledgehammer status --design <design>`. See [CLI_REFERENCE.md](CLI_REFERENCE.md).

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

1. Open browser to `http://localhost:8080`
2. Find your DAG in the list
3. Unpause the DAG (toggle the switch)
4. Click the **Play** button (▶)
5. (Optional) Enter JSON config: `{"sim_rtl": false, "syn": true, "par": true}`
6. Click "Trigger"

### Checking Status

```bash
source venv.sh

# List DAGs
airflow dags list

# Check import errors
airflow dags list-import-errors

# Check task states for a run
airflow tasks states-for-dag-run Sledgehammer_demo_gcd "<run_id>"
```

---

## Daily Usage

### Quick Start (after initial setup)

```bash
cd /bwrcq/C/<username>/hammer
source venv.sh    # activates venv + sets AIRFLOW_HOME + sources BWRC env
airflow standalone
```

> **Today**: the quick start is just `sledgehammer`, from any directory. It sources `venv.sh` in the checkout for you (see [Step 7](#step-7-start-airflow)).

### Convenience Script: `venv.sh`

Create this file in the hammer root:

> **Today**: `venv.sh` ships with the repo and does more than the version below (it also loads your encrypted secrets and strips conda library paths), so do not overwrite it.

```bash
source ./.venv/bin/activate

# Set AIRFLOW_HOME to current directory (Hammer root)
export AIRFLOW_HOME=$(pwd)

# Ensure uv and pg_config are on PATH
export PATH="$HOME/pg_local/usr/bin:$HOME/.local/bin:$PATH"

# Source BWRC environment for EDA tools (VCS, Genus, Innovus, etc.)
if [ -f /tools/C/ee290-sp25/bwrc-env.sh ]; then
    source /tools/C/ee290-sp25/bwrc-env.sh
    echo "BWRC EDA environment sourced."
fi

# RHEL 9 workaround: Cadence tools need libnsl.so.1 (removed in RHEL 9)
if [ -f "$HOME/libnsl_local/usr/lib64/libnsl.so.1" ]; then
    export LD_LIBRARY_PATH="$HOME/libnsl_local/usr/lib64:${LD_LIBRARY_PATH:-}"
fi

echo "Virtual environment activated."
echo "AIRFLOW_HOME set to: $AIRFLOW_HOME"
```

Usage: `source venv.sh`

---

## Relaunching (After Initial Setup)

```bash
# 1. Navigate to hammer directory
cd /bwrcq/C/<username>/hammer

# 2. Source the convenience script
source venv.sh

# 3. Start Airflow
airflow standalone

# Check password in: simple_auth_manager_passwords.json.generated
```

> **Today**: run `sledgehammer` instead of all three steps, and log in as yourself with the password in `.sledgehammer/airflow-ui-password.json` (see [Step 7](#step-7-start-airflow)).

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
# The DAG will be re-imported on the next scheduler scan
```

### Via Web UI

1. Go to the DAG's Grid view
2. Click on the specific DAG run
3. Click "Clear" to reset all tasks in that run

---

## uv Cheat Sheet (vs Poetry)

| Task | Poetry | uv |
|------|--------|-----|
| Install tool | `curl -sSL https://install.python-poetry.org \| python3 -` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Create venv | `poetry config virtualenvs.in-project true` | `uv venv` |
| Install deps | `poetry install` | `uv sync` |
| Install with dev | `poetry install` (auto) | `uv sync --group dev` |
| Add dependency | `poetry add package` | `uv add package` |
| Add dev dependency | `poetry add --dev package` | `uv add --group dev package` |
| Update lockfile | `poetry lock` | `uv lock` |
| Run command | `poetry run cmd` | `uv run cmd` |
| Activate venv | `poetry shell` | `source .venv/bin/activate` |
| Show installed | `poetry show` | `uv pip list` |
| Pin Python | N/A | `uv python pin 3.11` |
| Install Python | Need pyenv | `uv python install 3.11` |

### Key Advantages of uv

1. **10-100x faster**: Dependency resolution in milliseconds, not minutes
2. **Single binary**: No Python bootstrap required to install
3. **PEP 621 standard**: `pyproject.toml` uses the standard `[project]` format
4. **Built-in Python management**: Can install Python versions itself
5. **Lockfile**: `uv.lock` is cross-platform by default
6. **Source builds**: `--no-binary` flag compiles C extensions from source cleanly

---

## Verification

### Quick Check

```bash
cd /bwrcq/C/<username>/hammer
source venv.sh

# Test hammer
hammer-vlsi -h

# Test database
airflow db check
airflow db check-migrations

# Test postgres connection
sledgehammer db check
studio cache-status

# Run hammer tests
pytest tests/ -m "not long" -v
```

### Verify Metadata Storage

```bash
python3 << 'EOF'
from sqlalchemy import create_engine, text
from airflow.configuration import conf

engine = create_engine(conf.get('database', 'sql_alchemy_conn'))
with engine.connect() as conn:
    result = conn.execute(text("SELECT COUNT(*) FROM dag_run"))
    print(f"DAG runs: {result.fetchone()[0]}")
    
    result = conn.execute(text("SELECT COUNT(*) FROM task_instance"))
    print(f"Task instances: {result.fetchone()[0]}")
    
    result = conn.execute(text("SELECT COUNT(*) FROM task_instance WHERE state = 'success'"))
    print(f"Successful tasks: {result.fetchone()[0]}")
EOF
```

---

## Troubleshooting

### "uv: command not found"

```bash
export PATH="$HOME/.local/bin:$PATH"
# Add to ~/.bashrc for persistence
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

### psycopg2 fails to build from source

**Error**: `pg_config executable not found`

This means `pg_config` is not on your `PATH`. **Do NOT switch to `psycopg2-binary`**. Instead, set up `pg_config` locally (see [Step 1.5](#step-15-set-up-pg_config-for-psycopg2)):

```bash
# 1. Make sure pg_config is on PATH
export PATH="$HOME/pg_local/usr/bin:$PATH"
pg_config --version   # Should show PostgreSQL 13.x

# 2. Rebuild psycopg2 from source
uv pip install psycopg2==2.9.11 --no-binary psycopg2 --reinstall

# 3. Verify it's the real psycopg2 (compiled from C source)
python3 -c "import psycopg2; print('psycopg2:', psycopg2.__version__)"
# Should show: 2.9.11 (dt dec pq3 ext lo64)
```

**Error**: `cannot find -lpq` or `libpq.so: cannot open shared object file`

You ran an older version of Step 1.5 that used `dnf download --resolve libpq-devel`. On BWRC the system already has `libpq` installed, so `--resolve` skipped downloading it, leaving `~/pg_local/usr/lib64/libpq.so` as a dangling symlink. Re-run Step 1.5 (current version). It explicitly downloads both `libpq-devel` and `libpq` so the local copy is complete:

```bash
rm -rf ~/pg_local
# ...then re-run Step 1.5
```

**If `dnf download` doesn't work** in Step 1.5, contact:
- **Email**: anne_young@berkeley.edu
- **System Admins**: bwrc-sysadmins@lists.eecs.berkeley.edu
- **Request**: `libpq-devel` package (provides `pg_config` and PostgreSQL headers)

### Connection Refused to PostgreSQL

1. Verify you're on a compute node, not the login server
2. Check server: `ping barney.eecs.berkeley.edu`
3. Check port: `telnet barney.eecs.berkeley.edu 5433`
4. If server is down, contact: anne_young@berkeley.edu or bwrc-sysadmins@lists.eecs.berkeley.edu

### Authentication Failed

1. Verify username and password in `airflow.cfg` connection string (today it lives in your encrypted secrets file; see [SECRETS.md](SECRETS.md))
2. Check for extra spaces when copying password
3. Contact sysadmins if password doesn't work

### Tasks Not Starting / Network Unreachable

Ensure `base_url` is set in `[api]` section of `airflow.cfg` (today `sledgehammer` sets it to the port it serves on):
```ini
[api]
base_url = http://localhost:8080
```

### Port Already in Use

```bash
# Check what's using the port
lsof -i :8080
# Or use a different port
airflow api-server --port 8090
# Then update airflow.cfg: base_url = http://localhost:8090
```

> **Today**: `sledgehammer` moves to the next free port by itself and sets `base_url` to match. Set `SLEDGE_PORT=<port>` to pin one; the launch then stops if that port is taken.

### Migrations Pending

```bash
airflow db migrate
```

### Tasks fail with "Invalid auth token: Signature verification failed"

**Cause**: One or more security keys in `airflow.cfg` are missing or inconsistent.

**Fix (check in order)**:
1. Ensure `fernet_key`, `internal_api_secret_key`, and `secret_key` are all set (see [Step 12.2](#122-add-security-keys-critical-for-airflow-3x))
2. Ensure `internal_api_secret_key` and `secret_key` are the **same value**
3. Add `[api_auth] jwt_secret` if missing

### Tasks fail with "hammer-shell-test returned non-zero exit status" or "No module named 'hammer'"

**Cause**: The `hammer` package is not discoverable. The editable install `.pth` file may be stale.

**Fix**:
```bash
cd /bwrcq/C/<username>/hammer
uv pip install -e .
hammer-shell-test
# Should print: "hammer-shell appears to be on the path"
```

### Tasks crash with `RecursionError: maximum recursion depth exceeded`

**Cause**: The `run_cli_driver()` wrapper is calling itself instead of `CLIDriver().main()`.

**Fix**: Ensure `run_cli_driver()` calls `CLIDriver().main()` internally, NOT `run_cli_driver()`.

### `syn`, `par` tasks are skipped even though they're set to True

**This is expected** in the March demo DAGs. The `sim_or_syn_decide` branch treats `sim_rtl` and `syn/par` as **mutually exclusive** (a generated DAG has no such branch and runs every stage you select). To run syn/par:

```bash
airflow dags trigger Sledgehammer_demo_gcd --conf '{"sim_rtl": false}'
```

### `genus: error while loading shared libraries: libnsl.so.1`

**Cause**: RHEL 9 removed `libnsl.so.1`. Cadence Genus and Innovus depend on it.

**Fix**: Install `libnsl.so.1` locally (see [Step 11](#step-11-install-libnsl-locally-rhel-9-required-for-cadence-tools)) AND ensure `LD_LIBRARY_PATH` is set in `hammer_vlsi.py` (see [Step 10.2](#102-replace-hammer_vlsipy-with-dag-definitions)).

Setting `LD_LIBRARY_PATH` only in `venv.sh` is **not enough**: Airflow worker subprocesses don't inherit it. The path must also be set in the DAG file itself.

> **Today**: `sledgehammer` sources `venv.sh` before it starts the stack, and each task copies that environment, so the `venv.sh` setting does reach the tools. The block in `hammer_vlsi.py` is in the repo as well.

### DAGs not appearing

1. Check import errors: `airflow dags list-import-errors`
2. Verify that the folder Airflow reads holds the `<link>` from the `Linked DAG into dags folder: <link> -> <dag_file>` line the build printed. `sledgehammer config get-value core dags_folder` prints the folder Airflow reads, with `AIRFLOW_HOME` pinned to the checkout as the launch pins it. By default it is the checkout's `dags/` folder (in March it pointed to `hammer/shell/`). If `HAMMER_DAGS_FOLDER` is set, it must be that same path, since it overrides where the build links.
3. Test import manually: `python3 <link>`, using the `<link>` path from that line
4. Verify hammer is importable: `python3 -c "from hammer.vlsi import CLIDriver"`
5. Run `airflow dags reserialize` to force a re-scan

### `airflow db init` doesn't work

Airflow 3.x removed `db init`. Use `airflow db migrate` instead. It's idempotent and safe to run any number of times.

### Want to Reset Everything

```bash
cd /bwrcq/C/<username>/hammer
rm -rf .venv uv.lock airflow.cfg logs/ chipyard/
export PATH="$HOME/pg_local/usr/bin:$HOME/.local/bin:$PATH"
uv venv --python $HOME/python311/bin/python3
uv lock
uv sync --group dev
# Then re-do Steps 3-7 and Part 2
```

> **Today**: `uv.lock` and `airflow.cfg` are tracked in git, so leave them out of the `rm`. After removing `.venv`, rerun `./scripts/uv_setup.sh` instead of the remaining commands.

---

## All Modifications Summary

### Files Modified from Stock Hammer

| File | Change | Why |
|------|--------|-----|
| `pyproject.toml` | Converted from Poetry to PEP 621 for uv | uv compatibility |
| `hammer/shell/hammer_vlsi.py` | Replaced with ~1090-line Sledgehammer DAG definitions including `LD_LIBRARY_PATH` setup (today it holds no DAGs; see Step 10.2) | Airflow DAG file with libnsl workaround, `run_cli_driver()` wrapper, two DAG definitions |
| `hammer/synthesis/genus/__init__.py` | Commented out QRC tech file lines ~231-233 (today left active; see Step 10) | Sky130 has no QRC; prevents Genus errors |
| `e2e/configs-design/gcd/common.yml` | Relative paths → absolute paths (today relative again; see Step 10) | Airflow workers don't run from e2e/ |
| `e2e/configs-design/gcd/sim-rtl.yml` | Relative paths → absolute paths (today relative again; see Step 10) | Airflow workers don't run from e2e/ |
| `airflow.cfg` | dags_folder, security keys, PostgreSQL connection (today tracked in git, with no `dags_folder` and the secret fields blank) | Airflow configuration |
| `.gitignore` | Added chipyard/, airflow.cfg, logs/, wheels/ (today `airflow.cfg` is tracked, not ignored) | Git cleanliness |
| `venv.sh` | Created convenience script with libnsl `LD_LIBRARY_PATH` (today it ships with the repo) | Easy environment activation |

### Files Created (not in stock Hammer)

| File | Purpose |
|------|---------|
| `e2e/configs-design/gcd/syn.yml` | Synthesis constraints: clock period (20ns), input delays, QRC disabled |
| `e2e/configs-design/gcd/par.yml` | PAR config placeholder |

### Sky130 Files (do NOT replace)

| File | Note |
|------|------|
| `hammer/technology/sky130/__init__.py` | Sky130 PDK plugin, changed by the sky130 port (September 2026): do NOT replace with copies from other setups |
| `hammer/technology/sky130/sram-cache.json` | SRAM cache, changed by the sky130 port: do NOT replace |
| `hammer/technology/sky130/sram_compiler/__init__.py` | SRAM compiler, changed by the sky130 port: do NOT replace |

### External Dependencies Installed (not in the repo)

| Item | Location | Purpose |
|------|----------|---------|
| `libnsl.so.1` | `~/libnsl_local/usr/lib64/` | RHEL 9 workaround: Cadence tools need this library. Extracted from RPM. |

---

## Directory Structure

```
hammer/                               # AIRFLOW_HOME
├── airflow.cfg                       # Airflow configuration (CRITICAL)
├── pyproject.toml                    # PEP 621 project config (for uv)
├── uv.lock                           # uv lockfile
├── .venv/                            # Virtual environment
├── venv.sh                           # Convenience activation script
├── chipyard/                         # Chipyard repository (cloned in Step 8)
├── dags/                             # Links to generated DAGs (today)
├── logs/                             # Airflow task logs
├── simple_auth_manager_passwords...  # Auto-generated admin password (today in .sledgehammer/airflow-ui-password.json)
├── hammer/                           # Hammer Python package
│   ├── shell/
│   │   └── hammer_vlsi.py            # ★ hammer-vlsi entry point, helpers for generated DAGs
│   ├── vlsi/
│   │   └── cli_driver.py             # ★ CLIDriver
│   ├── synthesis/
│   │   └── genus/
│   │       └── __init__.py           # ★ Genus plugin
│   └── technology/
│       └── sky130/                   # Sky130 PDK, from the sky130 port (do NOT replace)
└── e2e/                              # End-to-end configs
    ├── configs-env/                  # Environment configs (bwrc-env.yml)
    ├── configs-pdk/                  # PDK configs (sky130.yml)
    ├── configs-tool/                 # Tool configs (cm.yml)
    ├── configs-design/               # Design configs
    │   └── gcd/
    │       ├── common.yml            # Design config (relative paths today)
    │       ├── sim-rtl.yml           # ★ Simulation config (relative paths today)
    │       ├── syn.yml               # ★ Synthesis config (CREATED, not in stock)
    │       ├── par.yml               # ★ PAR config (CREATED, not in stock)
    │       └── sky130.yml            # PDK-specific design config
    └── src/
        ├── gcd.v                     # GCD design source
        └── gcd_tb.v                  # GCD testbench
```

Files marked with ★ were modified from the stock Hammer repository.

---

## Best Practices

1. **All 4 security keys**: Always set `fernet_key`, `internal_api_secret_key`, `secret_key`, AND `jwt_secret`
2. **Port consistency**: `[api] base_url` must match the actual port Airflow runs on (today `sledgehammer` sets it for you)
3. **Source BWRC env**: Always source the EDA tool environment before starting Airflow
4. **Set AIRFLOW_HOME**: Every terminal that runs `airflow` commands must have `AIRFLOW_HOME` set
5. **No `sys.exit()` in tasks**: Never use `sys.exit()` in Airflow task code
6. **Use absolute paths in YAML configs**: Airflow workers don't run from the e2e directory (today not needed: generated DAGs run each step from the directory you ran the build in)
7. **Understand `sim_rtl` vs `syn/par` exclusivity**: The DAG branches, so you can run sim OR syn/par, not both (demo DAGs only; a generated DAG runs every stage you select)
8. **Use `airflow db migrate` freely**: It's idempotent, so it's safe to run anytime
9. **Verify editable installs after moving repos**: Re-run `uv pip install -e .` if you move the directory

---

## References

- [Chipyard Documentation](https://chipyard.readthedocs.io/)
- [Hammer VLSI Documentation](https://hammer-vlsi.readthedocs.io/)
- [Apache Airflow 3.x Documentation](https://airflow.apache.org/docs/)
- [uv Documentation](https://docs.astral.sh/uv/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [psycopg2 Documentation](https://www.psycopg.org/docs/)

---

## Support

For issues specific to SledgeHammer PostgreSQL integration:
1. Check this documentation
2. Run `sledgehammer db check` to test the connection to the Airflow metadata database
3. Run `sledgehammer db check-migrations` for the schema status and `studio cache-status` for the PD cache database
4. If the PostgreSQL server is down, contact:
   - **Email**: anne_young@berkeley.edu
   - **System Admins**: bwrc-sysadmins@lists.eecs.berkeley.edu

---

**Author**: Lawrence Rhee  
**Date**: April 1, 2026  
**Airflow Version**: 3.1.0  
**uv Version**: 0.10.8+  
**PostgreSQL Version**: 13.22 (on server)  
**Python Version**: 3.11.9
