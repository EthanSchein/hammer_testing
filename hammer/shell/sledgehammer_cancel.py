"""sledgehammer cancel: mark a run failed, stop its processes on this host and free the build folder it holds.

The CLI half runs inside `sledgehammer`. The Airflow database half runs as
``python -m hammer.shell.sledgehammer_cancel read|fail ...`` with the stack's
database settings and prints its answer as JSON on its last line.
"""
import json
import os
import shlex
import signal
import subprocess
import sys

from hammer.shell import sledgehammer_cli

_GRACE_SECONDS = 10
_DB_TIMEOUT = 120
_FINISHED = ("success", "failed")
_TASK_DONE = ("success", "failed", "skipped", "upstream_failed", "removed")
_RUNNING = ("running", "restarting", "deferred")
_AIRFLOW_PROCESSES = ("airflow scheduler", "airflow dag-processor", "airflow api-server", "airflow triggerer",
                      "LocalExecutor")


def _own(dag_id, user):
    head, tail = "sledgehammer_", f"_{user}"
    return dag_id.startswith(head) and dag_id.endswith(tail) and len(dag_id) > len(head) + len(tail)


def _value(state):
    return getattr(state, "value", state)


def _age(when, now):
    return None if when is None else max(0.0, (now - when).total_seconds())


def _read(user, dag_id, run_id):
    """The caller's runs with this dag_id and run_id, or the DAG's running and queued runs, with their tasks."""
    import datetime
    from sqlalchemy import select
    from airflow.models.dagrun import DagRun
    from airflow.models.taskinstance import TaskInstance
    from airflow.utils.session import create_session
    from hammer.vlsi import run_liveness
    query = select(DagRun).order_by(DagRun.id)
    if dag_id:
        query = query.where(DagRun.dag_id == dag_id)
    query = query.where(DagRun.run_id == run_id) if run_id else query.where(DagRun.state.in_(("running", "queued")))
    now = datetime.datetime.now(datetime.timezone.utc)
    runs = []
    with create_session() as session:
        for dr in session.scalars(query):
            if not _own(dr.dag_id, user):
                continue
            tis = session.scalars(select(TaskInstance).where(TaskInstance.dag_id == dr.dag_id,
                                                             TaskInstance.run_id == dr.run_id))
            runs.append({"dag_id": dr.dag_id, "run_id": dr.run_id, "state": _value(dr.state),
                         "started": _age(dr.start_date, now), "queued": _age(dr.queued_at, now),
                         "tasks": [[ti.task_id, _value(ti.state), ti.hostname, ti.pid, str(ti.id)] for ti in tis]})
    return {"host": run_liveness.this_host(), "runs": runs}


def _fail(dag_id, run_id):
    """Mark the run failed with the call Airflow's UI uses; a run that has finished meanwhile keeps its state."""
    from sqlalchemy import select
    from airflow.api.common.mark_tasks import set_dag_run_state_to_failed
    from airflow.models.dagbag import DBDagBag
    from airflow.models.dagrun import DagRun
    from airflow.utils.session import create_session
    query = select(DagRun).where(DagRun.dag_id == dag_id, DagRun.run_id == run_id)
    with create_session() as session:
        dr = session.scalar(query)
        if dr is None:
            raise LookupError(f"run {run_id} of {dag_id} is not in the Airflow database")
        was, changed = _value(dr.state), []
        if was not in _FINISHED:
            dag = DBDagBag().get_dag_for_run(dr, session=session)
            if dag is None:
                raise LookupError(f"the Airflow database has no copy of the DAG that run {run_id} was started from")
            changed = [[ti.task_id, _value(ti.state)]
                       for ti in set_dag_run_state_to_failed(dag=dag, run_id=run_id, commit=True, session=session)]
    with create_session() as session:
        state = _value(session.scalar(query).state)
    return {"was": was, "state": state, "changed": changed}


def _db_main(argv):
    """`read USER DAG_ID RUN_ID` or `fail DAG_ID RUN_ID`, answered as JSON on the last line of stdout."""
    try:
        got = _read(*argv[1:4]) if argv[0] == "read" else _fail(*argv[1:3])
    except Exception as e:
        cause = getattr(e, "orig", None) or e
        print(json.dumps({"error": " ".join(f"{type(cause).__name__}: {cause}".split())[:300]}))
        return 1
    print(json.dumps(got, default=str))
    return 0


def _db(*args):
    """The database half's answer, or {"error": ...} when it cannot give one."""
    env = sledgehammer_cli._airflow_env()
    env.setdefault("PGCONNECT_TIMEOUT", os.environ.get("HAMMER_PG_CONNECT_TIMEOUT") or "10")
    try:
        r = subprocess.run([sys.executable, "-m", "hammer.shell.sledgehammer_cancel", *args],
                           capture_output=True, text=True, env=env, timeout=_DB_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"error": f"no answer within {_DB_TIMEOUT} s"}
    except OSError as e:
        return {"error": f"{type(e).__name__}: {e}"}
    try:
        got = json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        got = None
    if not isinstance(got, dict):
        return {"error": sledgehammer_cli._airflow_error(r.returncode, r.stdout, r.stderr)}
    return got


def _airflow_process(proc):
    try:
        cmdline = " ".join(proc.cmdline())
    except Exception:
        return False
    return any(marker in cmdline for marker in _AIRFLOW_PROCESSES)


def _scan(run_liveness, dag_id, run_id, tasks, host):
    """The run's processes on this host, leaving out this command and its parents."""
    import psutil
    found = run_liveness.local_run_processes(dag_id, run_id, tasks, host)
    try:
        mine = {p.pid for p in psutil.Process(os.getpid()).parents()}
    except psutil.Error:
        mine = set()
    return [p for p in found if p.pid not in mine]


def _running(proc):
    import psutil
    try:
        return proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _stop(procs):
    """SIGTERM the processes deepest first, then SIGKILL those still running after _GRACE_SECONDS.

    Returns how many needed SIGKILL and the ones still running after it.
    """
    import psutil

    def depth(proc):
        try:
            return len(proc.parents())
        except psutil.Error:
            return 0

    procs = sorted(procs, key=depth, reverse=True)
    for proc in procs:
        try:
            proc.send_signal(signal.SIGTERM)
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=_GRACE_SECONDS)
    alive = [p for p in alive if _running(p)]
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    _, left = psutil.wait_procs(alive, timeout=5)
    return len(alive), [p for p in left if _running(p)]


def _ours(held, dag_id, run_id):
    return str(held.get("run_id")) == str(run_id) and str(held.get("dag_id") or dag_id) == str(dag_id)


def _release(run_liveness, lock, dag_id, run_id):
    """Remove the lock when this run holds it; True when it is gone."""
    text, held, _ = run_liveness.read_run_lock(lock)
    if not text or not _ours(held, dag_id, run_id):
        return False
    run_liveness.remove_run_lock(lock, text)
    return not os.path.exists(lock)


def _hosts_to_wait_for(run_liveness, lock, run_id, host, tasks):
    """Other hosts where the run's tasks were running, or that its lock already waits on."""
    held = run_liveness.read_run_lock(lock)[1]
    hosts = set(held.get("pending_hosts") or []) | {t.hostname for t in tasks if t.state in _RUNNING and t.hostname}
    return sorted(hosts - {host})


def _keep_for(run_liveness, lock, dag_id, run_id, hosts):
    """Record in the lock the hosts where cancel still has to run; False when the lock is no longer this run's."""
    text, held, _ = run_liveness.read_run_lock(lock)
    if not text or not _ours(held, dag_id, run_id):
        return False
    held["pending_hosts"] = hosts
    tmp = f"{lock}.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(held, f)
    os.replace(tmp, lock)
    return True


def _name(task_id):
    return task_id.replace("module_", "")


def _since(run_liveness, run):
    if run.get("started") is not None:
        return f", started {run_liveness._span(run['started'])} ago"
    if run.get("queued") is not None:
        return f", queued {run_liveness._span(run['queued'])} ago"
    return ""


def _changes(changed):
    failed = [_name(t) for t, s in changed if s == "failed"]
    skipped = sum(1 for _, s in changed if s == "skipped")
    parts = ([f"{', '.join(failed)} failed"] if failed else []) + \
        ([f"{skipped} pending task{'' if skipped == 1 else 's'} skipped"] if skipped else [])
    return f": {'; '.join(parts)}" if parts else ""


def _show(run_liveness, dag_id, run_id, run, tasks, procs, remote, folder):
    if run is None:
        print(f"[sledgehammer] run {run_id} of {dag_id} is not in the Airflow database")
    else:
        print(f"[sledgehammer] run {run_id} of {dag_id}: {run['state']}{_since(run_liveness, run)}")
    active = {}
    for t in tasks:
        if t.state and t.state not in _TASK_DONE:
            active.setdefault(t.state, []).append(_name(t.task_id))
    for state, names in sorted(active.items()):
        print(f"  {state}: {', '.join(sorted(names))}")
    if procs:
        print("  its processes on this host:")
        for proc in procs[:10]:
            print(f"    {run_liveness._describe(proc)}")
        if len(procs) > 10:
            print(f"    and {len(procs) - 10} more")
    for t in remote:
        print(f"  task {_name(t.task_id)} ({t.state}) ran on host {t.hostname}; its processes there are not "
              f"stopped from here")
    if folder:
        print(f"  it holds the build folder {folder}")


def _yes(prompt):
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def main(argv) -> int:
    import argparse
    import getpass
    p = argparse.ArgumentParser(
        prog="sledgehammer cancel",
        description="Mark a run failed, stop its processes on this host and free the build folder it holds. "
                    "Asks first; without a terminal to ask in, it needs --yes.")
    p.add_argument("run_id", nargs="?",
                   help="the run to cancel (default: the run holding the build folder, else the DAG's only "
                        "running or queued run)")
    p.add_argument("--obj_dir", help="build directory, a --workspace one too; taken from the registered DAG, or "
                   "$OBJ_DIR / the only build/<x>, when omitted")
    p.add_argument("-t", "--top", "--design", dest="top", help="top module / design name")
    p.add_argument("--yes", action="store_true", help="do not ask before cancelling")
    p.add_argument("--free", action="store_true",
                   help="free the build folder even though cancel has not run on another host the run used")
    a = p.parse_args(argv)
    from hammer.vlsi import run_liveness

    user = getpass.getuser()
    dags_folder, dags_src = sledgehammer_cli._dags_folder()
    run_id, dag_id, folder = a.run_id, None, None
    if a.obj_dir or a.top or not run_id:
        _, obj_dir, dag_id, _, baked = sledgehammer_cli._find_build(a.obj_dir, a.top, user, dags_folder, dags_src)
        folder = obj_dir if a.obj_dir else baked or obj_dir
    if not run_id:
        held = run_liveness.read_run_lock(os.path.join(folder, run_liveness.RUN_LOCK_NAME))[1]
        if held.get("run_id"):
            run_id, dag_id = str(held["run_id"]), str(held.get("dag_id") or dag_id)
            if not _own(dag_id, user):
                sys.exit(f"[sledgehammer] {folder} is held by run {run_id} of {dag_id}, which is not one of your "
                         f"DAGs, so it is not yours to cancel.")

    got = _db("read", user, dag_id or "", run_id or "")
    if "error" in got:
        sys.exit(f"[sledgehammer] could not read the run from the Airflow database, so nothing was changed: "
                 f"{got['error']}\n  Check the database with:  sledgehammer postgres show")
    runs = got.get("runs") or []
    if not run_id:
        if not runs:
            print(f"[sledgehammer] no run holds {folder} and {dag_id} has no running or queued run; "
                  f"nothing to cancel.")
            return 0
        if len(runs) > 1:
            sys.exit("\n".join([f"[sledgehammer] {dag_id} has {len(runs)} running or queued runs:"]
                               + [f"  {r['run_id']:44s} {r['state']}{_since(run_liveness, r)}" for r in runs]
                               + ["  Cancel one with:  sledgehammer cancel <run_id>"]))
    elif len(runs) > 1:
        sys.exit(f"[sledgehammer] more than one of your DAGs has a run {run_id}: "
                 f"{', '.join(r['dag_id'] for r in runs)}. Pick one with -t <design>.")
    elif not runs and not dag_id:
        sys.exit(f"[sledgehammer] none of your DAGs has a run {run_id} in the Airflow database; "
                 f"list them with:  sledgehammer runs")
    run = runs[0] if runs else None
    if run:
        dag_id, run_id = run["dag_id"], run["run_id"]
    if folder is None:
        folder = sledgehammer_cli._dag_obj_dir(os.path.join(dags_folder, f"{dag_id}.py"))

    host = got.get("host") or run_liveness.this_host()
    tasks = [run_liveness.Task(*t[:5], None) for t in (run or {}).get("tasks", [])]
    try:
        procs = _scan(run_liveness, dag_id, run_id, tasks, host)
    except Exception as e:
        sys.exit(f"[sledgehammer] could not list this host's processes, so nothing was changed: "
                 f"{type(e).__name__}: {e}")
    airflow = [proc for proc in procs if _airflow_process(proc)]
    if airflow:
        sys.exit(f"[sledgehammer] the ids of run {run_id} are in the environment of Airflow's own "
                 f"{run_liveness._describe(airflow[0])}, so its processes cannot be told apart from Airflow's; "
                 f"nothing was changed.\n  Restart sledgehammer from a shell without HAMMER_AIRFLOW_RUN_ID set, "
                 f"then run sledgehammer cancel again.")
    lock = os.path.join(folder, run_liveness.RUN_LOCK_NAME) if folder else None
    holds = bool(lock) and _ours(run_liveness.read_run_lock(lock)[1], dag_id, run_id)
    remote = [t for t in tasks if t.hostname and t.hostname != host and t.state not in ("success", "skipped")]
    to_fail = run is not None and run["state"] not in _FINISHED
    wait_for = _hosts_to_wait_for(run_liveness, lock, run_id, host, tasks) if holds and not a.free else []

    _show(run_liveness, dag_id, run_id, run, tasks, procs, remote, folder if holds else None)
    plan = (["mark it failed"] if to_fail else []) + \
        ([f"stop {len(procs)} process{'' if len(procs) == 1 else 'es'} on this host"] if procs else []) + \
        ([f"free {folder}"] if holds and not wait_for else []) + \
        ([f"keep {folder} locked until cancel has run on {', '.join(wait_for)} too"] if wait_for else [])
    if not plan:
        if run is None:
            sys.exit(f"[sledgehammer] nothing of run {run_id} runs on this host; nothing to cancel.")
        print(f"[sledgehammer] run {run_id} is already {run['state']}, nothing of it runs on this host and it "
              f"holds no build folder; nothing to do.")
        return 0
    print(f"  cancel will {', '.join(plan[:-1]) + ' and ' if len(plan) > 1 else ''}{plan[-1]}.")
    if not a.yes and not sys.stdin.isatty():
        print("[sledgehammer] there is no terminal to ask in, so nothing was changed; "
              "add --yes to cancel without asking.")
        return 1
    if not a.yes and not _yes("Go ahead? [y/N] "):
        print("[sledgehammer] nothing was changed.")
        return 1

    if to_fail:
        res = _db("fail", dag_id, run_id)
        if "error" in res:
            sys.exit(f"[sledgehammer] could not mark run {run_id} failed, so nothing was stopped: {res['error']}")
        if res.get("was") in _FINISHED:
            print(f"[sledgehammer] run {run_id} finished as {res['was']} in the meantime; that state stays.")
            to_fail = False
        elif res.get("state") != "failed":
            sys.exit(f"[sledgehammer] the Airflow database still shows run {run_id} as {res.get('state')}, "
                     f"so nothing was stopped.")
        else:
            print(f"[sledgehammer] marked run {run_id} failed{_changes(res.get('changed') or [])}")

    try:
        more = _scan(run_liveness, dag_id, run_id, tasks, host)
    except Exception:
        more = []
    if not any(_airflow_process(proc) for proc in more):
        seen = {proc.pid for proc in procs}
        procs += [proc for proc in more if proc.pid not in seen]
    left = []
    if procs:
        print(f"[sledgehammer] stopping {len(procs)} process{'' if len(procs) == 1 else 'es'}: SIGTERM, then "
              f"SIGKILL for any still running after {_GRACE_SECONDS} s")
        killed, left = _stop(procs)
        print(f"[sledgehammer] stopped {len(procs) - len(left)} of them"
              + (f", {killed} with SIGKILL" if killed else ""))
        for proc in left:
            print(f"  still running: {run_liveness._describe(proc)}")
    there = f"sledgehammer cancel {run_id}"
    if wait_for and _keep_for(run_liveness, lock, dag_id, run_id, wait_for):
        hosts, cmd = ", ".join(wait_for), f"sledgehammer cancel {run_id} --obj_dir {shlex.quote(folder)}"
        there = cmd
        print(f"[sledgehammer] {folder} stays locked until  {cmd}  has run on {hosts}\n"
              f"  If {hosts} is gone for good:  {cmd} --free")
    elif lock and _release(run_liveness, lock, dag_id, run_id):
        print(f"[sledgehammer] freed {folder}")

    print(f"[sledgehammer] run {run_id} is {'cancelled' if to_fail else 'cleaned up'}.")
    for remote_host in sorted({t.hostname for t in remote}):
        print(f"  On {remote_host}, stop what is left of it with:  {there}")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(_db_main(sys.argv[1:]))
