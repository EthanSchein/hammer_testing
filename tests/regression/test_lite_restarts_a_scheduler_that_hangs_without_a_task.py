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
API_PORT = "18123"

# Bodies of /api/v2/monitor/health from a real Airflow 3.1.8 lite stack: running, during a database outage, and with
# the scheduler's loop dead after that outage while the scheduler process stays alive.
HEALTHY = ('{"metadatabase":{"status":"healthy"},"scheduler":{"status":"healthy","latest_scheduler_heartbeat":'
           '"2026-10-18T08:39:09.384508+00:00"},"triggerer":{"status":null,"latest_triggerer_heartbeat":null},'
           '"dag_processor":{"status":"healthy","latest_dag_processor_heartbeat":"2026-10-18T08:39:15.012210+00:00"}}')
DB_DOWN = ('{"metadatabase":{"status":"unhealthy"},"scheduler":{"status":"unhealthy",'
           '"latest_scheduler_heartbeat":null},"triggerer":{"status":"unhealthy","latest_triggerer_heartbeat":null},'
           '"dag_processor":{"status":"unhealthy","latest_dag_processor_heartbeat":null}}')
STUCK = ('{"metadatabase":{"status":"healthy"},"scheduler":{"status":"unhealthy","latest_scheduler_heartbeat":'
         '"2026-10-18T08:39:09.384508+00:00"},"triggerer":{"status":null,"latest_triggerer_heartbeat":null},'
         '"dag_processor":{"status":"healthy","latest_dag_processor_heartbeat":"2026-10-18T08:39:35.119683+00:00"}}')

# Process tables as "pgid pid ppid stat wchan args", where @S@ is the scheduler's pid: its log prefixer and executor
# worker are its children, and the task process is the worker's child.
TASK_ALIVE = """\
1 1 0 Ss do_epoll_wait /sbin/init
@S@ @S@ 1 S do_wait airflow scheduler --skip-serve-logs
@S@ 900001 @S@ S pipe_read bash scripts/sledge-lite-supervisor.sh
@S@ 900002 @S@ S do_epoll_wait airflow worker -- LocalExecutor: 01a14e29-f5bb-73f5-b8ab-a13ff632f9dd
@S@ 900003 900002 S do_wait airflow worker -- 01a14e29-f5bb-73f5-b8ab-a13ff632f9dd
@S@ 900004 900003 S hrtimer_nanosleep sleep 150
"""
# The same after the worker stopped for good and its task process died: the task is a zombie under the stopped
# worker, and what the task ran is left in the group under init.
TASK_DEAD = """\
1 1 0 Ss do_epoll_wait /sbin/init
@S@ @S@ 1 S do_wait airflow scheduler --skip-serve-logs
@S@ 900001 @S@ S pipe_read bash scripts/sledge-lite-supervisor.sh
@S@ 900002 @S@ T do_signal_stop airflow worker -- LocalExecutor: 01a14e29-f5bb-73f5-b8ab-a13ff632f9dd
@S@ 900003 900002 Z - [airflow worker ] <defunct>
@S@ 900004 1 S hrtimer_nanosleep sleep 150
"""

# A task runner Airflow left behind: its worker gave up on it and went back to idle, so it is no running task.
TASK_LEFT = """\
1 1 0 Ss do_epoll_wait /sbin/init
@S@ @S@ 1 S do_wait airflow scheduler --skip-serve-logs
@S@ 900001 @S@ S pipe_read bash scripts/sledge-lite-supervisor.sh
@S@ 900002 @S@ S do_epoll_wait airflow worker -- LocalExecutor: <idle>
@S@ 900003 900002 S unix_stream_read_generic airflow worker -- 01a14e29-f5bb-73f5-b8ab-a13ff632f9dd
"""

# The scheduler crashes on its first FAKE_SCHEDULER_CRASHES starts, and ignores TERM when FAKE_SCHEDULER_HANGS is set.
FAKE_AIRFLOW = """#!/usr/bin/env bash
comp=$1
echo "start $comp $$" >> "$FAKE_LOG"
if [ "$comp" = scheduler ] && [ "$(grep -c '^start scheduler ' "$FAKE_LOG")" -le "${FAKE_SCHEDULER_CRASHES:-0}" ]; then
    exit 1
fi
if [ "$comp" = scheduler ] && [ -n "${FAKE_SCHEDULER_HANGS:-}" ]; then
    trap 'echo "ignored $comp $$" >> "$FAKE_LOG"' TERM
else
    trap 'echo "term $comp $$" >> "$FAKE_LOG"; exit 0' TERM
fi
while :; do sleep 1 & wait $!; done
"""

# Answers with the body in $FAKE_HEALTH, or fails when that file says fail.
FAKE_CURL = """#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
body=$(cat "$FAKE_HEALTH")
[ "$body" = fail ] && exit 7
printf '%s' "$body"
"""

# Prints the table in $FAKE_PS for the newest scheduler, in the columns asked for, or fails when that file says fail.
FAKE_PS = """#!/usr/bin/env bash
echo "ps $*" >> "$FAKE_LOG"
s=$(awk '$1 == "start" && $2 == "scheduler" { p = $3 } END { print p }' "$FAKE_LOG")
[ "$(cat "$FAKE_PS")" = fail ] && exit 1
case "$*" in
    *pgid,*) echo "   PGID     PID    PPID STAT WCHAN COMMAND"
             sed "s/@S@/$s/g" "$FAKE_PS" ;;
    *) sed "s/@S@/$s/g" "$FAKE_PS" | awk '{ line = $2 " " $3 " " $4; for (i = 6; i <= NF; i++) line = line " " $i
                                            print line }' ;;
esac
"""


class Stack:
    def __init__(self, tmp_path):
        self.bin = tmp_path / "bin"
        fake_postgres.executable(self.bin / "airflow", FAKE_AIRFLOW)
        fake_postgres.executable(self.bin / "curl", FAKE_CURL)
        fake_postgres.executable(self.bin / "ps", FAKE_PS)
        self.log = tmp_path / "airflow.log"
        self.health = tmp_path / "health.json"
        self.table = tmp_path / "ps.txt"
        self.out = tmp_path / "supervisor.out"
        self.proc = None

    def feed(self, health=None, table=None):
        if health is not None:
            self.health.write_text(health)
        if table is not None:
            self.table.write_text(table)

    def start(self, path=None, **env):
        env = {**os.environ, "PATH": path or f"{self.bin}{os.pathsep}{os.environ['PATH']}",
               "FAKE_LOG": str(self.log), "FAKE_HEALTH": str(self.health), "FAKE_PS": str(self.table),
               "AIRFLOW__API__PORT": API_PORT, "SLEDGE_LITE_POLL": "0.2", "SLEDGE_LITE_RESTART_DELAY": "0",
               "SLEDGE_LITE_WATCH_EVERY": "1", "SLEDGE_LITE_STUCK_AFTER": "2", **env}
        with open(self.out, "w") as out:
            self.proc = subprocess.Popen([shutil.which("bash"), str(SUPERVISOR)], stdout=out,
                                         stderr=subprocess.STDOUT, env=env, start_new_session=True)
        return self.proc

    def lines(self, kind):
        if not self.log.exists():
            return []
        return [line.split() for line in self.log.read_text().splitlines() if line.startswith(kind + " ")]

    def starts(self, comp):
        return [int(parts[2]) for parts in self.lines("start") if parts[1] == comp]

    def output(self):
        return self.out.read_text()

    def wait_for(self, cond, timeout=20):
        deadline = time.time() + timeout
        while not cond():
            assert time.time() < deadline, self.output()
            time.sleep(0.1)

    def checks(self, n):
        """Waits until the health has been asked for n more times."""
        want = len(self.lines("curl")) + n
        self.wait_for(lambda: len(self.lines("curl")) >= want)

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        assert self.proc.wait(20) == 143


def _exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def stack(tmp_path):
    stack = Stack(tmp_path)
    yield stack
    if stack.proc and stack.proc.poll() is None:
        os.killpg(stack.proc.pid, signal.SIGKILL)
    for pid in stack.starts("scheduler") + stack.starts("dag-processor") + stack.starts("api-server"):
        for kill in (os.killpg, os.kill):
            try:
                kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def test_a_hung_scheduler_is_left_running_while_a_task_runs_under_it(stack) -> None:
    stack.feed(STUCK, TASK_ALIVE)
    stack.start()
    stack.wait_for(lambda: stack.lines("curl"))
    stack.checks(4)
    out = stack.output()
    assert out.count("[lite] the scheduler has stopped scheduling but still runs a task; it is restarted after that "
                     "task ends") == 1
    assert "stopping it" not in out and len(stack.starts("scheduler")) == 1
    assert not stack.lines("term") and not stack.lines("ignored")
    assert stack.lines("curl")[0][-1] == f"http://127.0.0.1:{API_PORT}/api/v2/monitor/health"
    stack.stop()


def test_a_hung_scheduler_is_restarted_once_its_task_has_ended(stack) -> None:
    stack.feed(STUCK, TASK_ALIVE)
    stack.start()
    stack.wait_for(lambda: "still runs a task" in stack.output())
    assert len(stack.starts("scheduler")) == 1
    stack.feed(table=TASK_DEAD)
    stack.wait_for(lambda: len(stack.starts("scheduler")) == 2)
    assert "stopping it so it can be restarted" in stack.output()
    stack.stop()


def test_a_hung_scheduler_without_a_task_is_stopped_and_restarted(stack) -> None:
    stack.feed(STUCK, TASK_DEAD)
    stack.start(FAKE_SCHEDULER_CRASHES="2", FAKE_SCHEDULER_HANGS="1", SLEDGE_LITE_RESTART_DELAY="1",
                SLEDGE_LITE_STOP_TIMEOUT="1")
    stack.wait_for(lambda: len(stack.starts("scheduler")) == 4, timeout=40)
    hung = stack.starts("scheduler")[2]
    out = stack.output()
    assert "[lite] the scheduler has stopped scheduling for over 2s without exiting, and no task runs under it; " \
           "stopping it so it can be restarted. Its processes:" in out
    assert "[lite]      PGID     PID    PPID STAT WCHAN COMMAND" in out
    assert f"[lite]   {hung} 900003 900002 Z - [airflow worker ] <defunct>" in out
    assert f"[lite]   {hung} 900004 1 S hrtimer_nanosleep sleep 150" in out
    assert "/sbin/init" not in out
    assert [int(p[2]) for p in stack.lines("ignored") if p[1] == "scheduler"] == [hung]
    assert "[lite] the scheduler did not stop within 1s; killing it" in out
    restart = out.split("Its processes:")[1].split("[lite] started scheduler")[0]
    assert "restarting the scheduler in 1s" in restart
    stack.wait_for(lambda: not _exists(hung), timeout=10)
    assert len(stack.starts("dag-processor")) == 1 and len(stack.starts("api-server")) == 1
    assert "Killed" not in out
    stack.stop()


def test_a_task_runner_left_under_an_idle_worker_does_not_hold_back_the_restart(stack) -> None:
    stack.feed(STUCK, TASK_LEFT)
    stack.start()
    stack.wait_for(lambda: len(stack.starts("scheduler")) == 2)
    out = stack.output()
    assert "stopping it so it can be restarted" in out and "still runs a task" not in out
    stack.stop()


@pytest.mark.parametrize("health, table", [
    (HEALTHY, TASK_DEAD),
    (DB_DOWN, TASK_DEAD),
    ("fail", TASK_DEAD),
    ("<html><body>Bad Gateway</body></html>", TASK_DEAD),
    (STUCK, "fail"),
    (STUCK, "1 1 0 Ss do_epoll_wait /sbin/init\n"),
], ids=["healthy", "database-down", "curl-fails", "not-json", "ps-fails", "ps-without-the-scheduler"])
def test_the_scheduler_is_left_alone_when_the_health_does_not_prove_it_hung(stack, health, table) -> None:
    stack.feed(health, table)
    stack.start()
    stack.wait_for(lambda: stack.lines("curl"))
    stack.checks(4)
    out = stack.output()
    assert "stopping it" not in out and "still runs a task" not in out
    assert len(stack.starts("scheduler")) == 1 and not stack.lines("term")
    stack.stop()


def test_without_curl_the_stack_says_a_hung_scheduler_is_not_restarted(stack, tmp_path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("bash", "sleep", "tr", "cat", "grep", "awk", "sed"):
        os.symlink(shutil.which(name), tools / name)
    (stack.bin / "curl").unlink()
    stack.feed(STUCK, TASK_DEAD)
    stack.start(path=f"{stack.bin}{os.pathsep}{tools}")
    stack.wait_for(lambda: len(stack.starts("api-server")) == 1)
    time.sleep(5)
    out = stack.output()
    assert out.count("[lite] curl is not installed, so a scheduler that hangs without exiting is not restarted") == 1
    assert "stopping it" not in out and "not found" not in out
    assert len(stack.starts("scheduler")) == 1
    stack.stop()
