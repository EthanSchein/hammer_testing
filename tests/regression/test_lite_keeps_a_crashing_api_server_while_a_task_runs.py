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

# The api-server exits at once; the scheduler's worker runs fake-task, a task process that runs until it is stopped.
FAKE_AIRFLOW = """#!/usr/bin/env bash
comp=$1
echo "start $comp $$" >> "$FAKE_LOG"
[ "$comp" = api-server ] && exit 1
[ "$comp" = scheduler ] && bash -c 'fake-task & wait' &
trap 'echo "term $comp $$" >> "$FAKE_LOG"; exit 0' TERM
while :; do sleep 1 & wait $!; done
"""
FAKE_TASK = """#!/usr/bin/env bash
echo "task $$" >> "$FAKE_LOG"
trap 'echo "stopped task $$" >> "$FAKE_LOG"; exit 0' TERM
while :; do sleep 1 & wait $!; done
"""


def _log(path):
    return path.read_text().splitlines() if path.exists() else []


def test_a_crashing_api_server_does_not_stop_the_stack_while_a_task_runs(tmp_path) -> None:
    fake_postgres.executable(tmp_path / "bin" / "airflow", FAKE_AIRFLOW)
    fake_postgres.executable(tmp_path / "bin" / "fake-task", FAKE_TASK)
    log, out = tmp_path / "airflow.log", tmp_path / "supervisor.out"
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(log),
           "SLEDGE_LITE_POLL": "0.2", "SLEDGE_LITE_RESTART_DELAY": "0", "SLEDGE_LITE_RESTART_FOR": "2"}
    with open(out, "w") as f:
        proc = subprocess.Popen(["bash", str(SUPERVISOR)], stdout=f, stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)
    try:
        deadline = time.time() + 20
        while "a task still runs under the scheduler" not in out.read_text():
            assert proc.poll() is None and time.time() < deadline, out.read_text()
            time.sleep(0.1)
        time.sleep(2)
        assert proc.poll() is None and not any(line.startswith("stopped task") for line in _log(log))
        task = next(int(line.split()[1]) for line in _log(log) if line.startswith("task "))
        os.kill(task, signal.SIGKILL)
        assert proc.wait(30) == 1
        assert "stopping the stack, because tasks report through it" in out.read_text()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        for line in _log(log):
            if line.startswith(("start ", "task ")):
                pid = int(line.split()[-1])
                for kill in (os.killpg, os.kill):
                    try:
                        kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
