import os
import re
import shutil
import signal
import subprocess
import sys
import time

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

SUPERVISOR = fake_postgres.REPO / "scripts" / "sledge-lite-supervisor.sh"
COMPONENTS = ("scheduler", "dag-processor", "api-server")

# FAKE_<COMPONENT>=crash makes that component exit at once, and every component does while the file $DB_DOWN exists.
FAKE_AIRFLOW = """#!/usr/bin/env bash
comp=$1
echo "start $comp $$" >> "$FAKE_LOG"
var=FAKE_$(echo "$comp" | tr 'a-z-' 'A-Z_')
if [ "${!var:-run}" = crash ] || [ -e "${DB_DOWN:-}" ]; then
    exit 1
fi
trap 'echo "term $comp $$" >> "$FAKE_LOG"; exit 0' TERM
while :; do sleep 1 & wait $!; done
"""


class Stack:
    def __init__(self, tmp_path):
        self.bin = tmp_path / "bin"
        fake_postgres.executable(self.bin / "airflow", FAKE_AIRFLOW)
        self.log = tmp_path / "airflow.log"
        self.out = tmp_path / "supervisor.out"
        self.proc = None

    def start(self, **env):
        env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(self.log),
               "SLEDGE_LITE_POLL": "0.2", **env}
        with open(self.out, "w") as out:
            self.proc = subprocess.Popen(["bash", str(SUPERVISOR)], stdout=out, stderr=subprocess.STDOUT,
                                         env=env, start_new_session=True)
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
    for pid in [int(parts[2]) for parts in stack.lines("start")]:
        for kill in (os.killpg, os.kill):
            try:
                kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def test_a_dag_processor_that_keeps_crashing_waits_longer_before_each_restart(stack) -> None:
    proc = stack.start(FAKE_DAG_PROCESSOR="crash", SLEDGE_LITE_RESTART_DELAY="1")
    stack.wait_for(lambda: len(stack.starts("dag-processor")) >= 4, timeout=30)
    out = stack.output()
    assert re.findall(r"restarting the dag-processor in (\d+)s", out)[:3] == ["1", "2", "4"]
    assert "not restarting it" not in out and len(stack.starts("scheduler")) == 1 and proc.poll() is None
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143


def test_an_api_server_that_keeps_crashing_keeps_being_restarted(stack) -> None:
    proc = stack.start(FAKE_API_SERVER="crash", SLEDGE_LITE_RESTART_DELAY="0")
    stack.wait_for(lambda: "the api-server exited within a minute of starting 5 times in a row; it keeps being "
                           "restarted, 15s apart at most, until it stays up or 15 min have passed" in stack.output())
    assert proc.poll() is None and "stopping the stack" not in stack.output()
    assert len(stack.starts("scheduler")) == 1 and len(stack.starts("dag-processor")) == 1
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(20) == 143


def test_the_api_server_waits_less_than_the_others_between_restarts(stack) -> None:
    proc = stack.start(FAKE_API_SERVER="crash", FAKE_DAG_PROCESSOR="crash", SLEDGE_LITE_RESTART_DELAY="8")
    stack.wait_for(lambda: "restarting the api-server in 15s" in stack.output()
                   and "restarting the dag-processor in 16s" in stack.output(), timeout=30)
    assert re.findall(r"restarting the api-server in (\d+)s", stack.output()) == ["8", "15"]
    t0 = time.monotonic()
    proc.send_signal(signal.SIGINT)
    assert proc.wait(20) == 130
    assert time.monotonic() - t0 < 10


def test_every_component_is_up_again_after_a_database_outage(stack, tmp_path) -> None:
    down = tmp_path / "db-down"
    down.touch()
    proc = stack.start(DB_DOWN=str(down), SLEDGE_LITE_RESTART_DELAY="1")
    stack.wait_for(lambda: all(len(stack.starts(c)) >= 4 for c in COMPONENTS), timeout=40)
    down.unlink()

    def latest():
        return {c: stack.starts(c)[-1] for c in COMPONENTS}

    deadline = time.time() + 60
    while True:
        up = latest()
        if all(_exists(pid) for pid in up.values()):
            time.sleep(3)
            if latest() == up and all(_exists(pid) for pid in up.values()):
                break
        assert time.time() < deadline, stack.output()
        time.sleep(0.5)
    assert proc.poll() is None and "not restarting it" not in stack.output()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(40) == 143
    assert sorted(parts[1] for parts in stack.lines("term")) == sorted(COMPONENTS)
    for pid in up.values():
        stack.wait_for(lambda: not _exists(pid), timeout=10)
