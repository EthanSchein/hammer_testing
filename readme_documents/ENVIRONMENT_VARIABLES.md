# SledgeHammer environment variables

The environment variables SledgeHammer's own code reads, grouped by area.
Hammer's upstream variables are listed at the end and behave as upstream.

Switches do not all read their values the same way. The on/off switches
(`HAMMER_PD_CACHE`, `HAMMER_PD_CACHE_LEDGER`, `HAMMER_SUBSTEP_RESUME`,
`HAMMER_DB_CHECKPOINTS`, `HAMMER_ERROR_SCAN`) are off for `0`, `false`, `no`,
`off` or an empty value, in any case and with surrounding spaces ignored, and
on for anything else. Once set, the variable beats its config key, except that
an off `HAMMER_PD_CACHE` leaves the cache to its key. The matching config keys
take `true` or `false`, `1` or `0`, or `yes`, `no`, `on` or `off`, in any case,
and `""` as off; other text logs a warning and keeps the key's default from
`hammer/config/defaults.yml`. None of these keys is part of the dependency
check, so changing one never reruns a stage. Presence flags, marked "Set (any
value)" below, act whenever they hold anything, `0` included, so unset them to
turn them off. `HAMMER_CHECKPOINT_STREAM` follows its own rules, given in its
row.

## Setup

Read by `scripts/uv_setup.sh`.

| Variable | Default | Effect |
|---|---|---|
| `SLEDGE_LAB` | unset | Set (any value): the lab profile. Hammer only: no Airflow, Postgres driver, dev tools or prompts, and the system python is preferred. `SLEDGE_LAB=0` still selects it |
| `SLEDGE_NO_PLUGINS` | unset | Set (any value): skip the editable install of the `hammer-*-plugin*` directories next to the checkout |
| `PYVER` | `3.11` | Python minor version the venv is built with |
| `SLANG_VERSION` | `11.0` | slang release fetched into `tools/slang/slang`. Leave it: the fingerprint accepts only 11.0 (see below) |
| `AIRFLOW_VERSION`, `EDGE3_VERSION` | `3.1.0`, `1.3.0` | Versions of Airflow and the edge3 provider |

Setup also reads `SLEDGE_SECRETS_FILE` (see the launcher table) to decide
whether to offer creating the secrets.

## RTL fingerprint (slang)

| Variable | Default | Effect |
|---|---|---|
| `SLANG_BIN` | unset | Path to the slang binary. Wins over `slang` on `PATH` and `tools/slang/slang` in the checkout. A path that is not a file counts as slang missing |

`hammer/vlsi/rtl_check.py` looks for slang in that order and requires version
11.0, pinned in code by its `SLANG_VERSION` constant (the env var of the same
name only steers setup). When slang is missing, reports another version,
cannot parse the RTL, or the design nests too deeply, the fingerprint falls
back to a byte hash of the RTL files, defines and top module, and the stage
log warns. Under the fallback, comment and whitespace edits count as RTL
changes and files pulled in by `` `include `` are not covered. A byte hash
never equals a slang hash, so switching between the two reads as an RTL
change once.

## Launcher and login

Read by `sledgehammer`, `scripts/airflow-standalone-ldap.py`, `venv.sh` and
the pgAdmin plugin. `studio` reads the stack env file too.

| Variable | Default | Effect |
|---|---|---|
| `SLEDGE_ENV_FILE` | `~/.sledgehammer/env.sh`, then the nearest `stack_env.sh` in or above the current directory | Stack env file that the flow commands (`run`, `status`, `runs` and make-target names), `sledgehammer buildfile` (alias `build`), the Airflow passthrough (such as `sledgehammer dags list` or `db migrate`) and every `studio` command source. Exported values win, and nothing is sourced when `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` and `HAMMER_PG_HOST` are both set. The passthrough sources it before the secrets and keeps `AIRFLOW_HOME` pinned to the checkout even when the file sets another; the flow commands and `buildfile` take `AIRFLOW_HOME` from it, else the checkout. `studio` names the file it read on stderr. Only the launch (`sledgehammer` alone, `standalone`, `up`, `start`, `serve`) and the help skip it. A value that is not a file falls through to the defaults |
| `SLEDGE_SECRETS_FILE` | `.sledgehammer/airflow-secrets.env.gpg` in the checkout | GPG secrets file the launcher, the passthrough and flow commands, `uv_setup.sh` and the secrets scripts use. Not decrypted when `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` is already set, by you or, for the passthrough and flow commands, by the stack env file. `venv.sh` ignores it and decrypts the default file, so a launch from a terminal uses that file when it exists, unless `SLEDGE_NO_AUTO_SECRETS` is set |
| `SLEDGE_NO_AUTO_SECRETS` | unset | Set (any value): `source venv.sh` at a terminal no longer decrypts the secrets up front. Its `airflow` function still decrypts on first use |
| `AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE` | `.sledgehammer/airflow-ui-password.json` in the checkout | File holding the web UI password. The launch makes you (`$USER`) the UI's only login, adds a random password for you when the file has none, and keeps the file readable only by you. Edit the value to change the password; the next login uses it |
| `SLEDGE_PORT` | `auto` | `auto` takes the first free port from the `[api] port` (`AIRFLOW__API__PORT`, else `airflow.cfg`, else 8080) up to 19 above it. A number pins the port, and the launch refuses if it is taken |
| `SLEDGE_DRYRUN` | unset | Set (any value): print the launch command, the Airflow passthrough command or the `make` command `sledgehammer buildfile` would run, instead of running it. `run`, `status`, `runs` and the make-target names ignore it |
| `SLEDGE_NO_PGADMIN` | unset | Set (any value): do not auto-start pgAdmin |
| `PGADMIN_PORT` | `5050` | First port the launcher and the nav link's probe try for pgAdmin |
| `PGADMIN_URL` | unset | Fixed URL for the pgAdmin nav link instead of probing |
| `SLEDGE_ALLOW_MULTI_SCHEDULER` | unset | Set (any value): skip the warning about another live scheduler on the same metadata DB |

A launch, through `sledgehammer` or the script itself, fills in what you left
unset: `HAMMER_PD_CACHE=1`, and `HAMMER_PG_HOST`, `HAMMER_PG_PORT` and
`HAMMER_PG_USER` from the Airflow metadata connection. It always pins
`AIRFLOW_HOME` to the checkout and sets `AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS`
to `$USER:admin`. DAG tasks inherit all of these.

## PD cache and database

| Variable | Default | Effect |
|---|---|---|
| `HAMMER_PD_CACHE` | unset (off); `1` under the launcher | Turns the stage cache on. Read in any case, with surrounding spaces ignored: unset, empty, `0`, `false`, `no` or `off` leave it to `vlsi.pd_cache.enabled`. Any other value turns it on and beats the key. The variable alone, not the key, also mirrors `master_database.json` writes to Postgres on DAG runs, and the mirror reads it the same way, so an off value in any spelling stops it too |
| `HAMMER_PD_CACHE_LEDGER` | on | Records each cache decision in `hammer_poc.pd_cache_events` for `studio time-saved`. `0` turns it off; the per-run summary still prints. Config key `vlsi.pd_cache.ledger_enabled` |
| `HAMMER_PD_PROJECT` | unset | Project label on ledger rows and database checkpoints. Beats the trigger conf `project`, which beats `vlsi.pd_cache.project` |
| `HAMMER_PG_HOST` | host of the Airflow conn, else `localhost` | Host of the cache database (`studio`, the cache, checkpoints) |
| `HAMMER_PG_PORT` | port of the Airflow conn, else `5433` | Its port |
| `HAMMER_PG_DB` | `sledgehammer_studio` | Its database. Never taken from the Airflow conn |
| `HAMMER_PG_USER` | user of the Airflow conn, else your OS user | Its role |
| `HAMMER_PG_PASSWORD` | password of the Airflow conn | Its password. Without one, libpq's `PGPASSWORD`, then `PGPASSFILE` or `~/.pgpass`; with none of them, connecting fails, and most `studio` commands exit 1 with `studio: database unavailable: No Postgres password found. ...` |
| `HAMMER_PG_CONNECT_TIMEOUT` | `10` | Seconds each connection to the cache database waits for the server before it gives up, so a cache database that is down cannot hang a `studio` command or a stage. It also bounds the triggering-user lookup in the Airflow metadata DB. A whole number: unset or empty means 10, and `0` waits forever |
| `SLEDGE_EXTRA_METADATA_CONNS` | unset | Comma-separated Postgres URIs of other Airflow metadata DBs. `studio time-saved` adds their runs when it measures parallel savings and failed-run time |

"The Airflow conn" is `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`, else
`sql_alchemy_conn` in `airflow.cfg` (under `$AIRFLOW_HOME`, then the current
directory, then `~/airflow`).

## Sub-step resume

| Variable | Default | Effect |
|---|---|---|
| `HAMMER_SUBSTEP_RESUME` | on | Resumes a failed or paused syn or par run from its newest tool-confirmed checkpoint. `0` turns it off. Config key `vlsi.substep_resume.enabled` |
| `HAMMER_DB_CHECKPOINTS` | on | Pushes checkpoints to Postgres and fetches them when no local one fits. `0` turns it off; local resume keeps working. Config key `vlsi.substep_resume.db_checkpoints` |
| `HAMMER_CHECKPOINT_STREAM` | on | Pushes each new checkpoint while syn or par runs, not only on failure. Only `0`, `false` and `no` turn it off (`off` does not). Needs both switches above on |
| `HAMMER_CHECKPOINT_STREAM_SECS` | `300` | Seconds between streaming checks. A value that is not a number means 300 |

## Error scan

| Variable | Default | Effect |
|---|---|---|
| `HAMMER_ERROR_SCAN` | on | After a syn or par run the tool called clean, counts the `**ERROR` lines in `genus.log` or `innovus.log` and logs a verdict. `0` turns it off. Config key `vlsi.error_scan.enabled` |

The other keys have no variable:

| Key | Default | Effect |
|---|---|---|
| `vlsi.error_scan.ignore` | `[]` | Reviewed codes (for example `IMPLF-24`), counted apart and left out of the verdict |
| `vlsi.error_scan.fail_on` | `[]` | Codes that fail the stage even though the tool exited cleanly; a run that fails it is never stored in the PD cache |

A change to either list applies the next time syn or par runs or is restored
from the PD cache. It does not rerun a stage the dependency check skips as
unchanged, so to apply a new list to a finished stage, rerun it with
`sledgehammer syn --force` or `sledgehammer par --force`.

## Notifications

| Variable | Default | Effect |
|---|---|---|
| `SLEDGE_SMTP_USER` | unset | SMTP login and default sender. Unset, no mail is sent and `studio notify-test` exits 2 |
| `SLEDGE_SMTP_PASSWORD_FILE` | unset | File holding the SMTP password. Unset, no mail is sent and `studio notify-test` exits 2 |
| `SLEDGE_SMTP_HOST` | `smtp.gmail.com` | SMTP server, reached with STARTTLS |
| `SLEDGE_SMTP_PORT` | `587` | SMTP port |
| `SLEDGE_SMTP_FROM` | `SLEDGE_SMTP_USER` | From address |
| `SLEDGE_NOTIFY_TO` | unset | Sends every completion mail to this address instead of the user's registered one (`studio notify-email`). A trigger conf `notify: false` still silences a run |
| `SLEDGE_METADATA_CONN` | unset | Metadata DB URI for DAG tasks and callbacks, which Airflow 3 hands a decoy conn. They look up the triggering user and run states through it |
| `SLEDGE_METADATA_CONN_FILE` | `.sledge_metadata_conn` beside `SLEDGE_SMTP_PASSWORD_FILE` | File the launcher writes the Postgres metadata conn into (mode 600) at every start, for tasks and callbacks to read |
| `SLEDGEHAMMER_SMTP_USER` | unset | Default `--user` for `studio smtp-setup` |

The launcher loads `SLEDGE_SMTP_*` from `.sledgehammer/smtp.env` in the
checkout; exported values win. `studio smtp-setup` writes
`~/.sledgehammer/smtp.env` instead, which the launcher does not read, so export
it or link it into the checkout. `studio notify-test` reads that file only when
`SLEDGE_SMTP_USER` is not already set, so exporting the user alone skips the
file and the test stops with exit 2.

## DAG runs and workspaces

| Variable | Default | Effect |
|---|---|---|
| `HAMMER_DAGS_FOLDER` | unset | Override for the DAG folder. Set, it beats Airflow's own setting in the lookup below, so a leftover value links DAGs where Airflow does not look. `vlsi.core.airflow_dags_folder` still beats it for the build's link step |
| `AIRFLOW__CORE__DAGS_FOLDER` | `dags_folder` in `airflow.cfg`, else `$AIRFLOW_HOME/dags` | Airflow's DAG folder. Unless `HAMMER_DAGS_FOLDER` is set, `sledgehammer run` looks there, the build links generated DAGs there when `vlsi.core.airflow_dags_folder` is unset, and so does `studio make-dag` without `--dags-folder`. The stack env file can supply it to `sledgehammer` and `studio` commands but not to the launch: the server you start with `sledgehammer` sees it only when you export it |
| `OBJ_DIR` | unset | `sledgehammer run` and the make-target names use it as the obj_dir when neither `--obj_dir`, `-t` nor a registered DAG for the current directory gives one, before falling back to the only directory under `build/`. They never run make to find it |
| `HAMMER_WORKSPACE` | unset | Named workspace (`studio workspace-set --name`) a DAG run uses when the trigger conf names none. Set, it routes the DAG owner's runs through the workspace table too |
| `HAMMER_NO_PER_USER_WORKSPACE` | unset | Set (any value): DAG tasks keep the DAG's baked `OBJ_DIR` instead of moving another user's run into their workspace |

One lookup finds the DAG folder for the build's link step (when
`vlsi.core.airflow_dags_folder` is unset), `sledgehammer run`, the check
`sledgehammer buildfile` makes after the build, and `studio make-dag` without
`--dags-folder`. It takes the first of `$HAMMER_DAGS_FOLDER`,
`$AIRFLOW__CORE__DAGS_FOLDER`, `[core] dags_folder` in the Airflow config file,
then `$AIRFLOW_HOME/dags`. The config file is `$AIRFLOW_CONFIG`, else
`$AIRFLOW_HOME/airflow.cfg`, and `{AIRFLOW_HOME}` and `~` in its `dags_folder`
are expanded. `AIRFLOW_HOME` defaults to the checkout. The lookup reads
`airflow.cfg` itself and does not ask the Airflow CLI. `sledgehammer run` never
reads `vlsi.core.airflow_dags_folder`, so a project config that points that
key elsewhere leaves `run` unable to find the DAG. `sledgehammer buildfile`
warns when it finds no DAG for the current directory in the folder after the
build.

### Set by the generated DAG

Each DAG task sets these before it runs hammer, in `_resolve_workspace_obj_dir`
(`hammer/shell/hammer_vlsi.py`). They are listed for reference: do not set them
by hand.

| Variable | Holds | Read by |
|---|---|---|
| `HAMMER_AIRFLOW_DAG_ID` | The DAG id | Provenance on blobs, ledger rows and checkpoints |
| `HAMMER_AIRFLOW_RUN_ID` | The DAG run id | Provenance, the per-run summary, the run-lock check before a cache restore |
| `HAMMER_AIRFLOW_TRIGGERING_USER` | Who triggered the run, else `$USER` | Provenance |
| `HAMMER_AIRFLOW_DESIGN` | Design name (the build directory's basename) | Provenance, the `master_database.json` mirror, `studio checkpoints-push` |
| `HAMMER_AIRFLOW_WORKSPACE` | Workspace root the run's `OBJ_DIR` sits under | Provenance |
| `HAMMER_AIRFLOW_WORKSPACE_NAME` | Named workspace, when the run was routed through the table | Nothing reads it today |

The same step copies the trigger conf `project` into `HAMMER_PD_PROJECT` when
that is unset, and when it places the run it sets `OBJ_DIR` and clears
`HAMMER_D_MK`.

## Debug and test

| Variable | Default | Effect |
|---|---|---|
| `HAMMER_CHECKPOINT_DEBUG` | unset | Set (any value): print the traceback when a checkpoint push or clear fails, which is otherwise silent |
| `HAMMER_PD_COLLAT_DEBUG` | unset | File path. Every collateral fingerprint appends the `path:size:mtime` lines it hashed |

## Other tools

| Variable | Default | Effect |
|---|---|---|
| `SRAM22_ROOT` | `/tools/commercial/skywater/local/sram22_sky130_macros` | Where `studio design-register`, `augment` and `make-dag` look up sram22 macros |
| `AUTOTA_API_KEY` | unset | Gemini API key for AutoTA. Beats `hammer/shell/autoTA/.api_key`, then an `export AUTOTA_API_KEY=` line in `~/.bashrc` |
| `AUTOTA_PDK_PATTERNS` | unset | Extra comma-separated regexes that AutoTA masks as `PDK` in the log text it extracts |
| `AUTOTA_CONDA` | `hammer/shell/autoTA/conda/bin/conda`, else `conda` | Conda that AutoTA's auto-setup activates when its imports fail |

## Upstream Hammer

`HAMMER_ENVIRONMENT_CONFIGS`, `HAMMER_DRIVER_OBJ_DIR`, `HAMMER_DATABASE`,
`HAMMER_PYYAML_PATH`, the `hammer.d` make variables (`HAMMER_EXEC`,
`HAMMER_EXTRA_ARGS`, `HAMMER_DEPENDENCIES`, `HAMMER_*_DEPENDENCIES`), and
`HAMMER_D_MK`, the Makefile's path to `hammer.d`, behave as in upstream Hammer
and are not covered here.
