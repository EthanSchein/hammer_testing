import json
import shutil
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.par.mockpar import MockPlaceAndRoute
from hammer.vlsi import CLIDriver, rtl_check

PAR_STEPS = []


def _fixed_digest(paths, include_dirs=(), defines=(), top_module=None):
    return "0" * 64, []


def _recording(name: str, real):
    def step(self) -> bool:
        PAR_STEPS.append(name)
        return real(self)
    step.__name__ = name
    return step


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
    # mockpar's steps leave no file in a flat run, so record each one that runs
    for name in ("power_straps", "get_ilms"):
        monkeypatch.setattr(MockPlaceAndRoute, name, _recording(name, getattr(MockPlaceAndRoute, name)))
    PAR_STEPS.clear()


class Flow:
    """mocksynth writes stepN.txt into its temp folder for each step that runs."""

    def __init__(self, tmp_path: Path, build_system: str) -> None:
        self.tmp = tmp_path
        self.mock = tmp_path / "mock"
        self.obj = tmp_path / "obj"
        rtl = tmp_path / "top.v"
        rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
        (tmp_path / "config.json").write_text(json.dumps({
            "vlsi.core.technology": "hammer.technology.nop",
            "vlsi.core.build_system": build_system,
            "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
            "vlsi.core.par_tool": "hammer.par.mockpar",
            "vlsi.inputs.hierarchical.config_source": "none",
            "vlsi.technology.extra_macro_sizes": [],
            "synthesis.inputs.top_module": "dummy",
            "synthesis.inputs.input_files": [str(rtl)],
            "synthesis.mocksynth.temp_folder": str(self.mock),
        }, cls=HammerJSONEncoder, indent=4))

    def run(self, action: str, config: Path, *flags: str) -> int:
        with pytest.raises(SystemExit) as cm:
            CLIDriver().main(args=[action, *flags, "-p", str(config), "-o", str(self.tmp / f"{action}-out.json"),
                                   "--obj_dir", str(self.obj), "--log", str(self.tmp / "log.txt")])
        return cm.value.code

    def syn(self, *flags: str):
        shutil.rmtree(self.mock, ignore_errors=True)
        self.mock.mkdir()
        output = self.obj / "syn-rundir" / "syn-output.json"
        if output.exists():
            output.unlink()
        code = self.run("syn", self.tmp / "config.json", *flags)
        return code, sorted(p.stem for p in self.mock.glob("step*.txt")), output.is_file()

    def par(self, *flags: str):
        PAR_STEPS.clear()
        output = self.obj / "par-rundir" / "par-output.json"
        if output.exists():
            output.unlink()
        code = self.run("par", self.tmp / "syn-to-par-out.json", *flags)
        return code, list(PAR_STEPS), output.is_file()


@pytest.mark.parametrize("flags, steps", [
    ((), ["step1", "step2", "step3", "step4"]),
    (("--to_step", "step2"), ["step1", "step2"]),
    (("--until_step", "step3"), ["step1", "step2"]),
    (("--from_step", "step3"), ["step3", "step4"]),
    (("--after_step", "step2"), ["step3", "step4"]),
    (("--only_step", "step2"), ["step2"]),
    (("--from_step", "step2", "--to_step", "step3"), ["step2", "step3"]),
])
def test_step_flags_run_the_steps_upstream_hammer_runs(tmp_path, flags, steps) -> None:
    f = Flow(tmp_path, "hammer")
    assert f.syn() == (0, ["step1", "step2", "step3", "step4"], True)
    assert f.syn(*flags) == (0, steps, True)


@pytest.mark.parametrize("flags, steps", [
    ((), ["power_straps", "get_ilms"]),
    (("--to_step", "power_straps"), ["power_straps"]),
    (("--from_step", "get_ilms"), ["get_ilms"]),
    (("--after_step", "power_straps"), ["get_ilms"]),
    (("--only_step", "power_straps"), ["power_straps"]),
])
def test_par_step_flags_run_the_steps_upstream_hammer_runs(tmp_path, flags, steps) -> None:
    f = Flow(tmp_path, "hammer")
    assert f.syn()[0] == 0
    assert f.run("syn-to-par", f.obj / "syn-rundir" / "syn-output-full.json") == 0
    assert f.par() == (0, ["power_straps", "get_ilms"], True)
    assert f.par(*flags) == (0, steps, True)


def test_make_mode_still_wants_a_checkpoint_for_a_later_start_step(tmp_path) -> None:
    f = Flow(tmp_path, "make")
    assert f.syn()[0] == 0
    code, ran, _ = f.syn("--from_step", "step3")
    assert code == 1 and ran == []
