"""eco_par post-route optimization modes, final verification and duplicate-checkpoint links."""
import json
import os

import pytest

from hammer.config import HammerJSONEncoder
from hammer.par.innovus import Innovus
from hammer.vlsi import HammerDriver, HammerDriverOptions


def _innovus(tmp_path, **settings) -> Innovus:
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
        "par.inputs.eco_script": str(tmp_path / "eco_stylus.tcl"),
    }
    cfg.update({f"par.innovus.{k}": v for k, v in settings.items()})
    (tmp_path / "config.json").write_text(json.dumps(cfg, cls=HammerJSONEncoder))
    driver = HammerDriver(HammerDriverOptions(environment_configs=[], project_configs=[str(tmp_path / "config.json")],
                                              log_file=str(tmp_path / "log.txt"), obj_dir=str(tmp_path / "obj")))
    assert driver.load_par_tool(str(tmp_path / "eco-par-rundir"))
    assert isinstance(driver.par_tool, Innovus)
    return driver.par_tool


def _out(tool) -> str:
    return "\n".join(tool.output)


class TestDefaultsKeepTheFlow:
    def test_the_default_is_the_whole_design_optimization(self, tmp_path) -> None:
        tool = _innovus(tmp_path)
        assert tool.eco_place() and tool.eco_opt_design()
        out = _out(tool)
        assert "opt_design -post_route -setup -hold -expanded_views -timing_debug_report" in out
        assert "place_eco\n" in out + "\n" and "-fix_placed_insts" not in out
        assert "::hammer_eco::targeted" not in out



class TestModes:
    def test_targeted_runs_the_targeted_procedure_with_its_settings(self, tmp_path) -> None:
        tool = _innovus(tmp_path, eco_opt_mode="targeted", eco_opt_max_drv_passes=3, eco_opt_slack_tolerance=0.02)
        assert tool.eco_opt_design()
        out = _out(tool)
        assert f"source {tool.eco_lib}" in out and "::hammer_eco::targeted 3 0.02" in out
        assert "opt_design -post_route -setup -hold" not in out

    def test_full_records_what_the_optimization_touched(self, tmp_path) -> None:
        tool = _innovus(tmp_path, eco_opt_mode="full")
        assert tool.eco_opt_design()
        out = _out(tool)
        order = ["set _eco_before [::hammer_eco::snapshot]", "opt_design -post_route -setup -hold",
                 "::hammer_eco::record full $_eco_before"]
        assert [out.index(c) for c in order] == sorted(out.index(c) for c in order)

    def test_none_optimizes_nothing(self, tmp_path) -> None:
        tool = _innovus(tmp_path, eco_opt_mode="none")
        assert tool.eco_opt_design()
        out = _out(tool)
        assert "opt_design" not in out and "::hammer_eco::record none" in out

    @pytest.mark.parametrize("settings", [{"eco_opt_mode": "fast"},
                                          {"eco_opt_mode": "targeted", "eco_opt_max_drv_passes": 0}])
    def test_a_bad_setting_stops_the_step(self, tmp_path, settings) -> None:
        assert not _innovus(tmp_path, **settings).eco_opt_design()

    def test_fix_existing_keeps_placed_cells_where_they_are(self, tmp_path) -> None:
        tool = _innovus(tmp_path, eco_place_fix_existing=True)
        assert tool.eco_place()
        assert "place_eco -fix_placed_insts true" in _out(tool)

    @pytest.mark.parametrize("mode", ["full", "targeted", "none"])
    def test_every_mode_takes_the_baseline_and_verifies(self, tmp_path, mode) -> None:
        tool = _innovus(tmp_path, eco_opt_mode=mode)
        assert tool.eco_restore_design() and tool.eco_check()
        out = _out(tool)
        assert "::hammer_eco::baseline" in out and "::hammer_eco::verify 0.01" in out
        assert os.path.isfile(tool.eco_lib)


class TestCheckpoints:
    def test_duplicates_are_links_and_the_rest_are_written(self, tmp_path) -> None:
        tool = _innovus(tmp_path)
        steps = {s.name: s for s in tool.steps}
        expect = {("eco_restore_design", "eco_apply_patch"): f"ln -sfn {tool.eco_base_db} pre_eco_apply_patch",
                  ("eco_check", "write_regs"): "ln -sfn pre_eco_check pre_write_regs",
                  ("write_regs", "write_design"): "ln -sfn pre_write_regs pre_write_design",
                  ("eco_route", "eco_opt_design"): "write_db pre_eco_opt_design",
                  ("eco_apply_patch", "eco_place"): "write_db pre_eco_place"}
        for (prev, nxt), line in expect.items():
            tool.do_between_steps(steps[prev], steps[nxt])
            assert line in _out(tool).splitlines()[-4:]
            assert _out(tool).splitlines()[-1] == f"ln -sfn pre_{nxt} latest"

    def test_a_step_in_between_gets_its_own_checkpoint(self, tmp_path) -> None:
        """Only the exact pairs are duplicates: a hook (here eco_opt_design) before write_regs may change the design."""
        tool = _innovus(tmp_path)
        steps = {s.name: s for s in tool.steps}
        tool.do_between_steps(steps["eco_opt_design"], steps["write_regs"])
        assert "write_db pre_write_regs" in _out(tool).splitlines()

    def test_par_without_an_eco_writes_every_checkpoint(self, tmp_path, monkeypatch) -> None:
        tool = _innovus(tmp_path)
        steps = {s.name: s for s in tool.steps}
        monkeypatch.setattr(Innovus, "eco_script", property(lambda self: None))
        tool.do_between_steps(steps["eco_check"], steps["write_regs"])
        assert "write_db pre_write_regs" in _out(tool).splitlines()


def _verification_files(tool, after_setup="0.1", new=""):
    os.makedirs(tool.run_dir, exist_ok=True)
    for name, text in (("eco_before_timing.txt", "setup 0.1\nhold 0.05\n"),
                       ("eco_after_timing.txt", f"setup {after_setup}\nhold 0.05\n"),
                       ("eco_new_violations.txt", new)):
        with open(os.path.join(tool.run_dir, name), "w") as f:
            f.write(text)


class TestVerification:
    def test_nothing_beyond_the_baseline_passes(self, tmp_path) -> None:
        tool = _innovus(tmp_path, eco_opt_mode="none")
        _verification_files(tool)
        ok, summary = tool.eco_verification()
        assert ok and "none optimization" in summary
        assert open(os.path.join(tool.run_dir, "eco_verification.rpt")).read().startswith("PASS")

    @pytest.mark.parametrize("after_setup,new", [("-0.02", ""), ("0.1", "maxtran a/ECO_inst_1/A\n")])
    def test_an_introduced_violation_fails(self, tmp_path, after_setup, new) -> None:
        tool = _innovus(tmp_path)
        _verification_files(tool, after_setup, new)
        ok, summary = tool.eco_verification()
        assert not ok
        assert ("setup WNS -0.02" in summary) if new == "" else ("1 max_transition" in summary)

    def test_a_baseline_that_already_failed_is_not_the_ecos(self, tmp_path) -> None:
        tool = _innovus(tmp_path)
        os.makedirs(tool.run_dir, exist_ok=True)
        for name, text in (("eco_before_timing.txt", "setup -0.2\nhold 0.05\n"),
                           ("eco_after_timing.txt", "setup -0.205\nhold 0.05\n"), ("eco_new_violations.txt", "")):
            with open(os.path.join(tool.run_dir, name), "w") as f:
                f.write(text)
        assert tool.eco_verification()[0]

    def test_missing_reports_fail(self, tmp_path) -> None:
        assert not _innovus(tmp_path).eco_verification()[0]
