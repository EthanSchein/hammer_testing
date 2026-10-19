"""Whether an Airflow run still has anything alive, and the verdict on the build-folder lock it holds."""

import collections
import getpass
import json
import os
import shlex
import socket
import sys
import time

RUN_LOCK_NAME = ".sledgehammer-run.lock"

STALE_SECONDS = 600

FREE, FINISHED, LIVE, BUSY, STALE, UNKNOWN = "free", "finished", "live", "busy", "stale", "unknown"
TAKEOVER = (FREE, FINISHED, STALE)

Task = collections.namedtuple("Task", "task_id state hostname pid ti_id age")
Activity = collections.namedtuple("Activity", "state age tasks")
Verdict = collections.namedtuple("Verdict", "kind activity processes remote error")


class DatabaseUnreachable(RuntimeError):
    """The Airflow metadata database could not be asked about a run."""


_ACTIVITY_SQL = """
SELECT dr.state,
       EXTRACT(EPOCH FROM now() - GREATEST(dr.queued_at, dr.start_date, dr.end_date, dr.updated_at)),
       ti.task_id, ti.state, ti.hostname, ti.pid, ti.id::text,
       EXTRACT(EPOCH FROM now() - GREATEST(ti.last_heartbeat_at, ti.start_date, ti.end_date,
                                            ti.queued_dttm, ti.updated_at))
FROM dag_run dr
LEFT JOIN task_instance ti ON ti.dag_id = dr.dag_id AND ti.run_id = dr.run_id
WHERE dr.dag_id = %s AND dr.run_id = %s
"""


def _seconds(value):
    return None if value is None else max(0.0, float(value))


def run_activity(conn, dag_id, run_id):
    """The run's state and task rows, aged in seconds by the database clock, or None when it has no such run."""
    with conn.cursor() as cur:
        cur.execute(_ACTIVITY_SQL, (str(dag_id), str(run_id)))
        rows = cur.fetchall()
    if not rows:
        return None
    tasks = [Task(r[2], r[3], r[4], r[5], r[6], _seconds(r[7])) for r in rows if r[2] is not None]
    return Activity(rows[0][0], _seconds(rows[0][1]), tasks)


def fetch_run_activity(dag_id, run_id):
    """run_activity over the Airflow metadata connection; raises DatabaseUnreachable when it cannot ask."""
    try:
        import psycopg2
        from hammer.vlsi import pd_store
        settings = pd_store.airflow_metadata_conn_settings()
        if not settings:
            raise DatabaseUnreachable("no Postgres connection to the Airflow database is configured")
        conn = psycopg2.connect(**{"connect_timeout": pd_store._connect_timeout(), **settings})
    except DatabaseUnreachable:
        raise
    except Exception as e:
        raise DatabaseUnreachable(_one_line(e)) from e
    try:
        return run_activity(conn, dag_id, run_id)
    except Exception as e:
        raise DatabaseUnreachable(_one_line(e)) from e
    finally:
        conn.close()


def _one_line(e):
    return " ".join(f"{type(e).__name__}: {e}".split())


def this_host():
    """This host's name as Airflow records it in task_instance.hostname."""
    try:
        from airflow.utils.net import get_hostname
        return get_hostname()
    except Exception:
        return socket.getfqdn()


def local_run_processes(dag_id, run_id, tasks=(), host=None):
    """This user's live processes of the run here: verified task runners, processes with its ids, descendants."""
    import psutil
    host = host or this_host()
    me = os.getuid()
    found = {}

    def add(proc):
        found[proc.pid] = proc
        try:
            for child in proc.children(recursive=True):
                found[child.pid] = child
        except psutil.Error:
            pass

    for t in tasks:
        if not t.pid or t.hostname != host:
            continue
        try:
            proc = psutil.Process(int(t.pid))
            if proc.uids().real == me and f"airflow worker -- {t.ti_id}" in " ".join(proc.cmdline()):
                add(proc)
        except psutil.Error:
            pass
    for proc in psutil.process_iter():
        if proc.pid in found:
            continue
        try:
            if proc.uids().real != me:
                continue
            env = proc.environ()
        except psutil.Error:
            continue
        if env.get("HAMMER_AIRFLOW_RUN_ID") == str(run_id) and env.get("HAMMER_AIRFLOW_DAG_ID") == str(dag_id):
            add(proc)
    found.pop(os.getpid(), None)
    alive = []
    for pid in sorted(found):
        try:
            if found[pid].status() != psutil.STATUS_ZOMBIE:
                alive.append(found[pid])
        except psutil.Error:
            pass
    return alive


def _youngest(activity):
    ages = [a for a in [activity.age] + [t.age for t in activity.tasks] if a is not None]
    return min(ages) if ages else None


def run_verdict(dag_id, run_id, lock_host=None, stale_after=STALE_SECONDS):
    """FINISHED, LIVE, BUSY, STALE or UNKNOWN for a run, with the activity, processes and other hosts behind it."""
    try:
        activity = fetch_run_activity(dag_id, run_id)
    except DatabaseUnreachable as e:
        return Verdict(UNKNOWN, None, [], [], str(e))
    if activity is not None and activity.state in ("success", "failed"):
        try:
            processes = local_run_processes(dag_id, run_id, activity.tasks)
        except Exception:
            processes = []
        return Verdict(BUSY if processes else FINISHED, activity, processes, [], None)
    youngest = _youngest(activity) if activity is not None else None
    if youngest is not None and youngest < stale_after:
        return Verdict(LIVE, activity, [], [], None)
    host = this_host()
    tasks = activity.tasks if activity is not None else []
    remote = [t for t in tasks if t.hostname and t.hostname != host and t.state not in ("success", "skipped")]
    if activity is None and lock_host and lock_host != host:
        remote.append(Task(None, None, lock_host, None, None, None))
    try:
        processes = local_run_processes(dag_id, run_id, tasks, host)
    except Exception as e:
        return Verdict(UNKNOWN, activity, [], remote, f"could not list this host's processes: {type(e).__name__}: {e}")
    return Verdict(BUSY if remote or processes else STALE, activity, processes, remote, None)


def _span(seconds):
    return f"{int(seconds)} s" if seconds < 120 else f"{int(seconds // 60)} min"


def last_sign_of_life(activity, stale_after=STALE_SECONDS):
    """The run's most recent task activity in words, and its own record's when only that is recent, or None."""
    if activity is None:
        return None
    timed = [t for t in activity.tasks if t.age is not None]
    task = min(timed, key=lambda t: t.age) if timed else None
    signs = []
    if task is not None:
        where = f" on {task.hostname}" if task.hostname else ""
        signs.append(f"task {task.task_id} ({task.state}){where}, {_span(task.age)} ago")
    if activity.age is not None and (task is None or activity.age < stale_after <= task.age):
        signs.append(f"the run's record changed {_span(activity.age)} ago")
    return "; ".join(signs) or None


def _describe(proc):
    try:
        cmd = " ".join(proc.cmdline()).strip() or proc.name()
    except Exception:
        cmd = "?"
    return f"pid {proc.pid}: {cmd[:100]}"


def read_run_lock(lock):
    """(text, holder, age in seconds) of a lock file; age is None without a lock, text None when unreadable."""
    try:
        age = max(0.0, time.time() - os.stat(lock).st_mtime)
    except OSError:
        return "", {}, None
    try:
        with open(lock) as f:
            text = f.read()
    except OSError:
        return None, {}, age
    try:
        held = json.loads(text)
    except ValueError:
        held = {}
    return text, held if isinstance(held, dict) else {}, age


def remove_run_lock(lock, text):
    """Remove the lock only if it still reads ``text``, so a claim made meanwhile survives."""
    try:
        with open(lock) as f:
            if f.read() != text:
                return
        os.remove(lock)
    except OSError:
        pass


def _other_owner(lock):
    """The name of the user who owns the lock file, when that is not this user."""
    try:
        uid = os.stat(lock).st_uid
    except OSError:
        return None
    if uid == os.getuid():
        return None
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return f"uid {uid}"


def check_run_lock(obj_dir, held, age, stale_after=STALE_SECONDS, user=None):
    """(kind, text) for a lock's holder: the refusal for LIVE, BUSY and UNKNOWN, otherwise the takeover note."""
    if age is None:
        return FREE, ""
    run_id = held.get("run_id")
    studio = shlex.quote(os.path.join(os.path.dirname(sys.executable), "studio"))
    second = (f"  To run a second iteration at the same time, register a workspace once with:  "
              f"{studio} workspace-set {user or getpass.getuser()} <dir> --name <name>\n"
              f"  and then run:  sledgehammer run <stage> --workspace <name>")
    if not run_id:
        if age >= stale_after:
            return STALE, f"the lock file names no run and is {_span(age)} old; taking over the stale lock."
        return UNKNOWN, (f"run already in progress in {obj_dir}.\n"
                         f"  its lock file names no run yet; a lock like that is taken over "
                         f"once it is {_span(stale_after)} old.\n{second}")
    v = run_verdict(held.get("dag_id"), run_id, held.get("host"), stale_after)
    # Another user's processes cannot be listed from here, so their lock is never provably dead.
    other = _other_owner(os.path.join(obj_dir, RUN_LOCK_NAME))
    if other and v.kind in (FINISHED, STALE):
        v = v._replace(kind=BUSY)
    if v.activity is not None:
        state = v.activity.state
    else:
        state = "unknown" if v.kind == UNKNOWN else "not in the Airflow database"
    sign = last_sign_of_life(v.activity, stale_after)
    cmd = f"sledgehammer cancel {run_id}"
    if v.activity is None:
        cmd += f" --obj_dir {shlex.quote(str(obj_dir))}"
    if v.kind == FINISHED:
        return FINISHED, f"holder {run_id!r} is terminal ({state}); taking over the stale lock."
    if v.kind == STALE:
        if v.activity is None:
            return STALE, (f"holder {run_id!r} is not in the Airflow database and no process of it "
                           f"runs on this host; taking over the stale lock.")
        seen = f"last sign of life: {sign}; " if sign else ""
        return STALE, (f"holder {run_id!r} still shows as {state} in Airflow, but nothing of it is alive "
                       f"({seen}no process of it on this host); taking over the stale lock.\n"
                       f"  To mark that run failed:  sledgehammer cancel {run_id}")
    if v.activity is None and not v.processes:
        first = f"run {run_id!r} holds {obj_dir}, and whether anything of it still runs cannot be checked."
    elif v.activity is None or state not in ("success", "failed"):
        first = f"run already in progress in {obj_dir}."
    elif v.processes:
        first = f"run {run_id!r} has finished, but some of its processes are still alive in {obj_dir}."
    else:
        first = f"run {run_id!r} has finished, but whether anything of it still runs in {obj_dir} cannot be checked."
    lines = [first,
             f"  held by run {run_id!r} (dag {held.get('dag_id')!r}, user {held.get('user')!r}, "
             f"claimed {held.get('claimed')!r}, state {state})"]
    if sign:
        lines.append(f"  last sign of life: {sign}")
    if v.kind == BUSY:
        lines.append("  still alive, and possibly still writing here:")
        lines += [f"    {_describe(p)}" for p in v.processes[:5]]
        if len(v.processes) > 5:
            lines.append(f"    and {len(v.processes) - 5} more processes")
        for t in v.remote:
            what = f"task {t.task_id} ({t.state})" if t.task_id else "its first task"
            lines.append(f"    {what} on host {t.hostname}, which cannot be checked from here")
        if other:
            lines.append(f"    anything it runs as {other}, whose processes cannot be checked from here")
    if v.kind == UNKNOWN:
        lines.append(f"  could not check whether it is still alive: {v.error}")
    stop = (f"  Only {other} can stop that run and free this directory, with:  {cmd}" if other
            else f"  To stop that run and free this directory:  {cmd}")
    lines += [second, stop]
    return v.kind, "\n".join(lines)


def main(argv=None):
    """Print the verdict on ``<obj_dir>``'s lock as JSON, for `sledgehammer run` to show before it triggers."""
    obj_dir = (sys.argv[1:] if argv is None else argv)[0]
    text, held, age = read_run_lock(os.path.join(obj_dir, RUN_LOCK_NAME))
    kind, message = check_run_lock(obj_dir, held, age, user=getpass.getuser())
    print(json.dumps({"kind": kind, "text": message}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
