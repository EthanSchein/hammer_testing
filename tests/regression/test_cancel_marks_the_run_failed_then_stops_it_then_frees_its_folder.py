import getpass
import json
import os
import signal
import sys
import types
from types import SimpleNamespace

import pytest

from hammer.shell import sledgehammer_cancel, sledgehammer_cli
from hammer.vlsi import run_liveness

HERE = "here.example"
DAG = "sledgehammer_Top_alice"
ME = os.getuid()


class _Error(Exception):
    pass


class _NoSuchProcess(_Error):
    pass


class _Proc:
    """A process in the fake table: signals are logged, and it exits on SIGTERM unless told to ignore it."""

    def __init__(self, world, pid, cmd, env=None, parent=None, uid=ME, ignores_term=False):
        self.world, self.pid, self._cmd, self._env, self._uid = world, pid, cmd, env, uid
        self.parent, self.ignores_term, self.alive, self._children = parent, ignores_term, True, []
        if parent is not None:
            parent._children.append(self)
        world.procs.append(self)

    def uids(self):
        return SimpleNamespace(real=self._uid)

    def cmdline(self):
        return self._cmd.split()

    def name(self):
        return self._cmd.split()[0]

    def environ(self):
        if self._env is None:
            raise _Error(self.pid)
        return self._env

    def children(self, recursive=False):
        out = []
        for child in self._children:
            if child.alive:
                out += [child] + (child.children(True) if recursive else [])
        return out

    def parents(self):
        out, p = [], self.parent
        while p is not None:
            out.append(p)
            p = p.parent
        return out

    def status(self):
        if not self.alive:
            raise _NoSuchProcess(self.pid)
        return "sleeping"

    def send_signal(self, sig):
        self.world.events.append(("TERM" if sig == signal.SIGTERM else sig, self.pid))
        if not self.ignores_term:
            self.alive = False

    def kill(self):
        self.world.events.append(("KILL", self.pid))
        self.alive = False


def _ids(run_id="old", dag_id=DAG):
    return {"HAMMER_AIRFLOW_RUN_ID": run_id, "HAMMER_AIRFLOW_DAG_ID": dag_id, "PATH": "/usr/bin"}


def _run(state="running", run_id="old", dag_id=DAG, tasks=None, started=42 * 60):
    if tasks is None:
        tasks = [["module_Top.par", "running" if state == "running" else "failed", HERE, 4242, "ti-par"],
                 ["module_Top.drc", None, "", None, "ti-drc"], ["cache_summary_", None, "", None, "ti-cs"]]
    return {"dag_id": dag_id, "run_id": run_id, "state": state, "started": started, "queued": 43 * 60,
            "tasks": tasks}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """`sledgehammer cancel` as alice from the vlsi directory of a registered Top build.

    Run 'old' holds the build folder, and its task runner, hammer-vlsi and srun run under the
    scheduler. The database half and psutil are faked; every step lands in ``events``.
    """
    w = SimpleNamespace(events=[], procs=[], runs=[_run()], fail={"was": "running", "state": "failed",
                        "changed": [["cache_summary_", "skipped"], ["module_Top.drc", "skipped"],
                                    ["module_Top.par", "failed"]]}, read_error=None, prompts=[], answers=[])
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")
    dags = tmp_path / "dags"
    dags.mkdir()
    vlsi = tmp_path / "vlsi"
    w.obj = vlsi / "build" / "Top"
    w.obj.mkdir(parents=True)
    w.dag_file = dags / f"{DAG}.py"
    w.dag_file.write_text(f'OBJ_DIR = "{w.obj}"\n')
    monkeypatch.chdir(vlsi)
    monkeypatch.setattr(sledgehammer_cli, "_dags_folder", lambda: (str(dags), "test"))
    w.lock = w.obj / ".sledgehammer-run.lock"
    w.hold = lambda run_id="old", dag_id=DAG: w.lock.write_text(json.dumps(
        {"dag_id": dag_id, "run_id": run_id, "user": "alice", "host": HERE, "pid": 4242,
         "claimed": "2026-10-18T04:00:00"}))
    w.hold()

    def db(*args):
        w.events.append(("db",) + args)
        if args[0] == "read":
            if w.read_error:
                return {"error": w.read_error}
            return {"host": HERE, "runs": w.runs}
        w.lock_when_marked = w.lock.exists()
        return w.fail

    monkeypatch.setattr(sledgehammer_cancel, "_db", db)
    original_remove = run_liveness.remove_run_lock

    def remove(lock, text):
        w.events.append(("release",))
        original_remove(lock, text)

    monkeypatch.setattr(run_liveness, "remove_run_lock", remove)
    monkeypatch.setattr(run_liveness, "this_host", lambda: HERE)

    def process(pid=None):
        pid = os.getpid() if pid is None else pid
        for p in w.procs:
            if p.pid == pid and p.alive:
                return p
        raise _NoSuchProcess(pid)

    def wait_procs(procs, timeout=None):
        return [p for p in procs if not p.alive], [p for p in procs if p.alive]

    psutil = types.ModuleType("psutil")
    psutil.__dict__.update(Error=_Error, NoSuchProcess=_NoSuchProcess, STATUS_ZOMBIE="zombie", Process=process,
                           process_iter=lambda: [p for p in w.procs if p.alive], wait_procs=wait_procs)
    monkeypatch.setitem(sys.modules, "psutil", psutil)

    w.init = _Proc(w, 1, "init", env={})
    w.shell = _Proc(w, 900, "bash", env={}, parent=w.init)
    w.me = _Proc(w, os.getpid(), "python sledgehammer cancel", env={}, parent=w.shell)
    w.scheduler = _Proc(w, 100, "/venv/bin/python /venv/bin/airflow scheduler", env={}, parent=w.init)
    w.supervisor = _Proc(w, 101, "airflow worker -- LocalExecutor: ti-par", env={}, parent=w.scheduler)
    w.runner = _Proc(w, 4242, "airflow worker -- ti-par", env={}, parent=w.supervisor, ignores_term=True)
    w.hammer = _Proc(w, 4243, "python hammer-vlsi par", env=_ids(), parent=w.runner)
    w.srun = _Proc(w, 4244, "srun --job-name=h-par innovus", env=_ids(), parent=w.hammer)
    w.other = _Proc(w, 7000, "python hammer-vlsi syn", env=_ids("other"), parent=w.init)

    class Stdin:
        tty = True

        def isatty(self):
            return self.tty

    w.stdin = Stdin()
    monkeypatch.setattr(sys, "stdin", w.stdin)

    def ask(prompt):
        w.prompts.append(prompt)
        if not w.answers:
            raise EOFError
        return w.answers.pop(0)

    monkeypatch.setattr("builtins.input", ask)
    w.cancel = lambda *argv: sledgehammer_cancel.main(list(argv))
    w.signalled = lambda: [e[1] for e in w.events if e[0] in ("TERM", "KILL")]
    return w


class TestOrder:
    def test_marks_failed_then_stops_deepest_first_then_frees_the_folder(self, world, capsys) -> None:
        assert world.cancel("--yes") == 0
        assert world.events == [("db", "read", "alice", DAG, "old"), ("db", "fail", DAG, "old"),
                                ("TERM", 4244), ("TERM", 4243), ("TERM", 4242), ("KILL", 4242), ("release",)]
        assert world.lock_when_marked and not world.lock.exists()
        out = capsys.readouterr().out
        assert (f"[sledgehammer] run old of {DAG}: running, started 42 min ago\n"
                f"  running: Top.par\n"
                f"  its processes on this host:\n"
                f"    pid 4242: airflow worker -- ti-par\n"
                f"    pid 4243: python hammer-vlsi par\n"
                f"    pid 4244: srun --job-name=h-par innovus\n"
                f"  it holds the build folder {world.obj}\n"
                f"  cancel will mark it failed, stop 3 processes on this host and free {world.obj}.\n"
                f"[sledgehammer] marked run old failed: Top.par failed; 2 pending tasks skipped\n"
                f"[sledgehammer] stopping 3 processes: SIGTERM, then SIGKILL for any still running after 10 s\n"
                f"[sledgehammer] stopped 3 of them, 1 with SIGKILL\n"
                f"[sledgehammer] freed {world.obj}\n"
                f"[sledgehammer] run old is cancelled.\n") in out

    def test_never_signals_the_scheduler_its_workers_this_command_or_other_runs(self, world) -> None:
        world.shell._env = _ids()
        world.cancel("--yes")
        assert set(world.signalled()) == {4242, 4243, 4244}
        assert world.scheduler.alive and world.supervisor.alive and world.other.alive and world.shell.alive

    def test_processes_that_appear_while_marking_are_stopped_too(self, world, monkeypatch) -> None:
        def fail_and_spawn(*args):
            world.events.append(("db",) + args)
            if args[0] == "read":
                return {"host": HERE, "runs": world.runs}
            _Proc(world, 5000, "python hammer-vlsi par", env=_ids(), parent=world.init)
            return world.fail

        monkeypatch.setattr(sledgehammer_cancel, "_db", fail_and_spawn)
        world.cancel("--yes")
        assert 5000 in world.signalled()

    def test_an_already_failed_run_has_its_orphans_stopped_and_its_folder_freed(self, world, capsys) -> None:
        world.runs = [_run("failed")]
        world.runner.alive = False
        world.hammer.parent = world.init
        assert world.cancel("--yes") == 0
        assert world.events == [("db", "read", "alice", DAG, "old"), ("TERM", 4244), ("TERM", 4243), ("release",)]
        out = capsys.readouterr().out
        assert f"  cancel will stop 2 processes on this host and free {world.obj}.\n" in out
        assert "[sledgehammer] run old is cleaned up.\n" in out

    def test_a_finished_run_with_nothing_left_needs_nothing(self, world, capsys) -> None:
        world.runs = [_run("failed")]
        for p in (world.runner, world.hammer, world.srun):
            p.alive = False
        world.lock.unlink()
        assert world.cancel("old") == 0
        assert world.prompts == [] and world.events == [("db", "read", "alice", "", "old")]
        assert ("[sledgehammer] run old is already failed, nothing of it runs on this host and it holds no "
                "build folder; nothing to do.") in capsys.readouterr().out

    def test_a_process_that_outlives_sigkill_is_reported(self, world, capsys) -> None:
        world.runner.kill = lambda: world.events.append(("KILL", 4242))
        assert world.cancel("--yes") == 1
        assert "  still running: pid 4242: airflow worker -- ti-par\n" in capsys.readouterr().out

    def test_a_task_on_another_host_is_named_with_the_command_to_run_there(self, world, capsys) -> None:
        world.runs = [_run(tasks=[["module_Top.par", "running", "other.example", 31, "ti-par"]])]
        world.cancel("--yes")
        out = capsys.readouterr().out
        assert ("  task Top.par (running) ran on host other.example; its processes there are not stopped "
                "from here\n") in out
        assert (f"  On other.example, stop what is left of it with:  sledgehammer cancel old --obj_dir {world.obj}\n"
                in out)

    def test_a_task_running_on_another_host_keeps_the_folder_locked_until_cancel_runs_there(
            self, world, capsys, monkeypatch) -> None:
        world.runs = [_run(tasks=[["module_Top.par", "running", "other.example", 31, "ti-par"]])]
        assert world.cancel("--yes") == 0
        out = capsys.readouterr().out
        assert f" and keep {world.obj} locked until cancel has run on other.example too.\n" in out
        assert (f"[sledgehammer] {world.obj} stays locked until  sledgehammer cancel old --obj_dir {world.obj}  "
                f"has run on other.example\n  If other.example is gone for good:  sledgehammer cancel old "
                f"--obj_dir {world.obj} --free\n") in out
        held = json.loads(world.lock.read_text())
        assert (held["run_id"], held["pending_hosts"]) == ("old", ["other.example"])

        finished = run_liveness.Verdict(run_liveness.FINISHED, run_liveness.Activity("failed", 60, []), [], [], None)
        monkeypatch.setattr(run_liveness, "run_verdict", lambda *a, **k: finished)
        kind, text = run_liveness.check_run_lock(str(world.obj), held, 60, user="alice")
        assert kind == run_liveness.BUSY
        assert (f"    what it left on host other.example, where  sledgehammer cancel old --obj_dir {world.obj}  "
                f"has not run yet\n") in text
        assert text.splitlines()[0] == (f"run 'old' has finished, but whether anything of it still runs in "
                                        f"{world.obj} cannot be checked.")
        assert text.endswith(f"  To free this directory, run  sledgehammer cancel old --obj_dir {world.obj}  on "
                             f"other.example.\n  If other.example is gone for good:  sledgehammer cancel old "
                             f"--obj_dir {world.obj} --free")

        monkeypatch.setattr(run_liveness, "this_host", lambda: "other.example")
        assert run_liveness.check_run_lock(str(world.obj), held, 60, user="alice")[0] == run_liveness.FINISHED
        read = sledgehammer_cancel._db
        monkeypatch.setattr(sledgehammer_cancel, "_db",
                            lambda *a: {**read(*a), "host": "other.example"} if a[0] == "read" else read(*a))
        world.runs = [_run("failed", tasks=[["module_Top.par", "failed", "other.example", 31, "ti-par"]])]
        assert world.cancel("--yes") == 0
        assert not world.lock.exists()
        assert "[sledgehammer] freed " in capsys.readouterr().out

    def test_free_releases_a_folder_kept_for_another_host(self, world) -> None:
        world.runs = [_run(tasks=[["module_Top.par", "running", "other.example", 31, "ti-par"]])]
        assert world.cancel("--yes") == 0 and world.lock.exists()
        world.runs = [_run("failed", tasks=[["module_Top.par", "failed", "other.example", 31, "ti-par"]])]
        assert world.cancel("--yes") == 0 and world.lock.exists()
        assert world.cancel("--yes", "--free") == 0
        assert not world.lock.exists()

    def test_a_finished_task_on_another_host_does_not_keep_the_folder(self, world) -> None:
        world.runs = [_run(tasks=[["module_Top.par", "running", HERE, 4242, "ti-par"],
                                  ["module_Top.syn", "success", "other.example", 30, "ti-syn"],
                                  ["module_Top.drc", "failed", "other.example", 32, "ti-drc"]])]
        assert world.cancel("--yes") == 0
        assert not world.lock.exists()


class TestConfirmation:
    def test_no_changes_without_a_yes(self, world, capsys) -> None:
        world.answers = ["n"]
        assert world.cancel() == 1
        assert world.prompts == ["Go ahead? [y/N] "]
        assert world.events == [("db", "read", "alice", DAG, "old")]
        assert world.lock.exists() and world.runner.alive
        assert "[sledgehammer] nothing was changed.\n" in capsys.readouterr().out

    def test_end_of_input_at_the_prompt_changes_nothing(self, world) -> None:
        assert world.cancel() == 1
        assert world.signalled() == [] and world.lock.exists()

    def test_yes_at_the_prompt_cancels(self, world) -> None:
        world.answers = ["y"]
        assert world.cancel() == 0
        assert ("db", "fail", DAG, "old") in world.events and not world.lock.exists()

    def test_the_yes_flag_skips_the_prompt(self, world) -> None:
        assert world.cancel("--yes") == 0
        assert world.prompts == []

    def test_without_a_terminal_it_changes_nothing_unless_told_yes(self, world, capsys) -> None:
        world.stdin.tty = False
        assert world.cancel() == 1
        assert world.prompts == [] and world.signalled() == [] and world.lock.exists()
        assert ("[sledgehammer] there is no terminal to ask in, so nothing was changed; "
                "add --yes to cancel without asking.\n") in capsys.readouterr().out
        assert world.cancel("--yes") == 0
        assert ("db", "fail", DAG, "old") in world.events


class TestRefusals:
    def test_an_unreachable_database_changes_nothing(self, world) -> None:
        world.read_error = "OperationalError: connection to server at \"db\", port 5432 failed: timeout expired"
        with pytest.raises(SystemExit) as err:
            world.cancel("--yes")
        assert err.value.code == (
            "[sledgehammer] could not read the run from the Airflow database, so nothing was changed: "
            "OperationalError: connection to server at \"db\", port 5432 failed: timeout expired\n"
            "  Check the database with:  sledgehammer postgres show")
        assert world.events == [("db", "read", "alice", DAG, "old")]
        assert world.lock.exists() and world.runner.alive

    def test_a_failed_mark_stops_nothing(self, world) -> None:
        world.fail = {"error": "OperationalError: server closed the connection unexpectedly"}
        with pytest.raises(SystemExit) as err:
            world.cancel("--yes")
        assert err.value.code == ("[sledgehammer] could not mark run old failed, so nothing was stopped: "
                                  "OperationalError: server closed the connection unexpectedly")
        assert world.signalled() == [] and world.lock.exists()

    def test_a_process_list_that_cannot_be_read_changes_nothing(self, world, monkeypatch) -> None:
        def broken(*a, **k):
            raise OSError("no /proc")

        monkeypatch.setattr(run_liveness, "local_run_processes", broken)
        with pytest.raises(SystemExit, match="could not list this host's processes, so nothing was changed: "
                                             "OSError: no /proc"):
            world.cancel("--yes")
        assert world.events == [("db", "read", "alice", DAG, "old")] and world.lock.exists()

    def test_the_run_ids_in_an_airflow_process_environment_change_nothing(self, world) -> None:
        world.scheduler._env = _ids()
        with pytest.raises(SystemExit) as err:
            world.cancel("--yes")
        assert "Airflow's own pid 100: /venv/bin/python /venv/bin/airflow scheduler" in err.value.code
        assert world.events == [("db", "read", "alice", DAG, "old")] and world.signalled() == []


class TestTarget:
    def test_the_lock_holder_is_the_default(self, world) -> None:
        world.cancel("--yes")
        assert world.events[0] == ("db", "read", "alice", DAG, "old")

    def test_a_lock_held_by_another_users_dag_is_refused(self, world) -> None:
        world.hold(dag_id="sledgehammer_Top_bob")
        with pytest.raises(SystemExit) as err:
            world.cancel("--yes")
        assert err.value.code == (f"[sledgehammer] {world.obj} is held by run old of sledgehammer_Top_bob, which "
                                  f"is not one of your DAGs, so it is not yours to cancel.")
        assert world.events == []

    def test_without_a_lock_the_dags_only_active_run_is_cancelled(self, world) -> None:
        world.lock.unlink()
        world.runs = [_run(run_id="cli_2")]
        world.cancel("--yes")
        assert world.events[:2] == [("db", "read", "alice", DAG, ""), ("db", "fail", DAG, "cli_2")]

    def test_without_a_lock_several_active_runs_are_listed(self, world) -> None:
        world.lock.unlink()
        world.runs = [_run(run_id="cli_1"), _run("queued", run_id="cli_2", started=None)]
        with pytest.raises(SystemExit) as err:
            world.cancel("--yes")
        assert err.value.code == (f"[sledgehammer] {DAG} has 2 running or queued runs:\n"
                                  f"  {'cli_1':44s} running, started 42 min ago\n"
                                  f"  {'cli_2':44s} queued, queued 43 min ago\n"
                                  f"  Cancel one with:  sledgehammer cancel <run_id>")
        assert world.events == [("db", "read", "alice", DAG, "")]

    def test_without_a_lock_or_an_active_run_there_is_nothing_to_cancel(self, world, capsys) -> None:
        world.lock.unlink()
        world.runs = []
        assert world.cancel("--yes") == 0
        assert (f"[sledgehammer] no run holds {world.obj} and {DAG} has no running or queued run; "
                f"nothing to cancel.") in capsys.readouterr().out

    def test_a_run_id_alone_is_looked_up_among_my_dags_from_any_directory(self, world, monkeypatch, tmp_path) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert world.cancel("old", "--yes") == 0
        assert world.events[:2] == [("db", "read", "alice", "", "old"), ("db", "fail", DAG, "old")]
        assert not world.lock.exists()

    def test_a_run_id_none_of_my_dags_has_is_refused(self, world) -> None:
        world.runs = []
        with pytest.raises(SystemExit) as err:
            world.cancel("nope", "--yes")
        assert err.value.code == ("[sledgehammer] none of your DAGs has a run nope in the Airflow database; "
                                  "list them with:  sledgehammer runs")

    def test_a_run_id_in_two_of_my_dags_needs_the_design(self, world) -> None:
        world.runs = [_run(), _run(dag_id="sledgehammer_Other_alice")]
        with pytest.raises(SystemExit, match="more than one of your DAGs has a run old: sledgehammer_Top_alice, "
                                             "sledgehammer_Other_alice. Pick one with -t <design>."):
            world.cancel("old", "--yes")

    def test_a_design_names_the_dag(self, world) -> None:
        world.cancel("old", "-t", "Top", "--yes")
        assert world.events[0] == ("db", "read", "alice", DAG, "old")

    def test_an_obj_dir_names_the_folder_even_when_the_dag_builds_elsewhere(self, world, tmp_path) -> None:
        ws = tmp_path / "ws" / "Top"
        ws.mkdir(parents=True)
        (ws / ".sledgehammer-run.lock").write_text(json.dumps({"dag_id": DAG, "run_id": "ws1", "user": "alice"}))
        world.runs = [_run(run_id="ws1", tasks=[])]
        assert world.cancel("--obj_dir", str(ws), "--yes") == 0
        assert world.events == [("db", "read", "alice", DAG, "ws1"), ("db", "fail", DAG, "ws1"), ("release",)]
        assert not (ws / ".sledgehammer-run.lock").exists() and world.lock.exists()

    def test_a_holder_missing_from_the_database_still_has_its_processes_stopped(self, world, capsys) -> None:
        world.runs = []
        world.runner.alive = False
        world.hammer.parent = world.init
        assert world.cancel("--yes") == 0
        assert world.events == [("db", "read", "alice", DAG, "old"), ("TERM", 4244), ("TERM", 4243), ("release",)]
        assert f"[sledgehammer] run old of {DAG} is not in the Airflow database\n" in capsys.readouterr().out


def test_the_real_database_half_refuses_when_the_database_is_down(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path))
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "postgresql+psycopg2://u:p@127.0.0.1:1/airflow")
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "5")
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(sledgehammer_cancel.__file__))))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        [repo] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]))
    got = sledgehammer_cancel._db("read", "alice", DAG, "old")
    assert list(got) == ["error"] and got["error"]


def test_sledgehammer_cancel_loads_the_stack_and_does_not_pass_through_to_airflow(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(sledgehammer_cli, "_load_stack_env", lambda: seen.append("stack env"))
    monkeypatch.setattr(sledgehammer_cli, "_load_secrets", lambda: seen.append("secrets"))
    monkeypatch.setattr(sledgehammer_cancel, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(os, "execve", lambda *a: pytest.fail("passed through to airflow"))
    monkeypatch.setenv("AIRFLOW_HOME", "unset below")
    monkeypatch.delenv("AIRFLOW_HOME")
    monkeypatch.setattr(sys, "argv", ["sledgehammer", "cancel", "old", "--yes"])
    assert sledgehammer_cli.main() == 0
    assert seen == ["stack env", "secrets", ["old", "--yes"]]
    assert os.environ["AIRFLOW_HOME"] == sledgehammer_cli.REPO
    assert "  sledgehammer cancel " in sledgehammer_cli.__doc__


def test_a_lock_of_another_dag_with_the_same_run_id_is_left_alone(world, capsys) -> None:
    for p in (world.runner, world.hammer, world.srun):
        p.alive = False
    world.hold("old", "sledgehammer_Top_bob")
    world.runs = [_run("failed")]
    assert world.cancel("old", "--yes", "--free") == 0
    assert json.loads(world.lock.read_text())["dag_id"] == "sledgehammer_Top_bob"
    assert "freed" not in capsys.readouterr().out


def test_free_names_the_folder_so_it_works_for_a_workspace_folder(world, tmp_path, monkeypatch) -> None:
    for p in (world.runner, world.hammer, world.srun):
        p.alive = False
    world.lock.unlink()
    ws = tmp_path / "ws" / "Top"
    ws.mkdir(parents=True)
    lock = ws / ".sledgehammer-run.lock"
    lock.write_text(json.dumps({"dag_id": DAG, "run_id": "old", "user": "alice", "host": HERE, "claimed": "x"}))
    world.runs = [_run(tasks=[["module_Top.par", "running", "other.example", 31, "ti-par"]])]
    assert world.cancel("old", "--obj_dir", str(ws), "--yes") == 0
    finished = run_liveness.Verdict(run_liveness.FINISHED, run_liveness.Activity("failed", 60, []), [], [], None)
    real = run_liveness.run_verdict
    monkeypatch.setattr(run_liveness, "run_verdict", lambda *a, **k: finished)
    text = run_liveness.check_run_lock(str(ws), json.loads(lock.read_text()), 60, user="alice")[1]
    monkeypatch.setattr(run_liveness, "run_verdict", real)
    argv = text.splitlines()[-1].split(":  ", 1)[1].split()[2:]
    world.runs = [_run("failed", tasks=[["module_Top.par", "failed", "other.example", 31, "ti-par"]])]
    assert world.cancel(*argv, "--yes") == 0
    assert not lock.exists()


def test_a_lock_of_another_dag_is_left_alone_while_my_run_of_that_id_is_cancelled(world, capsys) -> None:
    world.hold("old", "sledgehammer_Top_bob")
    assert world.cancel("old", "--yes") == 0
    out = capsys.readouterr().out
    assert json.loads(world.lock.read_text())["dag_id"] == "sledgehammer_Top_bob"
    assert "holds the build folder" not in out and "freed" not in out


def test_the_printed_cancel_frees_a_folder_whose_run_is_gone_from_the_database(world, monkeypatch) -> None:
    world.runner.alive = False
    world.hammer.parent = world.init
    monkeypatch.setattr(run_liveness, "fetch_run_activity", lambda dag_id, run_id: None)
    text = run_liveness.check_run_lock(str(world.obj), json.loads(world.lock.read_text()), 60, user="alice")[1]
    argv = text.splitlines()[-1].split(":  ", 1)[1].split()[2:]
    world.runs = []
    assert world.cancel(*argv, "--yes") == 0
    assert not world.lock.exists() and not world.hammer.alive
