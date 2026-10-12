import json
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver, HammerDriver, legacy_hammer


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No SledgeHammer switch or Airflow run from the caller's shell; tests/conftest.py keeps the memos off."""
    for name in ("HAMMER_PD_CACHE", "HAMMER_PD_CACHE_LEDGER", "HAMMER_SUBSTEP_RESUME", "HAMMER_DB_CHECKPOINTS",
                 "HAMMER_CHECKPOINT_STREAM", "HAMMER_ERROR_SCAN", "HAMMER_AIRFLOW_DESIGN", "HAMMER_AIRFLOW_RUN_ID",
                 "AIRFLOW_CTX_DAG_RUN_ID"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture
def syn_runs(monkeypatch):
    runs = []
    real = HammerDriver.run_synthesis

    def spy(self, *args, **kwargs):
        runs.append(legacy_hammer.is_legacy_hammer(self))
        return real(self, *args, **kwargs)
    monkeypatch.setattr(HammerDriver, "run_synthesis", spy)
    return runs


@pytest.mark.parametrize("flags", [("--force",), ("--local",), ("--force", "--local")])
def test_hammer_syn_runs_the_tool_once_with(tmp_path: Path, syn_runs, flags) -> None:
    rtl = tmp_path / "top.v"
    rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
    (tmp_path / "mock").mkdir()
    (tmp_path / "config.json").write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.build_system": "hammer",
        "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.technology.extra_macro_sizes": [],
        "synthesis.inputs.top_module": "dummy",
        "synthesis.inputs.input_files": [str(rtl)],
        "synthesis.mocksynth.temp_folder": str(tmp_path / "mock"),
    }, cls=HammerJSONEncoder, indent=4))
    with pytest.raises(SystemExit) as cm:
        CLIDriver().main(args=["syn", *flags, "-p", str(tmp_path / "config.json"), "-o", str(tmp_path / "out.json"),
                               "--obj_dir", str(tmp_path / "obj"), "--log", str(tmp_path / "log.txt")])
    assert cm.value.code == 0
    assert syn_runs == [True]
    assert sorted(p.stem for p in (tmp_path / "mock").glob("step*.txt")) == ["step1", "step2", "step3", "step4"]
    assert (tmp_path / "obj" / "syn-rundir" / "syn-output-full.json").is_file()
    assert not (tmp_path / "obj" / "master_database.json").exists()
