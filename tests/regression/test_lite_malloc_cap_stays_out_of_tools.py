import json
import os
import sys
import types

import pytest

from hammer.vlsi import CLIDriver, HammerDriver, HammerDriverOptions, pd_store
from hammer.vlsi.hammer_build_systems import build_airflow_dag


class _Fail(Exception):
    pass


def _airflow_stubs(monkeypatch, context: dict) -> None:
    def module(name: str, **attrs) -> None:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)

    module("pendulum", datetime=lambda *a, **k: None)
    module("airflow")
    module("airflow.decorators", task=lambda fn=None, **kw: fn if fn is not None else (lambda f: f),
           dag=lambda *a, **k: (lambda f: (lambda: None)))
    module("airflow.models", Param=lambda *a, **k: None)
    module("airflow.utils")
    module("airflow.utils.task_group", TaskGroup=object)
    module("airflow.utils.trigger_rule",
           TriggerRule=types.SimpleNamespace(ALL_DONE="all_done", NONE_FAILED="none_failed"))
    module("airflow.exceptions", AirflowFailException=_Fail, AirflowSkipException=_Fail)
    module("airflow.sdk", get_current_context=lambda: context)


@pytest.fixture
def tool_env(tmp_path, monkeypatch):
    """The environment a DAG task hands to hammer (and the EDA tools) for a syn run by the DAG's owner."""
    for key in ("HAMMER_AIRFLOW_DAG_ID", "HAMMER_AIRFLOW_RUN_ID", "HAMMER_AIRFLOW_TRIGGERING_USER",
                "HAMMER_AIRFLOW_DESIGN", "HAMMER_AIRFLOW_WORKSPACE", "HAMMER_AIRFLOW_WORKSPACE_NAME",
                "OBJ_DIR", "HAMMER_D_MK", "MALLOC_ARENA_MAX", "SLEDGE_SET_MALLOC_ARENA_MAX"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setattr(pd_store, "lookup_triggering_user", lambda *a: None)
    monkeypatch.setattr(pd_store, "_pg_settings", lambda: {"host": "db"})
    monkeypatch.setattr(pd_store, "psycopg2", None)
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("HAMMER_DAGS_FOLDER", str(tmp_path / "dags"))
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "airflow"))
    rtl = tmp_path / "top.v"
    rtl.write_text("module Top(input a, output b); assign b = a; endmodule\n")
    cfg = tmp_path / "a.yml"
    cfg.write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
        "vlsi.inputs.hierarchical.config_source": "none",
        "synthesis.inputs.top_module": "Top",
        "synthesis.inputs.input_files": [str(rtl)],
    }))
    driver = HammerDriver(HammerDriverOptions(environment_configs=[], project_configs=[str(cfg)],
                                              log_file=str(tmp_path / "log.txt"), obj_dir=str(tmp_path / "obj")))
    build_airflow_dag(driver, lambda e: None)
    dag_run = types.SimpleNamespace(conf={}, dag_id="sledgehammer_Top_alice", run_id="r1")
    context = {"dag_run": dag_run, "params": {}, "ti": types.SimpleNamespace(task_id="syn"),
               "task": types.SimpleNamespace(task_id="syn")}
    _airflow_stubs(monkeypatch, context)
    ns: dict = {}
    exec(compile((tmp_path / "obj" / "hammer_dag.py").read_text(), "hammer_dag.py", "exec"), ns)
    envs: list = []
    ns["subprocess"] = types.SimpleNamespace(
        run=lambda cmd, **kw: envs.append(kw["env"]) or types.SimpleNamespace(returncode=0), DEVNULL=-3)

    def run(**env) -> dict:
        for key, val in env.items():
            monkeypatch.setenv(key, val)
        ns["run_hammer_action"]("syn", ["-p", "x.json"])
        return envs[-1]

    return run


def test_the_cap_the_lite_launcher_set_does_not_reach_the_tools(tool_env) -> None:
    env = tool_env(MALLOC_ARENA_MAX="2", SLEDGE_SET_MALLOC_ARENA_MAX="1")
    assert "MALLOC_ARENA_MAX" not in env and "SLEDGE_SET_MALLOC_ARENA_MAX" not in env


def test_a_cap_the_user_set_still_reaches_the_tools(tool_env) -> None:
    assert tool_env(MALLOC_ARENA_MAX="4")["MALLOC_ARENA_MAX"] == "4"


def test_hammer_itself_drops_the_launcher_cap_for_dags_generated_before(monkeypatch) -> None:
    monkeypatch.setenv("MALLOC_ARENA_MAX", "2")
    monkeypatch.setenv("SLEDGE_SET_MALLOC_ARENA_MAX", "1")
    with pytest.raises(SystemExit):
        CLIDriver().main(["--help"])
    assert "MALLOC_ARENA_MAX" not in os.environ and "SLEDGE_SET_MALLOC_ARENA_MAX" not in os.environ


def test_hammer_keeps_a_cap_the_user_set(monkeypatch) -> None:
    monkeypatch.setenv("MALLOC_ARENA_MAX", "4")
    monkeypatch.setenv("SLEDGE_SET_MALLOC_ARENA_MAX", "")
    monkeypatch.delenv("SLEDGE_SET_MALLOC_ARENA_MAX")
    with pytest.raises(SystemExit):
        CLIDriver().main(["--help"])
    assert os.environ["MALLOC_ARENA_MAX"] == "4"
