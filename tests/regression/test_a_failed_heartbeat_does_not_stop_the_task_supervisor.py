import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Runs in its own interpreter: importing Airflow configures logging the way the scheduler's task supervisors log.
PROBE = """
import sys
try:
    import httpx
    from airflow.sdk.api.client import ServerResponseError
    from airflow.sdk.execution_time.supervisor import ActivitySubprocess
except ImportError:
    sys.exit(77)


class Probe(ActivitySubprocess):
    def kill(self, *args, **kwargs):
        raise AssertionError("the task was killed after one failed heartbeat")


request = httpx.Request("PUT", "http://localhost/execution/task-instances/0/heartbeat")
response = httpx.Response(500, request=request, json={"detail": "the database is unreachable"})
probe = Probe.__new__(Probe)
probe.id = "0"
probe.failed_heartbeats = 0
probe._handle_heartbeat_failures(ServerResponseError("Server error", request=request, response=response))
assert probe.failed_heartbeats == 1
"""


@pytest.mark.skipif(sys.platform == "win32", reason="Airflow's task supervisor is POSIX only")
def test_a_failed_heartbeat_is_logged_and_retried(tmp_path) -> None:
    spec = importlib.util.find_spec("airflow")
    if spec is None or spec.origin is None:
        pytest.skip("Airflow is not installed")
    shutil.copy(REPO / "airflow.cfg", tmp_path / "airflow.cfg")
    env = dict(os.environ, AIRFLOW_HOME=str(tmp_path),
               AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=f"sqlite:///{tmp_path / 'airflow.db'}")
    res = subprocess.run([sys.executable, "-c", PROBE], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=120)
    if res.returncode == 77:
        pytest.skip("this Airflow has no task SDK supervisor")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Failed to send heartbeat" in res.stdout + res.stderr
