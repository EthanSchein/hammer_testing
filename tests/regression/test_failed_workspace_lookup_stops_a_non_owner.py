import json
import sys
import types

import pytest

import hammer.shell.hammer_vlsi as hammer_vlsi
from hammer.vlsi import HammerDriver, HammerDriverOptions, pd_store
from hammer.vlsi.hammer_build_systems import build_airflow_dag


class _OperationalError(Exception):
    pass


def _no_password(monkeypatch) -> None:
    for key in ("HAMMER_PG_PASSWORD", "PGPASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PGPASSFILE", "/nonexistent/.pgpass")
    monkeypatch.setenv("SLEDGE_PG_DIR", "/nonexistent/postgres")
    monkeypatch.setattr(pd_store, "_parse_airflow_cfg_conn", lambda: {})
    monkeypatch.setattr(pd_store, "psycopg2", types.SimpleNamespace(connect=lambda **kw: pytest.fail("connected")))


def _no_psycopg2(monkeypatch) -> None:
    monkeypatch.setattr(pd_store, "_pg_settings", lambda: {"host": "db"})
    monkeypatch.setattr(pd_store, "psycopg2", None)


def _too_many_clients(monkeypatch) -> None:
    def connect(**kw):
        raise _OperationalError("FATAL:  sorry, too many clients already")

    monkeypatch.setattr(pd_store, "_pg_settings", lambda: {"host": "db"})
    monkeypatch.setattr(pd_store, "psycopg2", types.SimpleNamespace(connect=connect))


BROKEN_LOOKUPS = {
    "no_password": (_no_password, "DatabaseUnavailable: No Postgres password found"),
    "no_psycopg2": (_no_psycopg2, "DatabaseUnavailable: psycopg2 is not installed"),
    "too_many_clients": (_too_many_clients, "too many clients"),
}
broken_lookup = pytest.mark.parametrize("breaker", BROKEN_LOOKUPS.values(), ids=BROKEN_LOOKUPS.keys())


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # The resolver stamps these for the cache layer; setenv first so they are restored.
    for key in ("HAMMER_AIRFLOW_DAG_ID", "HAMMER_AIRFLOW_RUN_ID", "HAMMER_AIRFLOW_TRIGGERING_USER",
                "HAMMER_AIRFLOW_DESIGN", "HAMMER_AIRFLOW_WORKSPACE", "HAMMER_AIRFLOW_WORKSPACE_NAME",
                "OBJ_DIR", "HAMMER_D_MK"):
        monkeypatch.setenv(key, "")
    for key in ("HAMMER_NO_PER_USER_WORKSPACE", "HAMMER_WORKSPACE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(pd_store, "lookup_triggering_user", lambda *a: None)


def _context(user, conf=None) -> dict:
    dag_run = types.SimpleNamespace(conf=conf or {}, dag_id="sledgehammer_Top_alice", run_id="r1")
    if user:
        dag_run.triggering_user_name = user
    return {"dag_run": dag_run, "params": {},
            "ti": types.SimpleNamespace(task_id="syn"), "task": types.SimpleNamespace(task_id="syn")}


non_owner_conf = pytest.mark.parametrize("conf", [{}, {"workspace": "iter2"}],
                                         ids=["default_workspace", "named_workspace"])


@broken_lookup
@non_owner_conf
def test_the_resolver_refuses_a_non_owner_whose_lookup_fails(breaker, conf, monkeypatch, tmp_path) -> None:
    breaker[0](monkeypatch)
    ws = conf.get("workspace", "default")
    with pytest.raises(hammer_vlsi.WorkspaceNotRegistered,
                       match=f"could not look up workspace '{ws}' for user 'bob'") as err:
        hammer_vlsi._resolve_workspace_obj_dir(_context("bob", conf), "Top", default_obj_dir=str(tmp_path / "alice"),
                                               gen_user="alice", claim=False)
    assert breaker[1] in str(err.value)


def _unusable_root(monkeypatch, tmp_path) -> None:
    root = tmp_path / "bob"
    root.write_text("")
    monkeypatch.setattr(pd_store, "get_user_workspace", lambda *a, **k: str(root))


@non_owner_conf
def test_the_resolver_refuses_a_non_owner_whose_obj_dir_cannot_be_claimed(conf, monkeypatch, tmp_path) -> None:
    _unusable_root(monkeypatch, tmp_path)
    with pytest.raises(hammer_vlsi.WorkspaceNotRegistered, match="cannot claim .* for user 'bob'") as err:
        hammer_vlsi._resolve_workspace_obj_dir(_context("bob", conf), "Top", default_obj_dir=str(tmp_path / "alice"),
                                               gen_user="alice")
    assert isinstance(err.value.__cause__, OSError)
    assert type(err.value.__cause__).__name__ in str(err.value)


@broken_lookup
def test_the_owner_still_falls_back_when_the_lookup_fails(breaker, monkeypatch, tmp_path) -> None:
    breaker[0](monkeypatch)
    assert hammer_vlsi._resolve_workspace_obj_dir(
        _context("alice", {"workspace": "iter2"}), "Top", default_obj_dir=str(tmp_path / "alice"),
        gen_user="alice", claim=False) is None


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
def stage(tmp_path, monkeypatch):
    """run_hammer_action of a DAG that 'alice' generated, exec'd with Airflow stubbed and its commands recorded."""
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("HAMMER_DAGS_FOLDER", str(tmp_path / "dags"))
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "airflow"))
    rtl = tmp_path / "top.v"
    rtl.write_text("module Top(input a, output b); assign b = a; endmodule\n")
    cfg = tmp_path / "a.yml"
    cfg.write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
        "vlsi.core.par_tool": "hammer.par.nop",
        "vlsi.inputs.hierarchical.config_source": "none",
        "synthesis.inputs.top_module": "Top",
        "synthesis.inputs.input_files": [str(rtl)],
    }))
    driver = HammerDriver(HammerDriverOptions(environment_configs=[], project_configs=[str(cfg)],
                                              log_file=str(tmp_path / "log.txt"), obj_dir=str(tmp_path / "obj")))
    build_airflow_dag(driver, lambda e: None)
    context: dict = {}
    _airflow_stubs(monkeypatch, context)
    ns: dict = {}
    exec(compile((tmp_path / "obj" / "hammer_dag.py").read_text(), "hammer_dag.py", "exec"), ns)
    assert ns["GEN_USER"] == "alice"
    cmds: list = []
    ns["subprocess"] = types.SimpleNamespace(
        run=lambda cmd, **kw: cmds.append(cmd) or types.SimpleNamespace(returncode=0), DEVNULL=-3)

    def run(user, conf=None) -> list:
        context.clear()
        context.update(_context(user, conf))
        ns["run_hammer_action"]("syn", ["-p", "x.json"])
        return cmds[-1]

    run.cmds, run.obj_dir = cmds, ns["OBJ_DIR"]
    return run


@broken_lookup
@non_owner_conf
def test_the_dag_fails_a_non_owner_whose_lookup_fails(stage, breaker, conf, monkeypatch) -> None:
    breaker[0](monkeypatch)
    with pytest.raises(hammer_vlsi.WorkspaceNotRegistered):
        stage("bob", conf)
    assert stage.cmds == []


@non_owner_conf
def test_the_dag_fails_a_non_owner_whose_obj_dir_cannot_be_claimed(stage, conf, monkeypatch, tmp_path) -> None:
    _unusable_root(monkeypatch, tmp_path)
    with pytest.raises(hammer_vlsi.WorkspaceNotRegistered):
        stage("bob", conf)
    assert stage.cmds == []


@pytest.mark.parametrize("user, conf", [(None, {}), ("alice", {"workspace": "iter2"})],
                         ids=["owner_from_USER", "owner_named_workspace"])
def test_the_dag_still_runs_the_owner_in_its_obj_dir_when_the_db_is_down(stage, user, conf, monkeypatch) -> None:
    _no_psycopg2(monkeypatch)
    assert stage(user, conf)[-3:] == ["--obj_dir", stage.obj_dir, "syn"]
