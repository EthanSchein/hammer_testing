#  hammer-vlsi
#  CLI script - by default, it just uses the default CLIDriver.
#
#  See LICENSE for licence details.
'''
from hammer.vlsi import CLIDriver

def main():
    CLIDriver().main()

'''
import re
import os
import shlex
import subprocess
import sys
import json
import datetime

# RHEL 9 workaround: Cadence tools (Genus, Innovus) need libnsl.so.1
_libnsl_path = os.path.expanduser("~/libnsl_local/usr/lib64")
if os.path.isfile(os.path.join(_libnsl_path, "libnsl.so.1")):
    _ld = os.environ.get("LD_LIBRARY_PATH", "")
    if _libnsl_path not in _ld:
        os.environ["LD_LIBRARY_PATH"] = f"{_libnsl_path}:{_ld}" if _ld else _libnsl_path



# Add the parent directory to the Python path to allow imports from 'vlsi'
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'vlsi')))

from hammer.vlsi import CLIDriver
#import pdb
#pdb.set_trace()


def run_cli_driver():
    """Wrapper around CLIDriver().main() that's safe to call from Airflow tasks.

    CLIDriver.main() calls sys.exit() on its way out, which would otherwise
    kill the Airflow worker process. We catch SystemExit and re-raise as a
    plain RuntimeError on nonzero exit codes; on exit code 0 we just return.
    """
    try:
        CLIDriver().main()
    except SystemExit as e:
        if e.code != 0 and e.code is not None:
            raise RuntimeError(f"CLIDriver.main() failed with exit code {e.code}")


# The completion-email callback lives in pd_notify, which has no Airflow imports
# so the cluster policy can import it without building DAGs. Imported here too so
# the @dag decorators below can wire it as their on_success/on_failure callback.
# The metadata-DB lookup it relied on now lives in pd_store.lookup_triggering_user.
from hammer.vlsi.pd_notify import notify_flow_complete as _notify_flow_complete


def _resolve_workspace_obj_dir(context, design, default_obj_dir=None, gen_user=None,
                               claim=True):
    """
    Resolve and set OBJ_DIR in os.environ for the user who triggered this DAG run.

    Looks up the triggering user (the Airflow username), then resolves
    their per-user workspace root from ``hammer_poc.user_workspaces`` and pins
    ``OBJ_DIR`` to ``<workspace_root>/<design>``.

    Resolution order for the triggering user:
        1. ``dag_run.triggering_user_name`` (if exposed by the SDK proxy)
        2. SQL lookup against ``dag_run`` table using ``dag_id`` + ``run_id``
           (the canonical source - works in Airflow 3 where the proxy hides
           the field)
        3. ``$USER`` env var (fallback; only used outside Airflow)

    This is the safety mechanism that prevents one user's "clean" task from
    nuking another user's build directory. Every AIRFlow*() instantiation
    accepts a ``context=...`` kwarg and calls this before reading ``OBJ_DIR``,
    so there is no path in the DAG code that can ever touch someone else's
    workspace.

    Returns the resolved OBJ_DIR string for logging.
    """
    user = None
    dag_id = None
    run_id = None
    ws_name = None
    proj_name = None
    try:
        if context is not None:
            dag_run = context.get("dag_run") if isinstance(context, dict) else getattr(context, "dag_run", None)
            if dag_run is not None:
                user = getattr(dag_run, "triggering_user_name", None)
                dag_id = getattr(dag_run, "dag_id", None)
                run_id = getattr(dag_run, "run_id", None)
                # Which NAMED workspace this run targets. This is what lets one
                # user operate in several workspaces at the same time: trigger
                # the DAG with conf={"workspace": "<name>"} and each run lands
                # in its own <workspace_root>/<design>. Defaults to 'default'.
                conf = getattr(dag_run, "conf", None)
                if isinstance(conf, dict):
                    ws_name = conf.get("workspace") or conf.get("workspace_name")
                    # Optional project label for the time-saved tracker: trigger
                    # the DAG with conf={"project": "<name>"} to bucket this run
                    # under a named project/tapeout.
                    proj_name = conf.get("project")
    except Exception:
        pass

    # Airflow 3 SDK proxy hides triggering_user_name. Fall back to a SQL
    # lookup keyed by (dag_id, run_id) - that always works.
    if not user:
        from hammer.vlsi import pd_store
        user = pd_store.lookup_triggering_user(dag_id, run_id)

    if not user:
        user = os.environ.get("USER", "default")

    # Run provenance for the cache layer -- stamped regardless of whether we
    # override OBJ_DIR, since it's path-independent metadata that
    # pd_cache.cache_or_run reads to tag each stored blob with who/which-run
    # produced it.
    if dag_id:
        os.environ["HAMMER_AIRFLOW_DAG_ID"] = str(dag_id)
    if run_id:
        os.environ["HAMMER_AIRFLOW_RUN_ID"] = str(run_id)
    if user:
        os.environ["HAMMER_AIRFLOW_TRIGGERING_USER"] = str(user)
    # The design/project name (DESIGN_NAME = the build-dir basename, e.g.
    # chipyard.harness.TestHarness.RocketConfig-ChipTop). Stamped to tag blobs
    # and the time-saved ledger with which design produced them -- otherwise the
    # `design` column stays NULL and only dag_id scopes.
    if design:
        os.environ["HAMMER_AIRFLOW_DESIGN"] = str(design)
    # Project label from the trigger conf (conf={"project": ...}). A shell env
    # var or vlsi.pd_cache.project still win, since neither is overwritten here.
    if proj_name and not os.environ.get("HAMMER_PD_PROJECT"):
        os.environ["HAMMER_PD_PROJECT"] = str(proj_name)

    # Route OBJ_DIR into the triggering user's workspace so builds on a shared
    # Airflow stay separated by who launched the DAG, not who generated it.
    # On by default; set HAMMER_NO_PER_USER_WORKSPACE=1 to skip this and keep
    # the DAG's baked OBJ_DIR (single-user setups).
    if os.environ.get("HAMMER_NO_PER_USER_WORKSPACE"):
        return None

    # Which named workspace, if the trigger asked for one explicitly.
    explicit_ws = ws_name or os.environ.get("HAMMER_WORKSPACE")

    # The owner of a DAG runs it in the DAG's own obj_dir. When the triggering
    # user IS the user who generated this DAG (and no named workspace was
    # requested), the baked directory wins -- no table lookup, no redirect.
    # Only someone ELSE triggering it gets routed to their own workspace.
    if (not explicit_ws and gen_user and default_obj_dir
            and str(user) == str(gen_user)):
        os.environ["OBJ_DIR"] = default_obj_dir
        os.environ.pop("HAMMER_D_MK", None)
        os.environ["HAMMER_AIRFLOW_WORKSPACE"] = os.path.dirname(default_obj_dir)
        print(f"[user-workspace] owner run: triggering_user={user!r} generated "
              f"this DAG -> using its own OBJ_DIR={default_obj_dir}")
        if claim:
            claim_obj_dir(default_obj_dir, dag_id, run_id, user)
        return default_obj_dir

    # Someone else's run (or an explicitly named workspace): resolve from the
    # user_workspaces table, read-only. Runs never create rows -- a missing
    # registration is an error with the fix spelled out, not a silent default.
    ws_name = explicit_ws or "default"
    is_owner = bool(gen_user) and str(user) == str(gen_user)
    try:
        from hammer.vlsi import pd_store
        workspace_root = pd_store.get_user_workspace(user, ws_name, auto_register=False)
    except Exception as e:
        if not is_owner:
            raise WorkspaceNotRegistered(
                f"could not look up workspace {ws_name!r} for user {user!r}, so the run "
                f"stops instead of using the DAG owner's OBJ_DIR. {type(e).__name__}: {e}"
            ) from e
        print(f"WARNING: could not resolve per-user workspace for {user!r}: {e}. "
              f"Falling back to the DAG's baked OBJ_DIR.")
        return None
    if not workspace_root:
        name_flag = "" if ws_name == "default" else f" --name {shlex.quote(str(ws_name))}"
        raise WorkspaceNotRegistered(
            f"no workspace registered for user {user!r} (workspace {ws_name!r}). "
            f"Register one first:  studio workspace-set {user} /path/to/their/build{name_flag}"
        )

    obj_dir = os.path.join(workspace_root, design)
    os.environ["OBJ_DIR"] = obj_dir
    # Clear HAMMER_D_MK (derives from OBJ_DIR) so a prior task's value can't leak
    # across users in the same worker.
    os.environ.pop("HAMMER_D_MK", None)
    os.environ["HAMMER_AIRFLOW_WORKSPACE"] = str(workspace_root)
    if ws_name:
        os.environ["HAMMER_AIRFLOW_WORKSPACE_NAME"] = str(ws_name)

    print(f"[user-workspace] triggering_user={user!r} workspace={ws_name!r} "
          f"dag_id={dag_id!r} run_id={run_id!r} -> OBJ_DIR={obj_dir}")
    if claim:
        try:
            claim_obj_dir(obj_dir, dag_id, run_id, user)
        except OSError as e:
            if is_owner:
                raise
            raise WorkspaceNotRegistered(
                f"cannot claim {obj_dir} for user {user!r}, so the run stops instead of "
                f"using the DAG owner's OBJ_DIR. {type(e).__name__}: {e}"
            ) from e
    return obj_dir


RUN_LOCK_NAME = ".sledgehammer-run.lock"


class WorkspaceNotRegistered(RuntimeError):
    """The run's workspace is not registered, or a non-owner cannot look it up or claim it.

    The owner gets it too when the trigger names an unregistered workspace.
    Like RunLockConflict, the generated DAGs re-raise it: falling back to the
    baked OBJ_DIR would run this build in the owner's directory, without the
    run lock.
    """


class RunLockConflict(RuntimeError):
    """Another run holds the obj_dir. Callers must not proceed into it.

    Distinct from RuntimeError so the generated DAGs can tell a lock conflict
    (fail the task) apart from resolver infrastructure trouble (fall back to
    the baked OBJ_DIR). Falling back on a conflict is how one run bulldozes
    another's rundirs.
    """


def _run_lock_path(obj_dir):
    return os.path.join(obj_dir, RUN_LOCK_NAME)


def _dag_run_state(dag_id, run_id):
    """The holder run's state per the Airflow metadata DB, or None if unknowable.

    Uses the same metadata-conn resolution as the notify path (env, then the
    SLEDGE_ file channel, then airflow.cfg) because Airflow 3 hands tasks a
    decoy connection.
    """
    if not dag_id or not run_id:
        return None
    try:
        import psycopg2
        from hammer.vlsi import pd_store
        settings = pd_store.airflow_metadata_conn_settings()
        if not settings:
            return None
        with psycopg2.connect(**settings) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM dag_run WHERE dag_id = %s AND run_id = %s",
                    (str(dag_id), str(run_id)))
                row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        print(f"[run-lock] holder liveness lookup failed: {e}")
        return None


def claim_obj_dir(obj_dir, dag_id, run_id, user):
    """Claim ``obj_dir`` for this DAG run, or fail.

    Tasks within one run share the directory on purpose -- that is how the
    leaf stages run side by side. Two *different* runs sharing it is the
    problem: they overwrite each other's rundirs and checkpoints, and the
    losing tool dies with no error of its own. To run a second iteration at
    the same time, trigger with conf={"workspace": "<name>"} so it resolves
    somewhere else.

    A lock whose holder the metadata DB says is finished (success/failed) is
    stale and gets taken over. When the holder is live, or its state cannot
    be determined, the claim refuses: a spurious refusal costs one manual
    `rm`, a spurious takeover costs someone hours of tool time.
    """
    if not obj_dir or not run_id:
        return
    lock = _run_lock_path(obj_dir)
    payload = json.dumps({
        "dag_id": dag_id, "run_id": run_id, "user": user,
        "pid": os.getpid(), "claimed": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    os.makedirs(obj_dir, exist_ok=True)
    for attempt in (1, 2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            return

        try:
            with open(lock) as f:
                held = json.load(f)
        except (OSError, ValueError):
            held = {}
        if str(held.get("run_id")) == str(run_id):
            return
        state = _dag_run_state(held.get("dag_id"), held.get("run_id"))
        if attempt == 1 and state in ("success", "failed"):
            print(f"[run-lock] holder {held.get('run_id')!r} is terminal "
                  f"({state}); taking over the stale lock.")
            try:
                os.remove(lock)
            except OSError:
                pass
            continue
        raise RunLockConflict(
            f"Error: run already in progress in {obj_dir}.\n"
            f"  held by run {held.get('run_id')!r} (dag {held.get('dag_id')!r}, "
            f"user {held.get('user')!r}, claimed {held.get('claimed')!r}, "
            f"state {state or 'unknown'})\n"
            f"  To run a second iteration at the same time, trigger with "
            f'conf={{"workspace": "<name>"}}.\n'
            f"  If that run is gone, remove {lock}"
        )


def release_obj_dir(obj_dir, run_id):
    """Drop this run's claim on ``obj_dir``. Another run's claim is left alone."""
    if not obj_dir or not run_id:
        return
    lock = _run_lock_path(obj_dir)
    try:
        with open(lock) as f:
            held = json.load(f)
    except (OSError, ValueError):
        return
    if str(held.get("run_id")) != str(run_id):
        return
    try:
        os.remove(lock)
    except OSError:
        pass


# The example Airflow DAGs that used to live here now sit in
# airflow_example_dags.py. They pulled airflow in at import time, which
# broke `hammer-vlsi` for anyone running the flow without airflow
# installed -- and nothing outside this file ever referenced them.
# Generated DAGs still import _resolve_workspace_obj_dir from here.

def main():
    CLIDriver().main()


