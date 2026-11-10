import getpass
import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.shell import sledgehammer_cli
from hammer.vlsi import CLIDriver, HammerDriver, HammerDriverOptions, fingerprints
from hammer.vlsi.hammer_build_systems import build_airflow_dag, build_makefile


class _FailException(Exception):
    pass


class _SkipException(Exception):
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
    module("airflow.exceptions", AirflowFailException=_FailException, AirflowSkipException=_SkipException)
    module("airflow.sdk", get_current_context=lambda: context)


BASE = {
    "vlsi.core.technology": "hammer.technology.nop",
    "vlsi.inputs.hierarchical.config_source": "none",
    "synthesis.inputs.top_module": "dummy",
}
PAR_OUT = {
    "par.inputs.top_module": "dummy",
    "par.inputs.input_files": ["/obj/syn-rundir/dummy.mapped.v"],
    "par.outputs.output_sim_netlist": "/obj/par-rundir/dummy.sim.v",
}
ECO_REFERENCE = {"formal.inputs.eco_reference_files": ["/fixed/syn-rundir/dummy.mapped.v"]}


def _driver(tmp_path: Path, cfg: dict) -> HammerDriver:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg, cls=HammerJSONEncoder))
    return HammerDriver(HammerDriverOptions(environment_configs=[], project_configs=[str(path)],
                                            log_file=str(tmp_path / "log.txt"), obj_dir=str(tmp_path / "obj")))


def _run(tmp_path: Path, action: str, *configs: str) -> int:
    args = [action]
    for c in configs:
        args += ["-p", c]
    with pytest.raises(SystemExit) as cm:
        CLIDriver().main(args=args + ["-o", str(tmp_path / f"{action}-out.json"), "--obj_dir", str(tmp_path / "obj"),
                                   "--log", str(tmp_path / "log.txt")])
    return cm.value.code


class TestParToEco:
    def test_par_outputs_and_the_reference_become_the_eco_input(self) -> None:
        """The post-par sim netlist is the design to patch; the corrected netlist is what it must match."""
        assert HammerDriver.par_output_to_eco_input(dict(PAR_OUT, **ECO_REFERENCE)) == {
            "formal.inputs.check": "eco",
            "formal.inputs.input_files": ["/obj/par-rundir/dummy.sim.v"],
            "formal.inputs.reference_files": ["/fixed/syn-rundir/dummy.mapped.v"],
            "formal.inputs.top_module": "dummy",
            "vlsi.builtins.is_complete": False,
        }

    def test_without_par_outputs_or_a_reference_there_is_no_eco_input(self) -> None:
        assert HammerDriver.par_output_to_eco_input(dict(ECO_REFERENCE)) is None
        assert HammerDriver.par_output_to_eco_input(dict(PAR_OUT)) is None
        assert HammerDriver.par_output_to_eco_input(dict(PAR_OUT, **{"formal.inputs.eco_reference_files": []})) is None

    def test_the_par_to_eco_action_writes_a_full_eco_config(self, tmp_path) -> None:
        par_out = tmp_path / "par-output-full.json"
        par_out.write_text(json.dumps(dict(BASE, **PAR_OUT)))
        eco_cfg = tmp_path / "eco.json"
        eco_cfg.write_text(json.dumps(ECO_REFERENCE))
        assert _run(tmp_path, "par-to-eco", str(par_out), str(eco_cfg)) == 0
        out = json.loads((tmp_path / "par-to-eco-out.json").read_text())
        assert out["formal.inputs.check"] == "eco"
        assert out["formal.inputs.input_files"] == ["/obj/par-rundir/dummy.sim.v"]
        assert out["formal.inputs.reference_files"] == ["/fixed/syn-rundir/dummy.mapped.v"]
        assert out["synthesis.inputs.top_module"] == "dummy"
        assert "vlsi.builtins.is_complete" not in out

    def test_par_to_eco_without_a_reference_fails_and_says_what_is_missing(self, tmp_path, capsys) -> None:
        par_out = tmp_path / "par-output-full.json"
        par_out.write_text(json.dumps(dict(BASE, **PAR_OUT)))
        assert _run(tmp_path, "par-to-eco", str(par_out)) == 1
        assert "formal.inputs.eco_reference_files" in capsys.readouterr().out


class TestEcoKeepsParCached:
    """Adding the ECO inputs after par must not make syn or par look changed, or the ECO would redo them."""

    def test_the_dependency_check_ignores_the_eco_reference(self, tmp_path) -> None:
        master = str(tmp_path / "master.json")
        before = _driver(tmp_path, BASE).database
        for stage in ("syn", "par"):
            assert before.stage_change_check(stage, master)
            before.commit_master_database()
        after = _driver(tmp_path, dict(BASE, **ECO_REFERENCE)).database
        assert not after.stage_change_check("syn", master)
        assert not after.stage_change_check("par", master)

    def test_the_par_fingerprint_ignores_the_eco_reference(self, tmp_path) -> None:
        def par_lines(cfg: dict) -> dict:
            driver = _driver(tmp_path, cfg)
            db = json.loads(driver.database.get_database_json())
            return fingerprints.config_lines(db, fingerprints.path_roots(driver), "par")

        assert par_lines(BASE) == par_lines(dict(BASE, **ECO_REFERENCE))


def test_make_runs_eco_as_formal_in_its_own_rundir(tmp_path) -> None:
    driver = _driver(tmp_path, BASE)
    build_makefile(driver, lambda e: None)
    text = (tmp_path / "obj" / "hammer.d").read_text()
    obj = os.path.realpath(tmp_path / "obj")
    par_out = os.path.join(obj, "par-rundir", "par-output-full.json")
    eco_in = os.path.join(obj, "eco-input.json")
    eco_out = os.path.join(obj, "eco-rundir", "formal-output-full.json")
    assert f"{eco_in}: {par_out}\n" in text
    assert f"-p {par_out} $(HAMMER_EXTRA_ARGS) -o {eco_in} --obj_dir {obj} par-to-eco\n" in text
    assert f"{eco_out}: {eco_in} $(HAMMER_ECO_DEPENDENCIES)\n" in text
    assert f"-p {eco_in} $(HAMMER_EXTRA_ARGS) --formal_rundir {os.path.join(obj, 'eco-rundir')} --obj_dir {obj} formal\n" in text


@pytest.fixture
def dag(tmp_path, monkeypatch):
    """A generated flat DAG, exec'd with Airflow stubbed; call(fn, conf, *args) runs one task and returns its command."""
    monkeypatch.setenv("HAMMER_DAGS_FOLDER", str(tmp_path / "dags"))
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "airflow"))
    monkeypatch.setattr(sys, "argv", [os.path.join(os.path.dirname(sys.executable), "hammer-vlsi")])
    driver = _driver(tmp_path, BASE)
    build_airflow_dag(driver, lambda e: None)
    text = (tmp_path / "obj" / "hammer_dag.py").read_text()

    import hammer.shell.hammer_vlsi as hammer_vlsi
    monkeypatch.setattr(hammer_vlsi, "_resolve_workspace_obj_dir", lambda *a, **k: None)
    cmds = []
    context: dict = {}
    _airflow_stubs(monkeypatch, context)
    ns: dict = {}
    exec(compile(text, "hammer_dag.py", "exec"), ns)
    ns["subprocess"] = types.SimpleNamespace(
        run=lambda cmd, **kw: cmds.append(cmd) or types.SimpleNamespace(returncode=0), DEVNULL=-3)

    def call(fn: str, conf: dict, *args, downstream=()):
        task_id = f"module_dummy.{fn}"
        context.clear()
        context.update(dag_run=types.SimpleNamespace(conf=conf, dag_id="d", run_id="r"),
                       ti=types.SimpleNamespace(task_id=task_id),
                       task=types.SimpleNamespace(task_id=task_id, downstream_list=list(downstream)), params={})
        cmds.clear()
        ns[fn](*args, **context)
        return cmds[0] if cmds else None

    call.text = text
    call.obj = str(tmp_path / "obj")
    return call


class TestEcoInTheDag:
    def test_the_dag_has_an_eco_stage_after_par(self, dag) -> None:
        assert "'eco': Param(default=False, type='boolean', title='Functional ECO')" in dag.text
        assert "p_node >> [p_to_sim, p_to_p, p_to_form, p_to_time, p_to_drc, p_to_lvs, p_to_eco]" in dag.text
        assert "p_to_eco >> e_node" in dag.text

    def test_par_to_eco_bridges_the_par_output(self, dag) -> None:
        cmd = dag("par_to_eco", {"eco": True, "redo": True}, "/o/par-output-full.json", "/o/eco-input.json")
        assert cmd[-3:] == ["--obj_dir", dag.obj, "par-to-eco"]
        assert cmd[cmd.index("-p"):cmd.index("-p") + 4] == ["-p", "/o/par-output-full.json", "-o", "/o/eco-input.json"]
        assert "--force" not in cmd

    def test_eco_runs_formal_in_the_eco_rundir(self, dag) -> None:
        cmd = dag("eco", {"eco": True}, "", "/o/eco-input.json", "/o/eco-rundir")
        assert cmd[-3:] == ["--obj_dir", dag.obj, "formal"]
        assert cmd[cmd.index("-p"):cmd.index("-p") + 4] == ["-p", "/o/eco-input.json", "--formal_rundir", "/o/eco-rundir"]
        assert "--force" in dag("eco", {"eco": True, "redo": True}, "", "/o/eco-input.json", "/o/eco-rundir")

    def test_eco_and_its_bridge_skip_unless_asked(self, dag) -> None:
        with pytest.raises(_SkipException):
            dag("eco", {"par": True}, "", "/o/eco-input.json", "/o/eco-rundir")
        with pytest.raises(_SkipException):
            dag("par_to_eco", {"lvs": True}, "/o/par-output-full.json", "/o/eco-input.json")

    def test_asking_for_eco_pulls_par_in(self, dag) -> None:
        eco = types.SimpleNamespace(task_id="module_dummy.eco", downstream_list=[])
        bridge = types.SimpleNamespace(task_id="module_dummy.par_to_eco", downstream_list=[eco])
        cmd = dag("par", {"eco": True}, "", "/o/par-input.json", downstream=[bridge])
        assert cmd[-1] == "par"


@pytest.mark.skipif(importlib.util.find_spec("airflow") is None, reason="needs Airflow installed")
def test_the_real_dag_puts_par_to_eco_between_par_and_eco(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HAMMER_DAGS_FOLDER", str(tmp_path / "dags"))
    driver = _driver(tmp_path, BASE)
    build_airflow_dag(driver, lambda e: None)
    probe = (
        "import json, runpy, sys\n"
        "dag = runpy.run_path(sys.argv[1])['dag_instance']\n"
        "task = dag.get_task\n"
        "print(json.dumps({'par': sorted(task('module_dummy.par').downstream_task_ids),\n"
        "                  'bridge': sorted(task('module_dummy.par_to_eco').upstream_task_ids),\n"
        "                  'eco_up': sorted(task('module_dummy.eco').upstream_task_ids),\n"
        "                  'eco_down': sorted(task('module_dummy.eco').downstream_task_ids)}))\n"
    )
    env = dict(os.environ, AIRFLOW_HOME=str(tmp_path / "airflow"), AIRFLOW__CORE__LOAD_EXAMPLES="False")
    res = subprocess.run([sys.executable, "-c", probe, str(tmp_path / "obj" / "hammer_dag.py")],
                         env=env, capture_output=True, text=True, timeout=600)
    assert res.returncode == 0, res.stdout + res.stderr
    edges = json.loads(res.stdout.strip().splitlines()[-1])
    assert "module_dummy.par_to_eco" in edges["par"]
    assert edges["bridge"] == ["module_dummy.par"]
    assert edges["eco_up"] == ["module_dummy.par_to_eco"]
    # eco feeds only its place-and-route bridge
    assert edges["eco_down"] == ["module_dummy.eco_to_par"]


@pytest.fixture
def trigger(tmp_path, monkeypatch):
    dags = tmp_path / "dags_cli"
    dags.mkdir()
    dag_file = dags / f"sledgehammer_Top_{getpass.getuser()}.py"
    calls = []
    monkeypatch.setattr(sledgehammer_cli, "_airflow", lambda *a, capture=True: calls.append(a) or (0, "", ""))
    monkeypatch.setattr(sledgehammer_cli, "_dags_folder", lambda: (str(dags), "test"))

    def run(dag_text: str, *argv: str) -> dict:
        dag_file.write_text(dag_text)
        calls.clear()
        assert sledgehammer_cli._cmd_run(list(argv) + ["--obj_dir", str(tmp_path / "Top"), "--no-wait"]) == 0
        sent = next(c for c in calls if c[:2] == ("dags", "trigger"))
        return json.loads(sent[sent.index("-c") + 1])
    return run


def test_sledgehammer_runs_eco_like_make_eco(trigger) -> None:
    assert sledgehammer_cli._parse_target("eco") == ("eco", None, False)
    assert sledgehammer_cli._parse_target("redo-eco-SubMod") == ("eco", "SubMod", True)
    assert sledgehammer_cli._parse_target("par-to-eco") is None
    assert trigger("'forceall': Param(...)\n'eco': Param(...)", "eco") == {"eco": True}


def test_a_dag_from_before_the_eco_stage_says_to_regenerate(trigger) -> None:
    with pytest.raises(SystemExit) as cm:
        trigger("'forceall': Param(...)", "eco")
    message = str(cm.value.code)
    assert "predates the eco stage" in message and "(sledgehammer buildfile)" in message
