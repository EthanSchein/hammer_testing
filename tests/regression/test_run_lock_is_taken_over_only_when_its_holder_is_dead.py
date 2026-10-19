import json
import os
import shlex
import sys
import time
import types
from decimal import Decimal
from types import SimpleNamespace

import pytest

import hammer.shell.hammer_vlsi as hammer_vlsi
from hammer.vlsi import pd_cache, pd_store, run_liveness
from hammer.vlsi.run_liveness import BUSY, FINISHED, LIVE, STALE, UNKNOWN, Activity, Task

HERE = "here.example"
STUDIO = shlex.quote(os.path.join(os.path.dirname(sys.executable), "studio"))
DAG = "sledgehammer_Top_alice"
ME = os.getuid()
MIN = 60


class _Error(Exception):
    pass


class _NoSuchProcess(_Error):
    pass


class _AccessDenied(_Error):
    pass


class _Proc:
    def __init__(self, pid, cmd, env=None, uid=ME, parent=None, status="sleeping"):
        self.pid, self._cmd, self._env, self._uid, self._status = pid, cmd, env, uid, status
        self._children = []
        if parent is not None:
            parent._children.append(self)

    def uids(self):
        return SimpleNamespace(real=self._uid)

    def cmdline(self):
        return self._cmd.split()

    def name(self):
        return self._cmd.split()[0]

    def environ(self):
        if self._env is None:
            raise _AccessDenied(self.pid)
        return self._env

    def children(self, recursive=False):
        out = []
        for child in self._children:
            out += [child] + (child.children(True) if recursive else [])
        return out

    def status(self):
        return self._status


def _ids(run_id="old", dag_id=DAG):
    return {"HAMMER_AIRFLOW_RUN_ID": run_id, "HAMMER_AIRFLOW_DAG_ID": dag_id, "PATH": "/usr/bin"}


def _runner_with_tool():
    runner = _Proc(4242, "airflow worker -- ti-par", env={})
    _Proc(4243, "python hammer-vlsi par", env=_ids(), parent=runner)
    return [runner, runner.children()[0]]


def _orphaned_tool():
    tool = _Proc(5000, "python hammer-vlsi par", env=_ids())
    _Proc(5001, "srun --job-name=h-par innovus", env=_ids(), parent=tool)
    return [tool, tool.children()[0], _Proc(1, "init", env={})]


def _task(age, host=HERE, state="running", pid=4242):
    return Task("module_Top.par", state, host, pid, "ti-par", age)


def _running(*tasks, age=50 * MIN):
    return Activity("running", age, list(tasks))


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A lock in obj_dir held by run 'old', with its database rows and this host's processes faked."""
    w = SimpleNamespace(activity=None, error=None, procs=[])

    def fetch(dag_id, run_id):
        assert (dag_id, run_id) == (DAG, "old")
        if w.error:
            raise run_liveness.DatabaseUnreachable(w.error)
        return w.activity

    def process(pid):
        for p in w.procs:
            if p.pid == pid:
                return p
        raise _NoSuchProcess(pid)

    psutil = types.ModuleType("psutil")
    psutil.__dict__.update(Error=_Error, NoSuchProcess=_NoSuchProcess, AccessDenied=_AccessDenied,
                           STATUS_ZOMBIE="zombie", Process=process, process_iter=lambda: list(w.procs))
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    monkeypatch.setattr(run_liveness, "fetch_run_activity", fetch)
    monkeypatch.setattr(run_liveness, "this_host", lambda: HERE)
    w.obj = tmp_path / "Top"
    w.obj.mkdir()
    w.lock = w.obj / ".sledgehammer-run.lock"
    w.held = {"dag_id": DAG, "run_id": "old", "user": "alice", "host": HERE, "pid": 4242,
              "claimed": "2026-10-18T04:00:00"}
    w.lock.write_text(json.dumps(w.held))
    w.claim = lambda: hammer_vlsi.claim_obj_dir(str(w.obj), DAG, "new", "alice")
    w.holder = lambda: json.loads(w.lock.read_text())["run_id"]
    return w


MATRIX = {
    "finished": (lambda: Activity("failed", 50 * MIN, [_task(50 * MIN, state="failed")]), list, FINISHED),
    "finished_with_tool_alive": (lambda: Activity("failed", 50 * MIN, [_task(50 * MIN, state="failed")]),
                                 _orphaned_tool, BUSY),
    "fresh_heartbeat": (lambda: _running(_task(12)), list, LIVE),
    "fresh_run_record": (lambda: _running(_task(50 * MIN), age=30), list, LIVE),
    "task_runner_alive": (lambda: _running(_task(50 * MIN)), _runner_with_tool, BUSY),
    "orphaned_tool_alive": (lambda: _running(_task(50 * MIN)), _orphaned_tool, BUSY),
    "task_on_another_host": (lambda: _running(_task(50 * MIN, host="other.example")), list, BUSY),
    "finished_task_on_another_host": (lambda: _running(_task(50 * MIN, host="other.example", state="success")),
                                      list, STALE),
    "task_pid_reused": (lambda: _running(_task(50 * MIN)), lambda: [_Proc(4242, "vim notes.txt", env={})], STALE),
    "task_runner_of_another_user": (lambda: _running(_task(50 * MIN)),
                                    lambda: [_Proc(4242, "airflow worker -- ti-par", env={}, uid=ME + 1)], STALE),
    "processes_of_other_runs": (lambda: _running(_task(50 * MIN)),
                                lambda: [_Proc(6000, "hammer-vlsi syn", env=_ids("other")),
                                         _Proc(6001, "hammer-vlsi syn", env=_ids(dag_id="sledgehammer_Top_bob"))],
                                STALE),
    "zombie_tool": (lambda: _running(_task(50 * MIN)),
                    lambda: [_Proc(6002, "hammer-vlsi", env=_ids(), status="zombie")], STALE),
    "unreadable_environment": (lambda: _running(_task(50 * MIN)), lambda: [_Proc(6003, "ssh-agent")], STALE),
    "nothing_alive": (lambda: _running(_task(50 * MIN)), list, STALE),
    "missing_from_database": (lambda: None, list, STALE),
    "missing_from_database_with_tool_alive": (lambda: None, _orphaned_tool, BUSY),
}


@pytest.mark.parametrize("activity, procs, kind", MATRIX.values(), ids=MATRIX.keys())
def test_liveness_matrix(world, activity, procs, kind) -> None:
    world.activity, world.procs = activity(), procs()
    assert run_liveness.run_verdict(DAG, "old", HERE).kind == kind


def test_an_unreachable_database_is_unknown_not_missing(world) -> None:
    world.error = "OperationalError: connection refused"
    verdict = run_liveness.run_verdict(DAG, "old", HERE)
    assert (verdict.kind, verdict.error) == (UNKNOWN, "OperationalError: connection refused")


def test_a_missing_holder_claimed_on_another_host_is_busy(world) -> None:
    assert run_liveness.run_verdict(DAG, "old", "other.example").kind == BUSY


def test_the_verdict_finds_task_runner_descendants_and_orphans(world) -> None:
    world.activity, world.procs = _running(_task(50 * MIN)), _runner_with_tool() + _orphaned_tool()
    verdict = run_liveness.run_verdict(DAG, "old", HERE)
    assert [p.pid for p in verdict.processes] == [4242, 4243, 5000, 5001]


def test_a_live_holder_is_refused_with_its_last_sign_of_life(world) -> None:
    world.activity = _running(_task(12))
    with pytest.raises(hammer_vlsi.RunLockConflict) as err:
        world.claim()
    assert str(err.value) == (
        f"Error: run already in progress in {world.obj}.\n"
        f"  held by run 'old' (dag '{DAG}', user 'alice', claimed '2026-10-18T04:00:00', state running)\n"
        f"  last sign of life: task module_Top.par (running) on here.example, 12 s ago\n"
        f"  To run a second iteration at the same time, register a workspace once with:  "
        f"{STUDIO} workspace-set alice <dir> --name <name>\n"
        f"  and then run:  sledgehammer run <stage> --workspace <name>\n"
        f"  To stop that run and free this directory:  sledgehammer cancel old")
    assert world.holder() == "old"


def test_the_last_sign_of_life_names_the_run_record_only_when_just_that_is_recent() -> None:
    assert run_liveness.last_sign_of_life(_running(_task(50 * MIN), age=30)) == (
        "task module_Top.par (running) on here.example, 50 min ago; the run's record changed 30 s ago")
    assert run_liveness.last_sign_of_life(_running(_task(4), age=1)) == (
        "task module_Top.par (running) on here.example, 4 s ago")
    assert run_liveness.last_sign_of_life(Activity("queued", 150, [])) == "the run's record changed 2 min ago"


def test_a_finished_holder_with_live_processes_is_refused_with_them(world) -> None:
    world.activity = Activity("failed", 50 * MIN, [_task(50 * MIN, state="failed")])
    world.procs = _orphaned_tool()
    with pytest.raises(hammer_vlsi.RunLockConflict) as err:
        world.claim()
    message = str(err.value)
    assert message.startswith(f"Error: run 'old' has finished, but some of its processes are still alive in "
                              f"{world.obj}.\n")
    assert "    pid 5000: python hammer-vlsi par\n    pid 5001: srun --job-name=h-par innovus\n" in message
    assert message.endswith("To stop that run and free this directory:  sledgehammer cancel old")
    assert world.holder() == "old"


@pytest.mark.parametrize("activity", [lambda: None, lambda: Activity("failed", 50 * MIN, []),
                                      lambda: _running(_task(50 * MIN))], ids=["missing", "finished", "quiet"])
def test_a_lock_another_user_owns_is_never_taken_over(world, monkeypatch, activity) -> None:
    world.activity = activity()
    monkeypatch.setattr(run_liveness, "_other_owner", lambda lock: "bob")
    with pytest.raises(hammer_vlsi.RunLockConflict) as err:
        world.claim()
    message = str(err.value)
    finished = world.activity is not None and world.activity.state == "failed"
    assert message.splitlines()[0] == (
        f"Error: run 'old' has finished, but whether anything of it still runs in {world.obj} cannot be checked."
        if finished else f"Error: run already in progress in {world.obj}." if world.activity is not None
        else f"Error: run 'old' holds {world.obj}, and whether anything of it still runs cannot be checked.")
    assert "    anything it runs as bob, whose processes cannot be checked from here\n" in message
    folder = "" if world.activity is not None else f" --obj_dir {world.obj}"
    assert message.endswith(f"Only bob can stop that run and free this directory, with:  "
                            f"sledgehammer cancel old{folder}")
    assert world.holder() == "old"


def test_a_holder_missing_from_the_database_with_live_processes_is_refused_with_them(world) -> None:
    world.procs = _orphaned_tool()
    with pytest.raises(hammer_vlsi.RunLockConflict) as err:
        world.claim()
    message = str(err.value)
    assert message.splitlines()[0] == f"Error: run already in progress in {world.obj}."
    assert "    pid 5000: python hammer-vlsi par\n" in message
    assert message.endswith(f"To stop that run and free this directory:  "
                            f"sledgehammer cancel old --obj_dir {world.obj}")


def test_only_a_lock_file_of_another_user_counts_as_theirs(world) -> None:
    assert run_liveness._other_owner(str(world.lock)) is None
    assert run_liveness._other_owner(str(world.obj / "no-such-lock")) is None


def test_a_quiet_holder_with_live_processes_is_refused_with_them(world) -> None:
    world.activity, world.procs = _running(_task(50 * MIN, host="other.example")), _orphaned_tool()
    with pytest.raises(hammer_vlsi.RunLockConflict) as err:
        world.claim()
    message = str(err.value)
    assert "  last sign of life: task module_Top.par (running) on other.example, 50 min ago\n" in message
    assert ("  still alive, and possibly still writing here:\n"
            "    pid 5000: python hammer-vlsi par\n"
            "    pid 5001: srun --job-name=h-par innovus\n"
            "    task module_Top.par (running) on host other.example, which cannot be checked from here\n") in message
    assert message.endswith("To stop that run and free this directory:  sledgehammer cancel old")
    assert ".sledgehammer-run.lock" not in message
    assert world.holder() == "old"


def test_a_dead_holder_is_taken_over_with_a_cancel_hint(world, capsys) -> None:
    world.activity = _running(_task(50 * MIN))
    world.claim()
    assert capsys.readouterr().out == (
        "[run-lock] holder 'old' still shows as running in Airflow, but nothing of it is alive "
        "(last sign of life: task module_Top.par (running) on here.example, 50 min ago; "
        "no process of it on this host); taking over the stale lock.\n"
        "  To mark that run failed:  sledgehammer cancel old\n")
    held = json.loads(world.lock.read_text())
    assert (held["run_id"], held["host"], held["user"]) == ("new", HERE, "alice")


def test_a_holder_missing_from_the_database_is_taken_over(world, capsys) -> None:
    world.claim()
    assert capsys.readouterr().out == (
        "[run-lock] holder 'old' is not in the Airflow database and no process of it runs on this host; "
        "taking over the stale lock.\n")
    assert world.holder() == "new"


def test_a_finished_holder_is_taken_over(world, capsys) -> None:
    world.activity = Activity("success", 5, [])
    world.claim()
    assert "holder 'old' is terminal (success); taking over the stale lock." in capsys.readouterr().out
    assert world.holder() == "new"


def test_an_unreachable_database_still_refuses(world) -> None:
    world.error = "OperationalError: connection to server failed"
    with pytest.raises(hammer_vlsi.RunLockConflict) as err:
        world.claim()
    assert str(err.value).splitlines()[0] == (
        f"Error: run 'old' holds {world.obj}, and whether anything of it still runs cannot be checked.")
    assert ("state unknown)\n"
            "  could not check whether it is still alive: OperationalError: connection to server failed\n"
            ) in str(err.value)
    assert world.holder() == "old"


def test_an_unreadable_lock_is_taken_over_only_once_old(world, capsys) -> None:
    world.lock.write_text("")
    with pytest.raises(hammer_vlsi.RunLockConflict, match="its lock file names no run yet"):
        world.claim()
    old = time.time() - 11 * MIN
    os.utime(world.lock, (old, old))
    world.claim()
    assert "the lock file names no run and is 11 min old; taking over the stale lock." in capsys.readouterr().out
    assert world.holder() == "new"


def test_a_claim_made_meanwhile_is_not_removed(world) -> None:
    seen = world.lock.read_text()
    world.lock.write_text(json.dumps(dict(world.held, run_id="newer")))
    run_liveness.remove_run_lock(str(world.lock), seen)
    assert world.holder() == "newer"


def test_the_cache_restore_refusal_points_to_cancel(world, monkeypatch) -> None:
    monkeypatch.setenv("HAMMER_AIRFLOW_RUN_ID", "new")
    with pytest.raises(pd_cache.CacheRestoreContested) as err:
        pd_cache._check_restore_allowed(world.obj / "par-rundir")
    assert str(err.value).endswith("To stop that run and free this directory:  sledgehammer cancel old")
    assert "remove" not in str(err.value)


class _Cursor:
    def __init__(self, rows):
        self.rows, self.executed = rows, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


def test_run_activity_reads_the_run_and_its_tasks_by_the_database_clock() -> None:
    cur =_Cursor([("running", Decimal("75.5"), "module_Top.par", "running", HERE, 4242, "ti-par", Decimal("4.2")),
                   ("running", Decimal("75.5"), "start", "success", HERE, 4100, "ti-start", Decimal("-1"))])
    activity = run_liveness.run_activity(SimpleNamespace(cursor=lambda: cur), DAG, "old")
    assert activity == Activity("running", 75.5, [Task("module_Top.par", "running", HERE, 4242, "ti-par", 4.2),
                                                  Task("start", "success", HERE, 4100, "ti-start", 0.0)])
    assert cur.executed[0][1] == (DAG, "old") and "now()" in cur.executed[0][0]


def test_run_activity_tells_a_run_without_tasks_from_a_missing_run() -> None:
    no_tasks = _Cursor([("queued", 3, None, None, None, None, None, None)])
    assert run_liveness.run_activity(SimpleNamespace(cursor=lambda: no_tasks), DAG, "old") == Activity(
        "queued", 3.0, [])
    assert run_liveness.run_activity(SimpleNamespace(cursor=lambda: _Cursor([])), DAG, "old") is None


def test_a_database_that_cannot_be_reached_raises_instead_of_reading_as_missing(monkeypatch) -> None:
    def refuse(**kw):
        raise RuntimeError("could not connect to server: Connection refused\n\tIs the server running?\n")

    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=refuse))
    monkeypatch.setattr(pd_store, "airflow_metadata_conn_settings", lambda: {"host": "db"})
    with pytest.raises(run_liveness.DatabaseUnreachable) as err:
        run_liveness.fetch_run_activity(DAG, "old")
    assert str(err.value) == "RuntimeError: could not connect to server: Connection refused Is the server running?"
    monkeypatch.setattr(pd_store, "airflow_metadata_conn_settings", lambda: {})
    with pytest.raises(run_liveness.DatabaseUnreachable, match="no Postgres connection"):
        run_liveness.fetch_run_activity(DAG, "old")
