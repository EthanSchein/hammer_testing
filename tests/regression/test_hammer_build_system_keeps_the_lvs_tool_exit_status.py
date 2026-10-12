import json
import os
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver, HammerTool, rtl_check


def _fixed_digest(paths, include_dirs=(), defines=(), top_module=None):
    return "0" * 64, []


def _incorrect_report(x: HammerTool) -> bool:
    """A Calibre-style lvs_results.rpt whose banner says the layout and schematic do not match."""
    with open(os.path.join(x.run_dir, "lvs_results.rpt"), "w") as f:
        f.write("OVERALL COMPARISON RESULTS\n\n   #     ###################       _   _\n"
                "         #                       #     INCORRECT     #\n")
    return True


class IncorrectLVSDriver(CLIDriver):
    def get_extra_lvs_hooks(self):
        return [HammerTool.make_post_insertion_hook("get_ilms", _incorrect_report)]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No SledgeHammer switch or Airflow run from the caller's shell; tests/conftest.py keeps the memos off."""
    for name in ("HAMMER_PD_CACHE", "HAMMER_PD_CACHE_LEDGER", "HAMMER_SUBSTEP_RESUME", "HAMMER_DB_CHECKPOINTS",
                 "HAMMER_CHECKPOINT_STREAM", "HAMMER_ERROR_SCAN", "HAMMER_AIRFLOW_DESIGN", "HAMMER_AIRFLOW_RUN_ID",
                 "AIRFLOW_CTX_DAG_RUN_ID"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setattr(rtl_check, "digest_units", _fixed_digest)
    monkeypatch.setattr(rtl_check, "digest_files", _fixed_digest)
    # the make contrast below must not reach for Postgres checkpoints
    monkeypatch.setenv("HAMMER_DB_CHECKPOINTS", "0")


def _lvs(tmp_path: Path, build_system: str, *flags: str) -> int:
    rtl = tmp_path / "top.v"
    rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
    (tmp_path / "mock").mkdir()
    (tmp_path / "config.json").write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.build_system": build_system,
        "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
        "vlsi.core.par_tool": "hammer.par.nop",
        "vlsi.core.lvs_tool": "hammer.lvs.mocklvs",
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.technology.extra_macro_sizes": [],
        "synthesis.inputs.top_module": "dummy",
        "synthesis.inputs.input_files": [str(rtl)],
        "synthesis.mocksynth.temp_folder": str(tmp_path / "mock"),
    }, cls=HammerJSONEncoder, indent=4))
    obj = tmp_path / "obj"

    def run(driver, action: str, config: Path, *extra: str) -> int:
        with pytest.raises(SystemExit) as cm:
            driver().main(args=[action, *extra, "-p", str(config), "-o", str(tmp_path / f"{action}-out.json"),
                                "--obj_dir", str(obj), "--log", str(tmp_path / "log.txt")])
        return cm.value.code

    assert run(CLIDriver, "syn", tmp_path / "config.json") == 0
    assert run(CLIDriver, "syn-to-par", obj / "syn-rundir" / "syn-output-full.json") == 0
    assert run(CLIDriver, "par", tmp_path / "syn-to-par-out.json") == 0
    assert run(CLIDriver, "par-to-lvs", obj / "par-rundir" / "par-output-full.json") == 0
    return run(IncorrectLVSDriver, "lvs", tmp_path / "par-to-lvs-out.json", *flags)


def test_hammer_lvs_exits_0_and_writes_its_outputs_whatever_the_report_says(tmp_path) -> None:
    assert _lvs(tmp_path, "hammer") == 0
    for name in ("lvs-output.json", "lvs-output-full.json"):
        assert isinstance(json.loads((tmp_path / "obj" / "lvs-rundir" / name).read_text()), dict), name


def test_make_lvs_still_fails_on_an_incorrect_report(tmp_path) -> None:
    assert _lvs(tmp_path, "make", "--force") == 1
    assert not (tmp_path / "obj" / "lvs-rundir" / "lvs-output.json").exists()
