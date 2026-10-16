# Airflow LDAP login

How SledgeHammer replaces Airflow's default "shared admin password" login with proper per-user LDAP authentication. Each person logs into the Airflow webserver with their own institutional credentials (at Berkeley EECS, that's their EECS account). No shared accounts, separate sessions per user.

A checkout already ships set up this way: `./scripts/uv_setup.sh` does step 1, and the committed `airflow.cfg`, `webserver_config.py` and launcher cover steps 2 to 4. Use the steps to check a setup, to point it at another LDAP server, or to debug a login.

Skip this if you're only using the cache CLI (`studio`) and don't run the Airflow webserver. The cache layer doesn't care about LDAP.

## What you need before starting

- An LDAP server you can reach. Berkeley EECS uses `ldaps://ldap.eecs.berkeley.edu`; substitute your institution's URL if you're elsewhere.
- The Airflow stack already running on your machine (see DATABASE_SETUP.md if you haven't set that up yet).
- Permission to edit `airflow.cfg` and `webserver_config.py` in `$AIRFLOW_HOME`.

## 1. Install FAB and python-ldap

Airflow 3 ships with `SimpleAuthManager` as the default. To use LDAP you need the FAB provider package plus a Python LDAP client. The Python LDAP client is a C extension, so it also needs OpenLDAP development headers (`lber.h` and friends) at build time.

`./scripts/uv_setup.sh` already does this step unless you ran it with `SLEDGE_NO_LDAP=1`. It downloads the headers with `dnf download openldap-devel` into `~/ldap_local`, so it needs no root, and builds python-ldap from source against them. Do not point the build at conda's OpenLDAP: a conda library on the linker path gets baked into the build and breaks it in later shells. To redo the step by hand, from a shell with no conda environment active:

```bash
# OpenLDAP headers, no root needed (only once):
mkdir -p ~/ldap_local
(cd "$(mktemp -d)" && dnf download openldap-devel && \
    rpm2cpio openldap-devel-*x86_64.rpm | (cd ~/ldap_local && cpio -idmv))
ln -sf /lib64/libldap.so.2 ~/ldap_local/usr/lib64/libldap.so
ln -sf /lib64/liblber.so.2 ~/ldap_local/usr/lib64/liblber.so
# or `apt install libldap2-dev libsasl2-dev` if you have root

# Python packages:
source ./venv.sh
export PATH=$(pwd)/.venv/bin:$PATH
uv pip install "apache-airflow-providers-fab==3.6.3" "sqlalchemy>=2.0.16,<2.1"
CPPFLAGS="-I$HOME/ldap_local/usr/include -I/usr/include" \
LDFLAGS="-L$HOME/ldap_local/usr/lib64 -L/lib64" \
    uv pip install "python-ldap==3.4.7" --no-binary python-ldap
```

Verify:

```bash
python -c "import flask_appbuilder, ldap; print('OK')"
```

## 2. Switch Airflow's auth_manager

Two settings in `airflow.cfg`:

```ini
[core]
auth_manager = airflow.providers.fab.auth_manager.fab_auth_manager.FabAuthManager
```

```ini
[api_auth]
jwt_issuer = airflow-sledgehammer
```

The `jwt_issuer` setting is mandatory. Without it, login succeeds against LDAP but the post-login JWT signing step fails with `TypeError: Issuer (iss) must be a string`. The value just has to be a non-empty string; a project identifier is fine.

## 3. Drop in `webserver_config.py`

This file lives at the root of `$AIRFLOW_HOME`. It tells FAB how to talk to LDAP. The version checked into this repo is configured for EECS LDAP. For another institution, swap the `AUTH_LDAP_SERVER` and `AUTH_LDAP_SEARCH` values.

The non-obvious bit: EECS LDAP keys each user record by `eecsDWRosterID=<number>`, not by username. You can't construct a bind DN from "lawrencejrhee" directly. FAB has to do "search-and-bind" instead: connect anonymously, look up the user's actual DN by uid, then bind as that DN with the user's password.

FAB's documented way to enable search-and-bind is `AUTH_LDAP_BIND_USER` plus `AUTH_LDAP_BIND_PASSWORD` (a service account). EECS doesn't hand out service accounts, but it does allow anonymous search. The included config monkey-patches FAB's `_ldap_bind_indirect` to skip the service account bind and go straight to anonymous. FAB only enters the search-bind flow when `AUTH_LDAP_BIND_USER` is set, so the config sets a dummy one; without the patch, FAB would try to bind as that dummy user and fail.

To point at a different LDAP server, edit:

```python
AUTH_LDAP_SERVER = "ldaps://your-ldap-host"
AUTH_LDAP_SEARCH = "dc=your,dc=domain"
AUTH_LDAP_UID_FIELD = "uid"   # whatever attribute your usernames are stored under
```

If your LDAP server is keyed by uid normally (i.e. user DNs look like `uid=alice,ou=people,dc=...`), you don't need the monkey patch. Remove the `_ldap_bind_indirect` patch (keep the whitelist one), drop `AUTH_LDAP_BIND_USER` / `AUTH_LDAP_BIND_PASSWORD`, and set `AUTH_LDAP_USERNAME_FORMAT` (e.g. `uid=%s,ou=people,dc=...`) so FAB's direct bind can build the DN. FAB's default flow handles that case.

## 4. Don't use `airflow standalone`: use the included replacement

`airflow standalone` hardcodes SimpleAuthManager and overrides whatever you set in `airflow.cfg`. The relevant lines in `airflow/cli/commands/standalone_command.py`:

```python
# Make sure we're using SimpleAuthManager
simple_auth_manager_classpath = (
    "airflow.api_fastapi.auth.managers.simple.simple_auth_manager.SimpleAuthManager"
)
if conf.get("core", "auth_manager") != simple_auth_manager_classpath:
    self.print_output("standalone", "Forcing auth manager to SimpleAuthManager")
    env["AIRFLOW__CORE__AUTH_MANAGER"] = simple_auth_manager_classpath
    os.environ["AIRFLOW__CORE__AUTH_MANAGER"] = simple_auth_manager_classpath  # also in this process!
```

It also tries to look up SimpleAuthManager-specific user info, which crashes when `auth_manager` is FabAuthManager. So `airflow standalone` is unusable with LDAP.

The repo includes a drop-in replacement at `scripts/airflow-standalone-ldap.py`. It subclasses `StandaloneCommand`, removes the auth_manager override, and skips the SimpleAuthManager user-info step. Everything else (subprocess management, colored output for each component, Ctrl+C cleanup) is inherited unchanged. Start it with `sledgehammer` and no arguments, which sources `venv.sh` in this checkout and runs the script:

```bash
unset AIRFLOW__CORE__AUTH_MANAGER     # in case it leaked into this shell earlier
sledgehammer
```

Before Airflow starts, the script also loads the encrypted secrets (see SECRETS.md), picks a free port starting from the `[api] port` in `airflow.cfg` (8081), and checks the login whitelist.

You should see the familiar four components come up:

```
standalone | Starting Airflow Standalone
standalone | Respecting configured auth_manager: not forcing SimpleAuthManager
api-server | Uvicorn running on http://0.0.0.0:8081
scheduler  | ...
dag-processor | ...
triggerer  | ...
```

Ctrl+C shuts everything down cleanly.

### Alternative: start components individually

If for any reason the wrapper script doesn't work, the equivalent manual setup is:

```bash
source ./venv.sh
export PATH=$(pwd)/.venv/bin:$PATH
unset AIRFLOW__CORE__AUTH_MANAGER

airflow api-server -H 0.0.0.0 -p 8081 &     # the web UI; this is where login happens
airflow scheduler &                           # schedules DAG runs
airflow dag-processor &                       # parses DAGs
airflow triggerer &                           # optional, for deferrable tasks
```

You'll have to manage their lifecycle yourself; Ctrl+C doesn't stop background jobs. The wrapper script is easier.

## 5. Test the login

Point a browser at `http://localhost:8081/auth/login` (with an SSH tunnel if your browser isn't on the same host as the api-server). If 8081 was taken, the launcher picked the next free port and printed it.

- Username: your institutional UID (e.g. `lawrencejrhee` for EECS, just the uid, no domain)
- Password: your institutional password

Your uid has to be on the login whitelist unless you started the server (see "Login whitelist" below). The launcher also turns on the TOTP second factor (`SLEDGE_2FA=1`), so after the password you scan a QR code on your first login and enter an authenticator code on later ones. See AIRFLOW_2FA_SETUP.md.

On success the UI loads with your real name in the top-right corner, and a new row appears in the `ab_user` table of the Airflow metadata DB, populated from your LDAP attributes (first name, last name, email).

## What the FAB auth flow actually does

For reference if you're debugging:

1. The patched `auth_user_ldap` in `webserver_config.py` checks the login whitelist and stops there if the uid isn't allowed (see below).
2. FAB sees `AUTH_LDAP_BIND_USER` is set → enters "Indirect Search Bind" flow.
3. Calls patched `_ldap_bind_indirect` → does `con.simple_bind_s()` (anonymous).
4. Runs `_search_ldap` with filter `(uid=lawrencejrhee)` → finds the DN.
5. Calls `_ldap_bind(user_dn, password)` → tries to bind as that DN. Succeeds iff the password is right.
6. On first successful login, FAB creates an `ab_user` row from the LDAP attributes (uid, first/last/email) with the `AUTH_USER_REGISTRATION_ROLE` role (default `User`).
7. With 2FA on, the user enters an authenticator code next. Then Airflow signs a JWT with `iss=airflow-sledgehammer` (or whatever you set), sets a session cookie, redirects to the UI.

## Login whitelist (who can log in)

LDAP lets *anyone* with an EECS account reach the login page, so a **whitelist**
decides who is actually allowed in. Anyone not on it is rejected at login,
except the server's owner and the uids in `AIRFLOW_ALLOWED_UIDS` (see below). A
valid EECS password is not enough: no bind, no session, no account.

The list lives in Postgres (`hammer_poc.login_whitelist`), **not** in git, and
you manage it live with one command (no restart, no commit):

```bash
studio whitelist <eecs-uid>           # allow someone (in on their next login)
studio whitelist                      # list everyone allowed
studio whitelist --remove <eecs-uid>  # revoke
```

Whitelisting does not give anyone a workspace. If the new user will trigger DAGs
they did not generate, register a workspace for them too (see "Registering a
user's workspace" below):

```bash
studio workspace-set <eecs-uid> /path/to/their/workspace_root
```

### First-time rollout (do not lock yourself out)

The OS user who started the server is always let in. That covers you only when
your OS username is your EECS uid (as on BWRC).

1. **Add yourself first:** `studio whitelist <your-eecs-uid>` (also creates the table).
2. **Safety net (optional):** `export AIRFLOW_ALLOWED_UIDS=<your-uid>`, an emergency
   bootstrap so you can still get in if the whitelist DB is ever unreachable.
3. **Then** start Airflow.

The launcher checks the list when it starts. If the whitelist is empty, it says
only the owner can log in and starts anyway. It **refuses to start** only when
it also cannot read the OS username and no `AIRFLOW_ALLOWED_UIDS` is set. If it
cannot reach the whitelist at all, it prints a warning and starts.

## Promoting a user to admin

By default, new users get the `User` role: view DAGs, trigger runs, but can't manage connections, variables, or other users. They also get a `Member` role (created by the launcher), which only adds menu entries and the plugin links. The OS user who started the server is the exception: the login hook and the launcher both give that user `Admin` automatically.

To promote someone to `Admin`, use `studio admin`:

```bash
studio admin <uid>             # grant Admin
studio admin                   # list everyone with Admin
studio admin --remove <uid>    # revoke Admin
```

The user must log in once first, because that login creates their Airflow account. Until then, `studio admin <uid>` stops with `No Airflow user '<uid>' yet`. After a grant, they log out and back in to pick up the role.

`studio admin` finds the Airflow metadata DB on its own while a server you started is up, while the secrets are loaded, or when your stack env file sets `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` (every `studio` command reads that file; see [CLI_REFERENCE.md](CLI_REFERENCE.md#the-environment-file)). When none of these is true, pass the connection with `--conn`:

```bash
studio admin <uid> --conn postgresql://USER:PW@HOST:PORT/DBNAME
```

**Never take `ab_user_role.id` from `MAX(id)+1`.** `studio admin` takes it from `nextval('ab_user_role_id_seq')`. A hand-written `INSERT ... (id, ...) VALUES (MAX(id)+1, ...)` leaves the sequence behind the table, and the next LDAP user to log in for the first time will crash with `duplicate key value violates unique constraint "ab_user_role_pkey"` in the api-server log, because FAB's first-login provisioning uses `nextval()` and collides with your manual row.

The Airflow 2.x CLI shortcut for this, `airflow users add-role -u <user> -r Admin`, still exists in Airflow 3: the FAB provider adds it when FAB is the auth manager. The UI under `Security → List Users` still works if you already have admin access.

## Per-user build directory isolation

The shared Airflow stack runs as one LocalExecutor under one OS user (whoever started the server). Without extra protection, every task triggered through the UI uses the build directory baked into the DAG when it was generated, regardless of which LDAP user clicked the button. The practical consequence is that user B's `syn` or `par` run overwrites user A's working build directory.

The isolation works through a `hammer_poc.user_workspaces` table keyed by LDAP username. Every Hammer task in a generated DAG resolves the triggering user's workspace and pins `OBJ_DIR` to `<workspace_root>/<design>` before the tool runs. After that, `syn`, `par`, and friends only ever read and write that directory. The Postgres `pd_blobs` cache is the one thing that intentionally crosses user boundaries: user B's `syn` can restore from a tarball user A produced, which is the whole point of the project.

### Registering a user's workspace

Only `studio workspace-set` creates a workspace row: login, runs and `studio workspace-show` never do. Each teammate who will trigger a DAG they did not generate, or pass a named workspace, needs one registered with `studio workspace-set`. The DAG's owner needs none for their own DAG: their runs use the DAG's own `OBJ_DIR`.

If an unregistered teammate triggers someone else's DAG, the resolver raises this error:

```
no workspace registered for user '<uid>' (workspace 'default'). Register one first:  studio workspace-set <uid> /path/to/their/build
```

The generated DAG catches it and logs this line in the task log, with the error above inside the parentheses:

```
[workspace] resolver unavailable (...); using gen-time OBJ_DIR
```

Register every teammate before they trigger someone else's DAG.

To see what's registered:

```bash
studio workspace-list              # every registered workspace
studio workspace-show <username>   # one user's workspace root
```

`studio workspace-show` only reads. For a user with no registration it prints this on stderr and exits 1, without registering anything:

```
No workspace 'default' registered for '<username>'. Set one: studio workspace-set <username> /path
```

To set or change a user's workspace:

```bash
studio workspace-set <username> /path/to/their/workspace_root
```

The daemon user has to have write permission to that path. To remove a user's registrations (all of them; add `--name NAME` to remove just that one):

```bash
studio workspace-unset <username>
```

The user stays unregistered until a new `studio workspace-set`: neither runs nor `studio workspace-show` register them again. Run `studio workspace-set` for them before their next run of a DAG they did not generate.

### Verifying the isolation

You can prove the isolation works without coordinating two LDAP users. There's a self-contained test script:

```bash
./scripts/test_per_user_workspace.sh
```

It registers two sandbox usernames pointed at `/tmp/sledgehammer_test/*`, seeds marker files in each, then drives `AIRFlow_synpar.clean()` programmatically with a fabricated DAG run context (the same code path the real scheduler takes). It asserts that the triggering user's directory is wiped and the other user's directory is untouched, then cleans up after itself. Exit code 0 means isolation is working.

Two passes like this should appear:

```
=== Test 1: __test_bob triggers clean ===
[user-workspace] triggering_user='__test_bob' -> OBJ_DIR=/tmp/sledgehammer_test/__test_bob/gcd
  OK: /tmp/sledgehammer_test/__test_alice/gcd/marker.txt still present
  OK: /tmp/sledgehammer_test/__test_bob/gcd/marker.txt is gone
```

Run it after any change to `_resolve_workspace_obj_dir`, `pd_store.lookup_triggering_user`, or any `AIRFlow*.__init__`.

### What this does and doesn't isolate

This table assumes every teammate who runs someone else's DAG is registered.

| Concern | Behavior |
|---|---|
| `syn`/`par` write only to the triggering user's dir | Yes (the whole point) |
| File ownership on the filesystem | Still the daemon user: `LocalExecutor` runs all tasks as one OS user |
| Cache (`pd_blobs`) | Shared across users, by design |
| Triggering-user shown in run history | Yes (Airflow's `dag_run.triggering_user_name`) |
| Two users triggering the same DAG at once | Safe: different `OBJ_DIR`s, no filesystem race |

If you need OS-level file ownership (each user's build files owned by them, not by the daemon), the shared stack isn't enough. That requires either per-user Airflow deployments (each user starts their own with `sledgehammer`, on a different port) or a CeleryExecutor with per-user workers. The shared stack is meant for demos and shared cache benefits; serious per-user usage is the per-stack model.

## Troubleshooting

**Login page hangs forever.** The api-server isn't running, or your browser's `localhost` resolves to a different machine. From a remote machine, `ssh -L 8081:localhost:8081 user@server` and load `http://localhost:8081/` (the launcher prints the exact tunnel command for its port).

**Login form returns "Invalid login. Please try again." right away.** Check the api-server log first. `LOGIN REJECTED (not whitelisted): '<uid>'` on its own means the uid isn't on the whitelist: add it with `studio whitelist <uid>`. If `whitelist DB check failed for '<uid>' (...) -- denying.` comes just before it, the whitelist DB couldn't be reached, and the parentheses give the reason. When the database host does not answer at all, the form waits `HAMMER_PG_CONNECT_TIMEOUT` seconds (10 by default) before this rejection, and the reason is a timeout (for example `timeout expired`). Otherwise either the LDAP server is unreachable, or the bind DN FAB constructed is wrong. The most useful clue is the `result:` code in any `ldap.LDAPError`:

- `result: 34`: Invalid DN syntax. FAB is binding with something that's not a valid DN. Usually means the patched `_ldap_bind_indirect` isn't installed, or `AUTH_LDAP_USERNAME_FORMAT` would help.
- `result: 49`: Invalid credentials. Search worked, bind failed. Wrong password, or the LDAP server is rejecting the bind for some other reason.
- `result: 32`: No such object. The search base doesn't exist. Check `AUTH_LDAP_SEARCH`. (A wrong `AUTH_LDAP_UID_FIELD` gives no error code at all: the search just finds nobody.)

**Login succeeds but lands on "Something bad has happened. TypeError: Issuer (iss) must be a string."** `jwt_issuer` isn't set in `[api_auth]`. See step 2.

**`ab_user` table doesn't exist.** Run `sledgehammer db migrate` to create the FAB tables. It runs `airflow db migrate` with this checkout as `AIRFLOW_HOME` and your stack env file and secrets loaded first.

**Login succeeds for one user but the second user gets a 500 with `duplicate key value violates unique constraint "ab_user_role_pkey"`.** Someone promoted a user to admin by hand with `MAX(id)+1` instead of `nextval('ab_user_role_id_seq')`. See "Promoting a user to admin" above. Fix is to realign the sequence:

```sql
SELECT setval('ab_user_role_id_seq', COALESCE((SELECT MAX(id) FROM ab_user_role), 0) + 1, false);
```

Run the same for `ab_user_id_seq` and `ab_register_user_id_seq` if those drifted too.

**A task fails with `hammer-shell does not appear to be on the path`.** `.venv/bin` wasn't on PATH when Airflow started, so the task can't find `hammer-shell-test`. The launcher puts this checkout's `.venv/bin` first for you. For the manual recipe, export it before starting the components: `export PATH=$(pwd)/.venv/bin:$PATH`.

## Files in this repo for LDAP

- `webserver_config.py`: checked in. Configured for EECS LDAP. Edit for other institutions.
- `scripts/airflow-standalone-ldap.py`: checked in. The drop-in replacement for `airflow standalone` that respects your configured auth_manager. `sledgehammer` with no arguments runs it.
- `scripts/test_per_user_workspace.sh`: checked in. Regression test for the per-user workspace isolation.
- `airflow.cfg`: checked in, with its secret fields blank (see SECRETS.md). It already carries the two settings from step 2, `auth_manager` and `jwt_issuer`.
- This file (`AIRFLOW_LDAP_SETUP.md`): what you're reading.
