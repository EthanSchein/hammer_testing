import os
import sys

import pytest

from hammer.logging import HammerVLSILogging

from tests.regression.test_conformal_eco_patches_and_verifies import formal  # noqa: F401 (fixture)
from tests.regression.test_eco_par_runs_after_eco import _innovus

# Stands in for Conformal in an eco_verify run: writes the two reports the dofile redirects, with the verdict,
# incomplete count and compare classes the test asks for, or exits without writing anything (a crash).
FAKE_LEC = """#!{python}
import os, re, sys
open("lec.args", "w").write(" ".join(sys.argv[1:]))
if os.environ.get("FAKE_CRASH"):
    sys.exit(3)
rows = eval(os.environ.get("FAKE_ROWS", "[('Equivalent', '3 2 5')]"))
for line in open(sys.argv[sys.argv.index("-dofile") + 1]).read().splitlines():
    m = re.match(r"redirect (\\S+) \\{{report_verification\\}}", line)
    if m:
        open(m.group(1), "w").write("2. Incomplete verification:    " + os.environ.get("FAKE_INCOMPLETE", "0") + "\\n"
                                    "6. Compare Results:            " + os.environ.get("FAKE_VERDICT", "PASS") + "\\n")
    m = re.match(r"redirect (\\S+) \\{{report_compare_data -summary\\}}", line)
    if m:
        body = "".join(f"{{name:<20}} {{counts}}\\n" for name, counts in rows)
        open(m.group(1), "w").write("=====\\nCompared points      PO     DFF       Total\\n-----\\n" + body + "=====\\n")
"""


@pytest.fixture
def verify(formal, tmp_path, monkeypatch):
    """The formal fixture's driver set up for an eco_verify run of the post-ECO netlist, with the fake above."""
    lec = tmp_path / "lec_verify"
    lec.write_text(FAKE_LEC.format(python=sys.executable))
    lec.chmod(0o755)
    db = formal.driver.database
    db.set_setting("formal.inputs.check", "eco_verify")
    db.set_setting("formal.conformal.conformal_lec_bin", str(lec))
    monkeypatch.setattr(HammerVLSILogging, "enable_buffering", True)
    HammerVLSILogging.add_callback(HammerVLSILogging.callback_buffering)
    return formal


class TestConformalEcoVerify:
    def test_the_post_eco_netlist_is_compared_flat_against_the_reference(self, verify) -> None:
        ok, _ = verify()
        assert ok
        commands = _commands_of(verify)
        lib = f"read_library {verify.tech_dir}/slow.lib {verify.tech_dir}/pads.lib -liberty -both -replace"
        expected = [lib,
                    f"read_design {verify.fixed} -golden -replace",   # the corrected reference
                    f"read_design {verify.impl} -revised -replace",   # the netlist par wrote
                    "set_root_module dummy -both", "set_system_mode lec", "add_compared_points -all", "compare",
                    "redirect eco_verify_verification.rpt {report_verification}",
                    "redirect eco_verify_compare_summary.rpt {report_compare_data -summary}"]
        assert [c for c in commands if c in expected] == expected
        assert (verify.run_dir / "lec.args").read_text().split()[0] == "-GXL"
        assert any("equivalent to the reference (Conformal PASS)" in line for line in HammerVLSILogging.get_buffer())

    @pytest.mark.parametrize("env, reason", [
        ({"FAKE_VERDICT": "FAIL:NONEQ", "FAKE_ROWS": "[('Equivalent', '4'), ('Non-equivalent', '1')]"},
         "the compare reported FAIL:NONEQ"),
        ({"FAKE_INCOMPLETE": "2"}, "the verification is incomplete"),
        ({"FAKE_ROWS": "[('Equivalent', '4'), ('Abort', '1 1')]"}, "1 Abort point(s)"),
        ({"FAKE_ROWS": "[('Equivalent', '4'), ('Not-compared', '2')]"}, "2 Not-compared point(s)"),
        ({"FAKE_ROWS": "[]"}, "no points were compared equivalent"),
        ({"FAKE_CRASH": "1"}, "the compare reported no result"),
    ])
    def test_anything_but_a_complete_pass_fails(self, verify, monkeypatch, env, reason) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        ok, _ = verify()
        assert not ok
        assert any(reason in line and "not proven equivalent" in line for line in HammerVLSILogging.get_buffer())

    def test_a_pass_left_by_an_earlier_run_does_not_count(self, verify, monkeypatch) -> None:
        assert verify()[0]
        monkeypatch.setenv("FAKE_CRASH", "1")
        assert not verify()[0]


def _commands_of(verify) -> list:
    return [line for line in (verify.run_dir / "eco_verify.tcl").read_text().splitlines()
            if line and not line.startswith("#")]


class TestInnovusEcoCheck:
    def test_eco_check_runs_after_the_optimization(self, tmp_path) -> None:
        names = [s.name for s in _innovus(tmp_path, eco=True).steps]
        assert names.index("eco_opt_design") < names.index("eco_check") < names.index("write_design")

    def test_the_patch_cells_nets_are_checked_and_drc_reported(self, tmp_path) -> None:
        tool = _innovus(tmp_path, eco=True)
        for step in (tool.eco_restore_design, tool.eco_apply_patch, tool.eco_check):
            assert step()
        out = "\n".join(tool.output)
        assert out.index("check_drc -out_file eco_drc_before.rpt") < out.index("eco_new_insts.txt w")
        assert "check_connectivity -nets $_eco_nets -type regular -out_file eco_connectivity.rpt" in out
        assert "check_drc -out_file eco_drc_after.rpt" in out

    def _finished(self, tmp_path, report: str = None, report_age: int = 200):
        """An eco_par run whose Innovus finished at t=200 after its script was written at t=100."""
        tool = _innovus(tmp_path, eco=True)
        assert tool.write_design()
        os.makedirs(tool.run_dir, exist_ok=True)
        script = os.path.join(tool.run_dir, "par.tcl")
        open(script, "w").close()
        os.utime(script, (100, 100))
        open(tool.output_sim_netlist_filename, "w").close()
        os.utime(tool.output_sim_netlist_filename, (200, 200))
        if report is not None:
            path = os.path.join(tool.run_dir, "eco_connectivity.rpt")
            with open(path, "w") as f:
                f.write(report)
            os.utime(path, (report_age, report_age))
        return tool

    def test_an_unrouted_eco_net_fails(self, tmp_path) -> None:
        tool = self._finished(tmp_path, "Net cpu/n_24: no routing\nBegin Summary\n    1 Problem(s)\nEnd Summary\n")
        assert not tool.fill_outputs()

    def test_no_connectivity_report_fails(self, tmp_path) -> None:
        assert not self._finished(tmp_path).fill_outputs()

    def test_a_report_from_an_earlier_run_fails(self, tmp_path) -> None:
        assert not self._finished(tmp_path, "Found no problems or warnings.\n", report_age=50).fill_outputs()

    def test_a_clean_report_passes_the_check(self, tmp_path) -> None:
        tool = self._finished(tmp_path, "Begin Summary\n    Found no problems or warnings.\nEnd Summary\n")
        # past the ECO checks, fill_outputs goes on to par's usual output checks
        with pytest.raises(ValueError, match="Output GDS"):
            tool.fill_outputs()
