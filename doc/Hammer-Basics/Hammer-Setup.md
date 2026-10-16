# Hammer Setup

Hammer depends on Python 3.9+.

The Hammer setup is different based on its usecase, which are summarized as follows:

* [User Setup](#user-setup): Use Hammer with no modifications.
* [Power User Setup](#power-user-setup): Modify Hammer and see these changes immediately reflected when calling Hammer [recommended].
* [Developer Setup](#developer-setup): Developing major Hammer features, adding dependencies, running Hammer unit tests, etc.

> **SledgeHammer:** The User Setup and Power User Setup below install upstream Hammer, which has no `sledgehammer` CLI.
> To set up SledgeHammer, run `./scripts/uv_setup.sh` from the root of your SledgeHammer checkout, as [Workspace setup](https://github.com/lawrencejrhee/hammer/blob/master/readme_documents/WORKSPACE_SETUP.md) describes.

Note that some tools and technologies have extra setup requirements:
* [ASAP7 setup directions](https://github.com/lawrencejrhee/hammer/blob/master/hammer/technology/asap7/README.md)
* [Sky130 & OpenROAD tools setup](../Examples/openroad-sky130.md)

> **SledgeHammer:** The RTL fingerprint needs the `slang` binary at version 11.0 (`SLANG_VERSION` in `hammer/vlsi/rtl_check.py`), which `./scripts/uv_setup.sh` downloads to `tools/slang/slang` on Linux x86_64 and macOS arm64; on other platforms, install slang 11.0 yourself and set `SLANG_BIN` to its path.
> Hammer uses `$SLANG_BIN` if it is set, otherwise `slang` on `PATH`, otherwise `tools/slang/slang` in the checkout, and counts slang as missing if that binary is not version 11.0 or `SLANG_BIN` is not a file.
> Without slang, or when slang cannot parse the RTL, the fingerprint falls back to a hash of the RTL files' bytes and logs a warning, so comment and whitespace edits count as RTL changes and files pulled in by `` `include `` are not covered.

## User Setup

Install the hammer python package from PyPI.

```shell
pip install hammer-vlsi

# if using the ASAP7 PDK you need the gdspy or gdstk dependencies
pip install hammer-vlsi[asap7]        # default: gdspy
pip install hammer-vlsi[asap7-gdstk]  # install gdstk instead

# verify that you can run the `hammer-vlsi` script from the command line
hammer-vlsi -h
```

## Power User Setup
Install Hammer as a source dependency to some target virtual environment, such as conda.

```shell
# clone Hammer somewhere on your disk
git clone https://github.com/ucb-bar/hammer.git

# activate your target virtual environment, e.g. Chipyard
#   commands vary for this based on your package/environment manager:
#     conda activate <env name>
#     source <path>/.venv/bin/activate

# install hammer package in editable mode to your environment
cd hammer
pip install -e .  # run this after activating your target environment

```

### Installing from Another Poetry Project

Hammer tool (e.g. `hammer-mentor-plugins`) and PDK plugin repositories are poetry projects (with a `pyproject.toml` in their root).
To depend on Hammer as a source dependency, first clone Hammer somewhere on your disk.
Then add this snippet to the tool/PDK plugin repo's `pyproject.toml` (and remove any PyPI dependency on Hammer):

```toml
[tool.poetry.dependencies]
#hammer-vlsi = "^1.0.0"
hammer-vlsi = {path = "path/to/hammer", extras = ["asap7"], develop = true}
```

Run `poetry update` and `poetry install`.
Do not commit the changes to `pyproject.toml` or `poetry.lock` without first removing the source dependency.
You only need to specify `extras` if you need the `asap7` optional dependency (gdstk).


## Developer Setup

1. Clone SledgeHammer with `git` (upstream `ucb-bar/hammer` still uses poetry, so the uv steps below do not apply to it)

```shell
git clone git@github.com:lawrencejrhee/hammer
cd hammer
```
2. [Install uv](https://docs.astral.sh/uv/getting-started/installation/) to manage the development virtualenv and dependencies

```shell
curl -LsSf https://astral.sh/uv/install.sh | sh
```

3. Create a uv-managed virtualenv using the dependencies from `pyproject.toml` and `uv.lock`

```shell
# create the virtualenv inside the project folder (in .venv)
uv sync --group dev
```

For the full SledgeHammer stack (Airflow, the Postgres driver, slang), run `./scripts/uv_setup.sh` instead, which runs this sync for you.
Do not run `uv sync` after it: Airflow is installed outside the lock, so a sync uninstalls it.

4. Activate the virtualenv. Within the virtualenv, Hammer is installed and you can access its scripts defined in
    `pyproject.toml` (in `[project.scripts]`)

```shell
source .venv/bin/activate
hammer-vlsi -h
```

### Using PyCharm

This project works out of the box with PyCharm.
You should install the [Pydantic plugin](https://plugins.jetbrains.com/plugin/12861-pydantic) to enable autocomplete for Pydantic BaseModels.

### Unit Tests with pytest

Within the virtualenv, from the root of Hammer, run the tests (`-v` will print out each test name explicitly)

```shell
pytest tests/ -v
```

If you want to skip the single long running-test in `test_stackup.py`:

```shell
pytest tests/ -m "not long" -v
```

If you want to run only a specific test use `-k` with a snippet of the test function you want to run:

```shell
pytest tests/ -k "lsf" -v

> tests/test_submit_command.py::TestSubmitCommand::test_lsf_submit[lsf] PASSED
```

By default, `pytest` will only display what a test prints to stdout if the test fails.
To display stdout even for a passing test, use `-rA`:

```shell
pytest tests/test_build_systems.py -k "flat_makefile" -rA -v

> __________________ TestHammerBuildSystems.test_flat_makefile _________________
> ---------------------------- Captured stdout call ----------------------------
> [<global>] Loading hammer-vlsi libraries and reading settings
> [<global>] Loading technology 'nop'
> =========================== short test summary info ==========================
> PASSED tests/test_build_systems.py::TestHammerBuildSystems::test_flat_makefile
```

### Type Checking with pyright

Within the virtualenv, run: `pyright`

### Testing Different Python Versions with tox

Hammer is supposed to work with Python 3.9+, so we run its unit tests on all supported Python versions using `tox` and `pyenv`.

1. [Install `pyenv`](https://github.com/pyenv/pyenv-installer)

```shell
curl https://pyenv.run | bash
```

Restart your shell and run `pyenv init` (and follow any of its instructions).
Then restart your shell again.

2. Install Python versions

Install the Python versions in the tox `envlist` (in `pyproject.toml` under `[tool.tox]`) using `pyenv`.
The `.python-version` file at the root of hammer names only `3.11` (the version uv builds `.venv` with), so select them for your shell with `pyenv shell`.

```shell
pyenv install 3.9.13
pyenv install 3.10.6
pyenv shell 3.9.13 3.10.6
```

Once the Python interpreters are selected, run `pyenv versions` from the root of hammer.

```shell
pyenv versions
  system
* 3.9.13 (set by PYENV_VERSION environment variable)
* 3.10.6 (set by PYENV_VERSION environment variable)
```

3. From within the virtualenv, run `tox`

```shell
tox
```

This will run the pytest unit tests using all the Python versions specified in `pyproject.toml` under the `[tool.tox]` key.

You can run tests only on a particular environment with `-e`

```shell
tox -e py39 # only run tests on Python 3.9
```

You can pass command line arguments to the pytest invocation within a tox virtualenv with `--`

```shell
tox -e py39 -- -k "lsf" -v
```

### Adding / Updating Dependencies

To add a new Python (`pip`) dependency, modify `pyproject.toml`.
If the dependency is only used for development, add it to the `dev` list under the key `[dependency-groups]`, otherwise add it to `dependencies` under the key `[project]`.
Then run `uv lock` and `uv sync --group dev` (rerun `./scripts/uv_setup.sh` instead if you set up with it).
The updated `uv.lock` file should be committed to Hammer.

To update an existing dependency, modify `pyproject.toml` with the new version constraint.
Run `uv lock --upgrade-package <name>` and `uv sync --group dev` (or rerun `./scripts/uv_setup.sh` in place of the sync) and commit `uv.lock`.

### Building Documentation

The Sphinx build needs `myst-parser` from the dev group.
The default `./scripts/uv_setup.sh` profile uninstalls it before it installs Airflow, so build the docs from a virtualenv made by `uv sync --group dev` alone, such as a separate clone.

First, generate the `schema.json` file from within your virtualenv:

```shell
python3 -c "from hammer.tech import TechConfig; print(TechConfig.schema_json(indent=2))" > doc/Technology/schema.json
```

Then:
- `cd doc`
- Modify any documentation files. You can migrate any rst file to Markdown if desired.
- Run `sphinx-build . build`
- The generated HTML files are placed in `build/`
- Open them in your browser `firefox build/index.html`

### Publishing

Build a sdist and wheel (results are in `dist`):

```shell
uv build
```

`hammer-vlsi` on PyPI is upstream Hammer's, so `.github/workflows/publish.yml` publishes only from `ucb-bar/hammer`, never from a fork.

To publish on TestPyPI:

1. Create an account on [TestPyPi](https://test.pypi.org/)
2. Create an API token in your TestPyPI account settings
3. Publish: `uv publish --publish-url https://test.pypi.org/legacy/ --token <token>`
