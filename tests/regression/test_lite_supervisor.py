import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

SUPERVISOR = fake_postgres.REPO / "scripts" / "sledge-lite-supervisor.sh"

# FAKE_<COMPONENT>: run (default), crash, crash-once, exit7, ignore-term, spawn-child or spawn-stubborn.
FAKE_AIRFLOW = """#!/usr/bin/env bash
comp=$1
echo "start $comp $$ $*" >> "$FAKE_LOG"
echo "hello from $comp"
var=FAKE_$(echo "$comp" | tr 'a-z-' 'A-Z_')
mode=${!var:-run}
case $mode in
    crash) exit 1 ;;
    crash-once) [ -e "$FAKE_LOG.$comp" ] || { touch "$FAKE_LOG.$comp"; exit 1; } ;;
    exit7) sleep 1; exit 7 ;;
    spawn-child) sleep 1000 & echo "child $comp $!" >> "$FAKE_LOG" ;;
    spawn-stubborn) (trap '' TERM; sleep 1000) & echo "child $comp $!" >> "$FAKE_LOG" ;;
esac
if [ "$mode" = ignore-term ]; then
    trap 'echo "ignored $comp $$" >> "$FAKE_LOG"' TERM
else
    trap 'echo "term $comp $$" >> "$FAKE_LOG"; echo "bye from $comp"; exit 0' TERM
fi
echo "ready $comp $$" >> "$FAKE_LOG"
while :; do sleep 1 & wait $!; done
"""


class Lite:
    def __init__(self, tmp_path):
        self.bin = tmp_path / "bin"
        fake_postgres.executable(self.bin / "airflow", FAKE_AIRFLOW)
        self.log = tmp_path / "airflow.log"
        self.out = tmp_path / "supervisor.out"
        self.proc = None

    def start(self, **modes):
        env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(self.log),
               "SLEDGE_LITE_POLL": "0.2", "SLEDGE_LITE_RESTART_DELAY": "0", **modes}
        with open(self.out, "w") as out:
            self.proc = subprocess.Popen(["bash", str(SUPERVISOR)], stdout=out, stderr=subprocess.STDOUT,
                                         env=env, start_new_session=True)
        return self.proc

    def lines(self, kind):
        if not self.log.exists():
            return []
        return [line.split() for line in self.log.read_text().splitlines() if line.startswith(kind + " ")]

    def count(self, kind, comp):
        return sum(1 for parts in self.lines(kind) if parts[1] == comp)

    def order(self, kind):
        return [parts[1] for parts in self.lines(kind)]

    def output(self):
        return self.out.read_text()

    def wait_for(self, cond, timeout=20):
        deadline = time.time() + timeout
        while not cond():
            assert time.time() < deadline, self.output()
            time.sleep(0.1)

    def started_all(self):
        self.wait_for(lambda: {p[1] for p in self.lines("ready")} >= {"scheduler", "dag-processor", "api-server"})

    def pids(self):
        return [int(p[2]) for p in self.lines("start")] + [int(p[2]) for p in self.lines("child")]

    def assert_all_gone(self):
        for pid in self.pids():
            self.wait_for(lambda: not _exists(pid), timeout=10)


def _exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def lite(tmp_path):
    lite = Lite(tmp_path)
    yield lite
    if lite.proc and lite.proc.poll() is None:
        os.killpg(lite.proc.pid, signal.SIGKILL)
    for pid in lite.pids():
        for kill in (os.killpg, os.kill):
            try:
                kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def test_the_three_components_start_and_stop_on_term(lite) -> None:
    proc = lite.start()
    lite.started_all()
    assert [p[3:] for p in lite.lines("start") if p[1] == "scheduler"] == [["scheduler", "--skip-serve-logs"]]
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143
    assert sorted(lite.order("term")) == ["api-server", "dag-processor", "scheduler"]
    lite.assert_all_gone()


def test_the_api_server_stops_after_the_scheduler(lite) -> None:
    proc = lite.start()
    lite.started_all()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143
    terms = lite.order("term")
    assert terms.index("scheduler") < terms.index("api-server")


def test_output_is_shown_while_running_and_kept_on_stop(lite) -> None:
    proc = lite.start()
    lite.started_all()
    lite.wait_for(lambda: all(f"{c} | hello from {c}" in lite.output()
                              for c in ("scheduler", "dag-processor", "api-server")))
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143
    for comp in ("scheduler", "dag-processor", "api-server"):
        assert f"{comp} | bye from {comp}" in lite.output()


def test_ctrl_c_stops_everything(lite) -> None:
    proc = lite.start()
    lite.started_all()
    proc.send_signal(signal.SIGINT)
    assert proc.wait(20) == 130
    assert len(lite.lines("term")) == 3
    lite.assert_all_gone()


def test_a_crashed_dag_processor_is_restarted(lite) -> None:
    proc = lite.start(FAKE_DAG_PROCESSOR="crash-once")
    lite.wait_for(lambda: lite.count("start", "dag-processor") == 2)
    lite.started_all()
    assert lite.count("start", "scheduler") == 1 and proc.poll() is None
    assert "the dag-processor exited (status 1)" in lite.output()
    assert "restarting the dag-processor in 0s" in lite.output()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143


def test_a_dag_processor_that_keeps_crashing_is_left_down_while_the_scheduler_runs(lite) -> None:
    proc = lite.start(FAKE_DAG_PROCESSOR="crash", SLEDGE_LITE_RESTART_FOR="2")
    lite.wait_for(lambda: "not restarting it" in lite.output())
    starts = lite.count("start", "dag-processor")
    time.sleep(1)
    assert starts >= 2 and lite.count("start", "dag-processor") == starts and proc.poll() is None
    assert "the dag-processor has kept exiting within a minute of starting for 2s (last status 1)" in lite.output()
    assert "new or changed DAG files are not picked up" in lite.output()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143
    assert sorted(lite.order("term")) == ["api-server", "scheduler"]


def test_an_api_server_that_keeps_crashing_stops_the_stack(lite) -> None:
    proc = lite.start(FAKE_API_SERVER="crash", SLEDGE_LITE_RESTART_FOR="2")
    assert proc.wait(20) == 1
    assert lite.count("start", "api-server") >= 2
    assert "stopping the stack, because tasks report through it" in lite.output()
    assert sorted(lite.order("term")) == ["dag-processor", "scheduler"]
    lite.assert_all_gone()


def test_a_crashed_scheduler_is_restarted_and_the_rest_keeps_running(lite) -> None:
    proc = lite.start(FAKE_SCHEDULER="crash-once")
    lite.wait_for(lambda: lite.count("start", "scheduler") == 2)
    lite.started_all()
    assert "the scheduler exited (status 1); giving its tasks up to 30s to stop" in lite.output()
    assert "restarting the scheduler in 0s" in lite.output()
    assert lite.count("start", "dag-processor") == 1 and lite.count("start", "api-server") == 1
    assert proc.poll() is None and lite.order("term") == []
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143


def test_a_scheduler_that_keeps_exiting_keeps_being_restarted(lite) -> None:
    proc = lite.start(FAKE_SCHEDULER="exit7")
    lite.wait_for(lambda: lite.count("start", "scheduler") >= 7, timeout=40)
    out = lite.output()
    assert out.count("5 times in a row; it keeps being restarted, a minute apart at most, until it stays up or "
                     "15 min have passed") == 1
    assert "the scheduler exited (status 7)" in out
    assert proc.poll() is None and lite.order("term") == []
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143
    lite.assert_all_gone()


def test_a_killed_scheduler_leaves_nothing_of_its_group_behind(lite) -> None:
    proc = lite.start(FAKE_SCHEDULER="spawn-child")
    lite.started_all()
    lite.wait_for(lambda: lite.lines("child"))
    leader = next(int(p[2]) for p in lite.lines("start") if p[1] == "scheduler")
    child = int(lite.lines("child")[0][2])
    os.kill(leader, signal.SIGKILL)
    lite.wait_for(lambda: lite.count("start", "scheduler") == 2)
    lite.wait_for(lambda: not _exists(child), timeout=10)
    assert "the scheduler exited (status 137)" in lite.output()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143
    lite.assert_all_gone()


@pytest.mark.parametrize("sig, status", [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_a_stop_during_the_restart_wait_is_prompt(lite, sig, status) -> None:
    proc = lite.start(FAKE_SCHEDULER="crash", SLEDGE_LITE_RESTART_DELAY="40")
    lite.wait_for(lambda: "restarting the scheduler in 40s" in lite.output())
    t0 = time.monotonic()
    proc.send_signal(sig)
    assert proc.wait(20) == status
    assert time.monotonic() - t0 < 10
    lite.assert_all_gone()


@pytest.mark.parametrize("value, want", [("0.5", 5), ("5s", 5), ("08", 8), ("-1", 5), ("1234567", 60),
                                         ("0000003", 3), ("000", 0)])
def test_a_restart_delay_that_is_not_whole_seconds_does_not_stop_the_supervisor(lite, value, want) -> None:
    proc = lite.start(FAKE_SCHEDULER="crash-once", SLEDGE_LITE_RESTART_DELAY=value,
                      SLEDGE_LITE_STOP_TIMEOUT="soon")
    lite.wait_for(lambda: "restarting the scheduler in" in lite.output())
    assert "giving its tasks up to 30s to stop" in lite.output()
    assert f"restarting the scheduler in {want}s" in lite.output()
    assert proc.poll() is None
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143
    lite.assert_all_gone()


def test_a_scheduler_that_keeps_failing_to_start_stops_the_stack_after_the_limit(lite) -> None:
    proc = lite.start(FAKE_SCHEDULER="exit7", SLEDGE_LITE_RESTART_FOR="4")
    assert proc.wait(40) == 7
    out = lite.output()
    assert "the scheduler has kept exiting within a minute of starting for 4s (last status 7); stopping the " \
           "dag-processor and api-server" in out
    assert lite.count("start", "scheduler") >= 3
    assert sorted(lite.order("term")) == ["api-server", "dag-processor"]
    lite.assert_all_gone()


def test_the_others_are_watched_while_the_scheduler_waits_to_restart(lite) -> None:
    proc = lite.start(FAKE_SCHEDULER="crash", FAKE_DAG_PROCESSOR="exit7", SLEDGE_LITE_RESTART_DELAY="40")
    lite.wait_for(lambda: "restarting the scheduler in 40s" in lite.output())
    lite.wait_for(lambda: "restarting the dag-processor in 40s" in lite.output())
    assert "the dag-processor exited (status 7)" in lite.output() and lite.count("start", "scheduler") == 1
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143
    lite.assert_all_gone()


def test_a_stop_while_the_schedulers_tasks_wind_down_still_logs_its_exit(lite) -> None:
    proc = lite.start(FAKE_SCHEDULER="spawn-stubborn", SLEDGE_LITE_STOP_TIMEOUT="3")
    lite.started_all()
    lite.wait_for(lambda: lite.lines("child"))
    leader = next(int(p[2]) for p in lite.lines("start") if p[1] == "scheduler")
    os.kill(leader, signal.SIGKILL)
    lite.wait_for(lambda: "the scheduler exited (status 137); giving its tasks up to 3s to stop" in lite.output())
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143
    lite.assert_all_gone()


def test_the_restart_wait_grows_after_each_quick_exit(lite) -> None:
    proc = lite.start(FAKE_SCHEDULER="crash", SLEDGE_LITE_RESTART_DELAY="1")
    lite.wait_for(lambda: lite.count("start", "scheduler") == 3, timeout=30)
    assert "restarting the scheduler in 1s" in lite.output() and "restarting the scheduler in 2s" in lite.output()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143
    lite.assert_all_gone()


def test_a_component_that_ignores_term_is_killed_after_the_timeout(lite) -> None:
    proc = lite.start(FAKE_API_SERVER="ignore-term", SLEDGE_LITE_STOP_TIMEOUT="1")
    lite.started_all()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(30) == 143
    assert "api-server did not stop within 1s; killing it" in lite.output()
    lite.assert_all_gone()
