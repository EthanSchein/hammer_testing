import json
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
def syn_runs(monkeypatch):
    runs = []
    real = HammerDriver.run_synthesis

    def spy(self, *args, **kwargs):
        runs.append("syn")
        return real(self, *args, **kwargs)
    monkeypatch.setattr(HammerDriver, "run_synthesis", spy)
    return runs


def _syn(tmp_path: Path, build_system: str) -> int:
    rtl = tmp_path / "top.v"
    rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
    (tmp_path / "mock").mkdir()
    (tmp_path / "config.json").write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.build_system": build_system,
        "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.technology.extra_macro_sizes": [],
        "synthesis.inputs.top_module": "dummy",
        "synthesis.inputs.input_files": [str(rtl)],
        "synthesis.mocksynth.temp_folder": str(tmp_path / "mock"),
    }, cls=HammerJSONEncoder, indent=4))
    with pytest.raises(SystemExit) as cm:
        CLIDriver().main(args=["syn", "-p", str(tmp_path / "config.json"), "-o", str(tmp_path / "out.json"),
                               "--obj_dir", str(tmp_path / "obj"), "--log", str(tmp_path / "log.txt")])
    return cm.value.code


@pytest.mark.parametrize("variable", ["HAMMER_AIRFLOW_RUN_ID", "AIRFLOW_CTX_DAG_RUN_ID"])
def test_a_dag_task_in_hammer_mode_fails_and_says_to_use_make(tmp_path, monkeypatch, capsys, syn_runs,
                                                              variable) -> None:
    monkeypatch.setenv(variable, "manual__2026-10-12")
    assert _syn(tmp_path, "hammer") == 1
    err = capsys.readouterr().err
    assert "vlsi.core.build_system is hammer" in err
    assert "manual__2026-10-12" in err and "make" in err
    assert syn_runs == []


def test_a_dag_task_in_sledgehammer_mode_still_runs(tmp_path, monkeypatch, syn_runs) -> None:
    monkeypatch.setenv("HAMMER_AIRFLOW_RUN_ID", "manual__2026-10-12")
    assert _syn(tmp_path, "sledgehammer") == 0
    assert syn_runs == ["syn"]
