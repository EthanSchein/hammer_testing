import json
import os
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver, HammerDriver, rtl_check


def _fixed_digest(paths, include_dirs=(), defines=(), top_module=None):
    return "0" * 64, []


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
    monkeypatch.setenv("HAMMER_DB_CHECKPOINTS", "0")


@pytest.fixture
def tool_runs(monkeypatch):
    runs = []
    for name in ("run_synthesis", "run_par"):
        real = getattr(HammerDriver, name)

        def spy(self, *args, _real=real, _name=name, **kwargs):
            runs.append(_name)
            return _real(self, *args, **kwargs)
        monkeypatch.setattr(HammerDriver, name, spy)
    return runs


HIER = {
    "vlsi.inputs.hierarchical.top_module": "dummy",
    "vlsi.inputs.hierarchical.flat": "hierarchical",
    "vlsi.inputs.hierarchical.config_source": "manual",
    "vlsi.inputs.hierarchical.manual_modules": [{"mod1": ["m1s1"], "dummy": ["mod1"]}],
    "vlsi.inputs.hierarchical.manual_placement_constraints": [],
    "vlsi.inputs.hierarchical.constraints": [],
}


class Flow:
    def __init__(self, tmp_path: Path, build_system: str, **extra) -> None:
        self.tmp = tmp_path
        self.obj = tmp_path / "obj"
        rtl = tmp_path / "top.v"
        rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
        (tmp_path / "mock").mkdir()
        config = {
            "vlsi.core.technology": "hammer.technology.nop",
            "vlsi.core.build_system": build_system,
            "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
            "vlsi.core.par_tool": "hammer.par.nop",
            "vlsi.inputs.hierarchical.config_source": "none",
            "vlsi.technology.extra_macro_sizes": [],
            "synthesis.inputs.top_module": "dummy",
            "synthesis.inputs.input_files": [str(rtl)],
            "synthesis.mocksynth.temp_folder": str(tmp_path / "mock"),
        }
        config.update(extra)
        (tmp_path / "config.json").write_text(json.dumps(config, cls=HammerJSONEncoder, indent=4))

    def run(self, action: str, *configs: str) -> None:
        args = [action]
        for c in configs or (str(self.tmp / "config.json"),):
            args += ["-p", c]
        with pytest.raises(SystemExit) as cm:
            CLIDriver().main(args=args + ["-o", str(self.tmp / f"{action}-out.json"), "--obj_dir", str(self.obj),
                                          "--log", str(self.tmp / "log.txt")])
        assert cm.value.code == 0, action

    def syn_and_par(self) -> None:
        self.run("syn")
        self.run("syn-to-par", str(self.obj / "syn-rundir" / "syn-output-full.json"))
        self.run("par", str(self.tmp / "syn-to-par-out.json"))


def test_hammer_reruns_an_unchanged_syn_and_par(tmp_path, tool_runs) -> None:
    f = Flow(tmp_path, "hammer")
    f.syn_and_par()
    assert tool_runs == ["run_synthesis", "run_par"]
    syn_out = f.obj / "syn-rundir" / "syn-output.json"
    os.utime(syn_out, (0, 0))

    f.syn_and_par()
    assert tool_runs == ["run_synthesis", "run_par"] * 2
    assert syn_out.stat().st_mtime > 0
    for stage in ("syn", "par"):
        for name in (f"{stage}-output.json", f"{stage}-output-full.json"):
            assert isinstance(json.loads((f.obj / f"{stage}-rundir" / name).read_text()), dict), name


def test_make_skips_the_same_unchanged_syn_and_par(tmp_path, tool_runs) -> None:
    f = Flow(tmp_path, "make")
    f.syn_and_par()
    f.syn_and_par()
    assert tool_runs == ["run_synthesis", "run_par"]


@pytest.mark.parametrize("build_system, runs", [("hammer", 2), ("make", 1)])
def test_an_unchanged_hierarchical_syn_reruns_only_in_hammer_mode(tmp_path, tool_runs, build_system, runs) -> None:
    f = Flow(tmp_path, build_system, **HIER)
    f.run("syn-m1s1")
    f.run("syn-m1s1")
    assert tool_runs == ["run_synthesis"] * runs
    module_config = json.loads((f.obj / "syn-m1s1" / "full_config.json").read_text())
    assert module_config["synthesis.inputs.top_module"] == "m1s1"
    assert (f.obj / "syn-m1s1" / "syn-output-full.json").is_file()
