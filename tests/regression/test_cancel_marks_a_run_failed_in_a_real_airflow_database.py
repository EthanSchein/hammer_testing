import getpass
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import types

import pytest

from hammer.shell import sledgehammer_cancel, sledgehammer_cli

pytestmark = pytest.mark.skipif(importlib.util.find_spec("airflow") is None, reason="needs Airflow installed")

DAG = "sledgehammer_Top_u"

_PROBE_DAG = f'''
import pendulum
from airflow.sdk import dag, task

OBJ_DIR = "{{obj}}"


@dag(dag_id="{DAG}", schedule=None, catchup=False, start_date=pendulum.datetime(2026, 1, 1, tz="UTC"))
def probe():
    @task(task_id="module_Top.par")
    def par():
        pass

    @task(task_id="cache_summary_", trigger_rule="all_done")
    def cache_summary_():
        pass

    par() >> cache_summary_()


probe()
'''

_START_RUN = textwrap.dedent('''
    import sys
    from sqlalchemy import select
    from airflow.models.dagbag import DBDagBag
    from airflow.models.dagrun import DagRun
    from airflow.utils.session import create_session

    with create_session() as session:
        dr = session.scalar(select(DagRun).where(DagRun.dag_id == sys.argv[1], DagRun.run_id == sys.argv[2]))
        dr.dag = DBDagBag().get_dag_for_run(dr, session=session)
        dr.verify_integrity(session=session, dag_version_id=dr.created_dag_version_id)
        session.flush()
        for ti in dr.get_task_instances(session=session):
            if ti.task_id == "module_Top.par":
                ti.state, ti.hostname, ti.pid = "running", "elsewhere.example", 1
        dr.state = "running"
''')


@pytest.fixture
def airflow_db(tmp_path, monkeypatch):
    """A migrated sqlite Airflow database holding run cli_1 of the probe DAG, with module_Top.par running."""
    home, dags, obj = tmp_path / "home", tmp_path / "dags", tmp_path / "Top"
    for d in (home, dags, obj):
        d.mkdir()
    (dags / f"{DAG}.py").write_text(_PROBE_DAG.replace("{obj}", str(obj)))
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(sledgehammer_cancel.__file__))))
    for var in [v for v in os.environ if v.startswith("AIRFLOW")]:
        monkeypatch.delenv(var)
    monkeypatch.setenv("AIRFLOW_HOME", str(home))
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", f"sqlite:///{home / 'airflow.db'}")
    monkeypatch.setenv("AIRFLOW__CORE__DAGS_FOLDER", str(dags))
    monkeypatch.setenv("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        [repo] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]))
    for cmd in (["-m", "airflow", "db", "migrate"], ["-m", "airflow", "dags", "reserialize"],
                ["-m", "airflow", "dags", "trigger", DAG, "-r", "cli_1", "-c", json.dumps({"par": True})],
                ["-c", _START_RUN, DAG, "cli_1"]):
        r = subprocess.run([sys.executable, *cmd], capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    monkeypatch.setattr(getpass, "getuser", lambda: "u")
    monkeypatch.setattr(sledgehammer_cli, "_dags_folder", lambda: (str(dags), "test"))
    psutil = types.ModuleType("psutil")
    psutil.__dict__.update(Error=OSError, STATUS_ZOMBIE="zombie", process_iter=lambda: [],
                           Process=lambda pid=None: types.SimpleNamespace(parents=lambda: []))
    monkeypatch.setitem(sys.modules, "psutil", psutil)
    return obj


def _tasks(run):
    return {t[0]: t[1] for t in run["tasks"]}


def test_cancel_marks_the_run_failed_with_airflows_own_call(airflow_db, capsys) -> None:
    got = sledgehammer_cancel._db("read", "u", "", "cli_1")
    assert [(r["dag_id"], r["run_id"], r["state"]) for r in got["runs"]] == [(DAG, "cli_1", "running")]
    assert _tasks(got["runs"][0]) == {"module_Top.par": "running", "cache_summary_": None}
    assert got["host"]
    assert sledgehammer_cancel._db("read", "someone_else", "", "cli_1")["runs"] == []

    assert sledgehammer_cancel.main(["cli_1", "--yes"]) == 0
    assert "[sledgehammer] marked run cli_1 failed: Top.par failed; 1 pending task skipped\n" in \
        capsys.readouterr().out
    run = sledgehammer_cancel._db("read", "u", DAG, "cli_1")["runs"][0]
    assert run["state"] == "failed"
    assert _tasks(run) == {"module_Top.par": "failed", "cache_summary_": "skipped"}
    assert sledgehammer_cancel._db("fail", DAG, "cli_1") == {"was": "failed", "state": "failed", "changed": []}
