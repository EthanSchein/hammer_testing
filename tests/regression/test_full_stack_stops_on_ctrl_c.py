import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

# The shutdown path of Airflow 3.1's StandaloneCommand: on KeyboardInterrupt it TERMs each component, then joins
# the thread reading its output, which ends only once every process holding that output pipe has exited.
STUBS = {
    "airflow/__init__.py": "",
    "airflow/cli/__init__.py": "",
    "airflow/cli/commands/__init__.py": "",
    "airflow/cli/commands/standalone_command.py":
        "import subprocess, threading, time\n"
        "from collections import deque\n"
        "class StandaloneCommand:\n"
        "    def __init__(self):\n"
        "        self.subcommands = {}\n"
        "        self.output_queue = deque()\n"
        "    def run(self):\n"
        "        for name in ('scheduler', 'dag-processor', 'api-server', 'triggerer'):\n"
        "            self.subcommands[name] = SubCommand(self, name=name, command=[name], env=None)\n"
        "        for command in self.subcommands.values():\n"
        "            command.start()\n"
        "        try:\n"
        "            while True:\n"
        "                self.update_output()\n"
        "                time.sleep(0.1)\n"
        "        except KeyboardInterrupt:\n"
        "            pass\n"
        "        self.print_output('standalone', 'Shutting down components')\n"
        "        for command in self.subcommands.values():\n"
        "            command.stop()\n"
        "        for command in self.subcommands.values():\n"
        "            command.join()\n"
        "        self.print_output('standalone', 'Complete')\n"
        "    def update_output(self):\n"
        "        while self.output_queue:\n"
        "            name, line = self.output_queue.popleft()\n"
        "            self.print_output(name, line.decode().strip())\n"
        "    def print_output(self, name, output):\n"
        "        for line in output.splitlines():\n"
        "            print(f'{name:10} | {line}', flush=True)\n"
        "class SubCommand(threading.Thread):\n"
        "    def __init__(self, parent, name, command, env):\n"
        "        super().__init__()\n"
        "        self.parent, self.name, self.command, self.env = parent, name, command, env\n"
        "    def run(self):\n"
        "        self.process = subprocess.Popen(['airflow', *self.command], stdout=subprocess.PIPE,\n"
        "                                        stderr=subprocess.STDOUT, env=self.env)\n"
        "        for line in self.process.stdout:\n"
        "            self.parent.output_queue.append((self.name, line))\n"
        "    def stop(self):\n"
        "        self.process.terminate()\n",
    "airflow/executors/__init__.py": "",
    "airflow/executors/executor_constants.py": "LOCAL_EXECUTOR = 'LocalExecutor'\n",
    "airflow/executors/executor_loader.py":
        "class ExecutorLoader:\n"
        "    @staticmethod\n"
        "    def import_default_executor_cls():\n"
        "        return type('E', (), {'is_local': True}), None\n",
    "airflow/utils/__init__.py": "",
    "airflow/utils/db.py": "def initdb():\n    pass\n",
    "psycopg2/__init__.py":
        "class OperationalError(Exception):\n"
        "    pass\n"
        "def connect(*a, **k):\n"
        "    raise OperationalError('no server here')\n",
    "psycopg2/extras.py": "Json = object\n",
}

# Components as LocalExecutor runs them: the scheduler's two idle workers ignore INT and TERM and leave only on a
# stop message each, which the scheduler sends a second after its first signal unless another signal came first;
# then it waits on them for ever, and a further signal ends it with the workers still running.
FAKE_AIRFLOW = """#!@PYTHON@
import os, signal, sys, time
name = sys.argv[1]

def note(*words):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(" ".join(str(w) for w in words) + "\\n")

signals = []

def on_signal(signum, frame):
    note("signal", name, signum)
    signals.append(signum)

signal.signal(signal.SIGINT, on_signal)
signal.signal(signal.SIGTERM, on_signal)
if name == "scheduler":
    r, w = os.pipe()
    for _ in range(2):
        if os.fork() == 0:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            note("worker", name, os.getpid())
            os.read(r, 1)
            os._exit(0)
note("start", name, os.getpid())
print(name, "running", flush=True)
while not signals:
    time.sleep(0.05)
if name != "scheduler":
    sys.exit(0)
time.sleep(1)
if len(signals) == 1:
    os.write(w, b"xx")
    while True:
        try:
            os.wait()
        except ChildProcessError:
            sys.exit(0)
note("stuck", name)
seen = len(signals)
while len(signals) == seen:
    time.sleep(0.05)
note("left", name)
"""


def _launch(tmp_path, wrapper=(), **env):
    repo = fake_postgres.checkout(tmp_path / "checkout", "airflow-standalone-ldap.py")
    stubs = tmp_path / "stubs"
    for rel, text in STUBS.items():
        (stubs / rel).parent.mkdir(parents=True, exist_ok=True)
        (stubs / rel).write_text(text)
    fake_postgres.executable(tmp_path / "bin" / "airflow", FAKE_AIRFLOW.replace("@PYTHON@", sys.executable))
    home = tmp_path / "home"
    home.mkdir()
    full_env = {"HOME": str(home), "USER": "alice", "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
                "SLEDGE_NO_PGADMIN": "1", "SLEDGE_PORT": str(fake_postgres.free_port()),
                "FAKE_LOG": str(tmp_path / "components.log"),
                "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": f"sqlite:///{tmp_path}/airflow.db",
                "AIRFLOW__API_AUTH__JWT_SECRET": "test-jwt", "AIRFLOW__API__SECRET_KEY": "test-key",
                "PYTHONPATH": os.pathsep.join((str(stubs), str(fake_postgres.REPO))), **env}
    out = open(tmp_path / "launcher.out", "w")
    return subprocess.Popen([*wrapper, sys.executable, str(repo / "scripts" / "airflow-standalone-ldap.py")],
                            stdout=out, stderr=subprocess.STDOUT, cwd=str(home), env=full_env,
                            start_new_session=True)


class Stack:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.proc = None

    def start(self, **kwargs):
        self.proc = _launch(self.tmp_path, **kwargs)
        deadline = time.time() + 60
        while len(self.lines("start")) < 4 or len(self.lines("worker")) < 2:
            assert self.proc.poll() is None and time.time() < deadline, self.output()
            time.sleep(0.1)
        time.sleep(0.5)

    def ctrl_c(self, times=1):
        for _ in range(times):
            os.killpg(self.proc.pid, signal.SIGINT)
            time.sleep(0.1)

    def lines(self, kind):
        log = self.tmp_path / "components.log"
        if not log.exists():
            return []
        return [line.split() for line in log.read_text().splitlines() if line.startswith(kind + " ")]

    def signals(self, name):
        return [int(parts[2]) for parts in self.lines("signal") if parts[1] == name]

    def pids(self):
        return [int(parts[2]) for kind in ("start", "worker") for parts in self.lines(kind)]

    def output(self):
        return (self.tmp_path / "launcher.out").read_text()

    def wait(self, timeout):
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            pass
        pytest.fail(f"the launcher was still running {timeout}s after Ctrl-C:\n{self.output()}")

    def assert_all_gone(self):
        for pid in self.pids():
            deadline = time.time() + 10
            while _exists(pid):
                assert time.time() < deadline, f"process {pid} is still running"
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
    for pid in stack.pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def test_one_ctrl_c_stops_the_full_stack_and_signals_the_scheduler_once(stack) -> None:
    stack.start(SLEDGE_FULL_STOP_TIMEOUT="20")
    started = time.time()
    stack.ctrl_c()
    assert stack.wait(15) == 0
    assert time.time() - started < 15
    out = stack.output()
    assert "Shutting down components" in out and "Complete" in out
    assert "killing it" not in out
    assert stack.signals("scheduler") == [signal.SIGINT]
    assert not stack.lines("stuck")
    stack.assert_all_gone()


def test_a_scheduler_left_waiting_on_its_idle_workers_is_killed_after_the_stop_timeout(stack) -> None:
    stack.start(SLEDGE_FULL_STOP_TIMEOUT="2")
    stack.ctrl_c(times=2)
    assert stack.wait(30) == 0
    out = stack.output()
    assert stack.lines("stuck")
    assert "scheduler did not stop within 2s; killing it" in out
    assert "dag-processor did not stop" not in out
    assert out.rstrip().endswith("Complete")
    stack.assert_all_gone()


def test_workers_a_stuck_scheduler_left_behind_are_killed_too(stack) -> None:
    stack.start(SLEDGE_FULL_STOP_TIMEOUT="4")
    stack.ctrl_c(times=2)
    deadline = time.time() + 3
    while not stack.lines("stuck"):
        assert time.time() < deadline, stack.output()
        time.sleep(0.05)
    stack.ctrl_c()
    assert stack.wait(30) == 0
    assert stack.lines("left")
    assert "scheduler did not stop within 4s; killing it" in stack.output()
    stack.assert_all_gone()


def test_the_stuck_scheduler_is_killed_when_the_launcher_does_not_lead_its_process_group(stack) -> None:
    stack.start(SLEDGE_FULL_STOP_TIMEOUT="2", wrapper=("bash", "-c", '"$@"; exit $?', "launch"))
    stack.ctrl_c(times=2)
    assert stack.wait(30) == 0
    assert stack.lines("stuck")
    assert "scheduler did not stop within 2s; killing it" in stack.output()
    stack.assert_all_gone()
