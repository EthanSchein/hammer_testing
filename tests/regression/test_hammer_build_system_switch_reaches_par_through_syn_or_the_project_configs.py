import json
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver, HammerDriver, legacy_hammer, rtl_check


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
    # the make runs below must not reach for Postgres checkpoints
    monkeypatch.setenv("HAMMER_DB_CHECKPOINTS", "0")


@pytest.fixture
def tool_runs(monkeypatch):
    """(tool, ran in hammer mode) for each tool run."""
    runs = []
    for name in ("run_synthesis", "run_par"):
        real = getattr(HammerDriver, name)

        def spy(self, *args, _real=real, _name=name, **kwargs):
            runs.append((_name, legacy_hammer.is_legacy_hammer(self)))
            return _real(self, *args, **kwargs)
        monkeypatch.setattr(HammerDriver, name, spy)
    return runs


class Flow:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.obj = tmp_path / "obj"
        self.config = tmp_path / "project.json"
        self.par_input = tmp_path / "syn-to-par-out.json"
        (tmp_path / "top.v").write_text("module dummy(input a, output b); assign b = a; endmodule\n")
        (tmp_path / "mock").mkdir()

    def write_config(self, build_system: str) -> None:
        self.config.write_text(json.dumps({
            "vlsi.core.technology": "hammer.technology.nop",
            "vlsi.core.build_system": build_system,
            "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
            "vlsi.core.par_tool": "hammer.par.nop",
            "vlsi.inputs.hierarchical.config_source": "none",
            "vlsi.technology.extra_macro_sizes": [],
            "synthesis.inputs.top_module": "dummy",
            "synthesis.inputs.input_files": [str(self.tmp / "top.v")],
            "synthesis.mocksynth.temp_folder": str(self.tmp / "mock"),
        }, cls=HammerJSONEncoder, indent=4))

    def run(self, action: str, *configs: Path) -> None:
        args = [action]
        for c in configs:
            args += ["-p", str(c)]
        with pytest.raises(SystemExit) as cm:
            CLIDriver().main(args=args + ["-o", str(self.tmp / f"{action}-out.json"), "--obj_dir", str(self.obj),
                                          "--log", str(self.tmp / "log.txt")])
        assert cm.value.code == 0, action

    def syn_and_par(self) -> None:
        self.run("syn", self.config)
        self.run("syn-to-par", self.obj / "syn-rundir" / "syn-output-full.json")
        self.run("par", self.par_input)


def test_project_configs_passed_after_par_input_switch_par_at_once(tmp_path, tool_runs, capsys) -> None:
    f = Flow(tmp_path)
    f.write_config("make")
    f.syn_and_par()
    assert tool_runs == [("run_synthesis", False), ("run_par", False)]

    f.write_config("hammer")
    capsys.readouterr()
    f.run("par", f.par_input, f.config)  # the e2e Makefile passes the project ymls after par-input.json
    assert tool_runs[2:] == [("run_par", True)]
    assert "rerun from syn after switching it" in capsys.readouterr().out


def test_rerunning_from_syn_carries_the_switch_into_par_input(tmp_path, tool_runs) -> None:
    f = Flow(tmp_path)
    f.write_config("make")
    f.syn_and_par()

    f.write_config("hammer")
    f.syn_and_par()  # make redo-syn, then make par
    assert json.loads(f.par_input.read_text())["vlsi.core.build_system"] == "hammer"
    assert tool_runs[2:] == [("run_synthesis", True), ("run_par", True)]
