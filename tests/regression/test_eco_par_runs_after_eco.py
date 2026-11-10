import json
import os
import types

import pytest

from hammer.config import HammerJSONEncoder
from hammer.par.innovus import Innovus
from hammer.shell import sledgehammer_cli
from hammer.vlsi import HammerDriver, HammerDriverOptions
from hammer.vlsi.hammer_build_systems import build_makefile

from tests.regression.test_eco_stage_runs_after_par import (BASE, PAR_OUT, _SkipException, _driver, _run,
                                                            dag, trigger)  # noqa: F401 (fixtures)

ECO_OUT = {
    "formal.inputs.top_module": "dummy",
    "formal.outputs.eco_netlist": "/obj/eco-rundir/dummy.eco.v",
    "formal.outputs.eco_stylus_script": "/obj/eco-rundir/eco_stylus.tcl",
}
ECO_STEPS = ["eco_restore_design", "eco_apply_patch", "eco_place", "eco_route", "eco_opt_design",
             "write_regs", "write_design"]


class TestEcoToPar:
    def test_eco_outputs_become_the_eco_par_input(self) -> None:
        """The mapped patch is applied as netlist edits to the database of the par run it patched."""
        assert HammerDriver.eco_output_to_par_input(dict(PAR_OUT, **ECO_OUT)) == {
            "par.inputs.eco_script": "/obj/eco-rundir/eco_stylus.tcl",
            "par.inputs.input_files": ["/obj/eco-rundir/dummy.eco.v"],
            "par.inputs.top_module": "dummy",
            "vlsi.builtins.is_complete": False,
        }

    def test_without_the_eco_script_there_is_no_eco_par_input(self) -> None:
        eco_out = {k: v for k, v in ECO_OUT.items() if k != "formal.outputs.eco_stylus_script"}
        assert HammerDriver.eco_output_to_par_input(dict(PAR_OUT, **eco_out)) is None

    def test_the_eco_to_par_action_writes_a_full_par_config(self, tmp_path) -> None:
        eco_out = tmp_path / "formal-output-full.json"
        eco_out.write_text(json.dumps(dict(BASE, **PAR_OUT, **ECO_OUT)))
        assert _run(tmp_path, "eco-to-par", str(eco_out)) == 0
        out = json.loads((tmp_path / "eco-to-par-out.json").read_text())
        assert out["par.inputs.eco_script"] == "/obj/eco-rundir/eco_stylus.tcl"
        # the patched par run's outputs ride along: eco_par finds its database beside them
        assert out["par.outputs.output_sim_netlist"] == "/obj/par-rundir/dummy.sim.v"


def test_par_itself_never_sees_the_eco_script(tmp_path) -> None:
    """The key is not a default, so par's config, and with it par's dependency check, stays as it was."""
    assert not _driver(tmp_path, BASE).database.has_setting("par.inputs.eco_script")


def _innovus(tmp_path, eco: bool) -> Innovus:
    par_dir = tmp_path / "par-rundir"
    (par_dir / "top_FINAL").mkdir(parents=True)
    (tmp_path / "eco_stylus.tcl").write_text("create_inst -name ECO_inst_1 -base_cell INV\n")
    (tmp_path / "top.v").write_text("module top(input a, output b); assign b = a; endmodule\n")
    cfg = {
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.par_tool": "hammer.par.innovus",
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.technology.extra_macro_sizes": [],
        "par.inputs.input_files": [str(tmp_path / "top.v")],
        "par.inputs.top_module": "top",
        "par.innovus.version": "251",
        "par.outputs.output_netlist": str(par_dir / "top.lvs.v"),
    }
    if eco:
        cfg["par.inputs.eco_script"] = str(tmp_path / "eco_stylus.tcl")
    (tmp_path / "config.json").write_text(json.dumps(cfg, cls=HammerJSONEncoder))
    driver = HammerDriver(HammerDriverOptions(environment_configs=[], project_configs=[str(tmp_path / "config.json")],
                                              log_file=str(tmp_path / "log.txt"), obj_dir=str(tmp_path / "obj")))
    assert driver.load_par_tool(str(tmp_path / "eco-par-rundir"))
    assert isinstance(driver.par_tool, Innovus)
    return driver.par_tool


class TestInnovusEco:
    def test_without_an_eco_script_par_runs_its_full_flow(self, tmp_path) -> None:
        names = [s.name for s in _innovus(tmp_path, eco=False).steps]
        assert names[0] == "init_design" and "eco_place" not in names

    def test_eco_steps_replace_the_full_flow(self, tmp_path) -> None:
        assert [s.name for s in _innovus(tmp_path, eco=True).steps] == ECO_STEPS

    def test_eco_restores_patches_and_places_incrementally(self, tmp_path) -> None:
        tool = _innovus(tmp_path, eco=True)
        for step in (tool.eco_restore_design, tool.eco_apply_patch, tool.eco_place, tool.eco_route,
                     tool.eco_opt_design):
            assert step()
        out = "\n".join(tool.output)
        db = tmp_path / "par-rundir" / "top_FINAL"
        script = tmp_path / "eco_stylus.tcl"
        order = [f"read_db {db}", f"source {script}", "place_eco", "route_eco",
                 "opt_design -post_route -setup -hold -expanded_views -timing_debug_report"]
        assert [out.index(c) for c in order] == sorted(out.index(c) for c in order)
        # a failed restore or patch exits instead of leaving innovus at its prompt
        assert f"if {{[catch {{read_db {db}}} err]}}" in out and "exit 2" in out
        assert "init_design" not in out and "place_opt_design" not in out

    def test_eco_without_the_patched_database_stops(self, tmp_path) -> None:
        tool = _innovus(tmp_path, eco=True)
        (tmp_path / "par-rundir" / "top_FINAL").rmdir()
        assert not tool.eco_restore_design()

    def test_outputs_older_than_the_script_fail_the_eco_run(self, tmp_path) -> None:
        """Innovus's exit status is not checked, so an ECO run that stopped early must not pass on old outputs."""
        tool = _innovus(tmp_path, eco=True)
        assert tool.write_design()
        os.makedirs(tool.run_dir, exist_ok=True)
        netlist = tool.output_sim_netlist_filename
        open(netlist, "w").close()
        open(os.path.join(tool.run_dir, "par.tcl"), "w").close()
        os.utime(netlist, (0, 0))
        assert not tool.fill_outputs()


def test_make_runs_eco_par_after_eco_in_its_own_rundir(tmp_path) -> None:
    driver = _driver(tmp_path, BASE)
    build_makefile(driver, lambda e: None)
    text = (tmp_path / "obj" / "hammer.d").read_text()
    obj = os.path.realpath(tmp_path / "obj")
    eco_out = os.path.join(obj, "eco-rundir", "formal-output-full.json")
    eco_par_in = os.path.join(obj, "eco-par-input.json")
    eco_par_out = os.path.join(obj, "eco-par-rundir", "par-output-full.json")
    assert f"{eco_par_in}: {eco_out}\n" in text
    assert f"-p {eco_out} $(HAMMER_EXTRA_ARGS) -o {eco_par_in} --obj_dir {obj} eco-to-par\n" in text
    assert f"{eco_par_out}: {eco_par_in} $(HAMMER_ECO_PAR_DEPENDENCIES)\n" in text
    assert (f"-p {eco_par_in} $(HAMMER_EXTRA_ARGS) --par_rundir {os.path.join(obj, 'eco-par-rundir')} "
            f"--obj_dir {obj} eco-par\n") in text


class TestEcoParInTheDag:
    def test_the_dag_has_an_eco_par_stage_after_eco(self, dag) -> None:
        assert "'eco_par': Param(default=False, type='boolean', title='ECO Place and Route')" in dag.text
        assert "p_to_eco >> e_node >> e_to_par >> ep_node" in dag.text

    def test_eco_to_par_bridges_the_eco_output(self, dag) -> None:
        cmd = dag("eco_to_par", {"eco_par": True}, "/o/eco-rundir/formal-output-full.json", "/o/eco-par-input.json")
        assert cmd[-3:] == ["--obj_dir", dag.obj, "eco-to-par"]
        assert cmd[cmd.index("-p"):cmd.index("-p") + 4] == [
            "-p", "/o/eco-rundir/formal-output-full.json", "-o", "/o/eco-par-input.json"]

    def test_eco_par_runs_in_the_eco_par_rundir(self, dag) -> None:
        cmd = dag("eco_par", {"eco_par": True}, "/o/eco-par-input.json", "/o/eco-par-rundir")
        assert cmd[-3:] == ["--obj_dir", dag.obj, "eco-par"]
        assert cmd[cmd.index("-p"):cmd.index("-p") + 4] == [
            "-p", "/o/eco-par-input.json", "--par_rundir", "/o/eco-par-rundir"]

    def test_eco_par_and_its_bridge_skip_unless_asked(self, dag) -> None:
        with pytest.raises(_SkipException):
            dag("eco_par", {"eco": True}, "/o/eco-par-input.json", "/o/eco-par-rundir")
        with pytest.raises(_SkipException):
            dag("eco_to_par", {"eco": True}, "/o/eco-rundir/formal-output-full.json", "/o/eco-par-input.json")

    def test_asking_for_eco_par_pulls_eco_in(self, dag) -> None:
        eco_par = types.SimpleNamespace(task_id="module_dummy.eco_par", downstream_list=[])
        bridge = types.SimpleNamespace(task_id="module_dummy.eco_to_par", downstream_list=[eco_par])
        cmd = dag("eco", {"eco_par": True}, "", "/o/eco-input.json", "/o/eco-rundir", downstream=[bridge])
        assert cmd[-1] == "formal"


def test_sledgehammer_runs_eco_par_like_make_eco_par(trigger) -> None:
    assert sledgehammer_cli._parse_target("eco-par") == ("eco_par", None, False)
    assert sledgehammer_cli._parse_target("eco-to-par") is None
    assert trigger("'forceall': Param(...)\n'eco_par': Param(...)", "eco_par") == {"eco_par": True}


def test_a_dag_from_before_the_eco_par_stage_says_to_regenerate(trigger) -> None:
    with pytest.raises(SystemExit) as cm:
        trigger("'forceall': Param(...)\n'eco': Param(...)", "eco_par")
    assert "predates the eco_par stage" in str(cm.value.code)
