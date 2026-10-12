import json
from pathlib import Path

import pytest

from hammer.config import HammerDatabase, HammerJSONEncoder
from hammer.drc.mockdrc import MockDRC
from hammer.lvs.mocklvs import MockLVS
from hammer.vlsi import CLIDriver
from hammer.vlsi import code_fingerprints, error_scan, fingerprints, pd_cache, pd_store, rtl_check
from hammer.vlsi import substep_resume, time_tracking

# Everything SledgeHammer adds around a stage. In hammer mode none of it may run.
FORBIDDEN = [
    (rtl_check, "digest_or_bytes"), (rtl_check, "digest_units"), (rtl_check, "digest_files"),
    (fingerprints, "stage_fingerprints"),
    (code_fingerprints, "framework_fingerprint"), (code_fingerprints, "tool_fingerprint"),
    (code_fingerprints, "hooks_fingerprint"), (code_fingerprints, "tech_fingerprint"),
    (pd_cache, "cache_or_run"), (pd_cache, "try_restore_from_cache"), (pd_cache, "_build_cache_key"),
    (pd_cache, "_run_with_checkpoint_stream"),
    (substep_resume, "plan_resume"), (substep_resume, "record_attempt"),
    (substep_resume, "ensure_step_checkpoint"), (substep_resume, "push_checkpoint_db"),
    (substep_resume, "clear_checkpoint_db"), (substep_resume, "clean_checkpoints"),
    (substep_resume, "write_marker"),
    (error_scan, "scan_and_report"), (error_scan, "scan_log"),
    (time_tracking, "record_event"), (time_tracking, "stage_module"),
    (pd_store, "_connect"), (pd_store, "store_par_input"), (pd_store, "store_artifact"),
    (pd_store, "store_master_database"), (pd_store, "record_cache_event"), (pd_store, "load_stage_blob"),
    (pd_store, "store_stage_blob"), (pd_store, "store_checkpoint"), (pd_store, "fetch_checkpoint"),
    (HammerDatabase, "stage_change_check"), (HammerDatabase, "commit_master_database"),
    (HammerDatabase, "revert_rerun"), (HammerDatabase, "_mirror_master_to_db"),
]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No SledgeHammer switch or Airflow run from the caller's shell; tests/conftest.py keeps the memos off."""
    for name in ("HAMMER_PD_CACHE", "HAMMER_PD_CACHE_LEDGER", "HAMMER_SUBSTEP_RESUME", "HAMMER_DB_CHECKPOINTS",
                 "HAMMER_CHECKPOINT_STREAM", "HAMMER_ERROR_SCAN", "HAMMER_AIRFLOW_DESIGN", "HAMMER_AIRFLOW_RUN_ID",
                 "AIRFLOW_CTX_DAG_RUN_ID"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.fixture
def calls(monkeypatch):
    seen = []
    for owner, name in FORBIDDEN:
        def forbidden(*args, _name=f"{getattr(owner, '__name__', owner)}.{name}", **kwargs):
            seen.append(_name)
            raise AssertionError(f"{_name} ran in hammer mode")
        monkeypatch.setattr(owner, name, forbidden)
    if pd_store.psycopg2 is not None:
        monkeypatch.setattr(pd_store.psycopg2, "connect", lambda *a, **k: seen.append("psycopg2.connect"))
    return seen


def _config(tmp_path: Path) -> None:
    rtl = tmp_path / "top.v"
    rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
    (tmp_path / "mock").mkdir()
    (tmp_path / "config.json").write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.build_system": "hammer",
        "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
        "vlsi.core.par_tool": "hammer.par.nop",
        "vlsi.core.drc_tool": "hammer.drc.mockdrc",
        "vlsi.core.lvs_tool": "hammer.lvs.mocklvs",
        "vlsi.core.sim_tool": "hammer.sim.mocksim",
        "vlsi.core.sram_generator_tool": "hammer.sram_generator.mocksram_generator",
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.technology.extra_macro_sizes": [],
        "synthesis.inputs.top_module": "dummy",
        "synthesis.inputs.input_files": [str(rtl)],
        "synthesis.mocksynth.temp_folder": str(tmp_path / "mock"),
        "sim.inputs.top_module": "dummy",
        "sim.inputs.input_files": [str(rtl)],
        "sim.inputs.level": "rtl",
        "sim.submit.command": "local",
        "sim.mocksim.temp_folder": str(tmp_path / "mock"),
        "sram_generator.inputs.top_module": "dummy",
        "sram_generator.inputs.layout_file": "/dev/null",
        "sram_generator.temp_folder": str(tmp_path / "mock"),
        "sram_generator.submit.command": "local",
        "vlsi.inputs.sram_parameters": [{"depth": 32, "width": 32, "name": "small", "family": "1r1w",
                                         "mask": False, "vt": "SVT", "mux": 1}],
        "vlsi.inputs.mmmc_corners": [{"name": "c1", "type": "setup", "voltage": "0.5V", "temp": "0C"}],
    }, cls=HammerJSONEncoder, indent=4))


def _run(tmp_path: Path, action: str, *configs: str) -> int:
    args = [action]
    for c in configs or (str(tmp_path / "config.json"),):
        args += ["-p", c]
    with pytest.raises(SystemExit) as cm:
        CLIDriver().main(args=args + ["-o", str(tmp_path / f"{action}-out.json"), "--obj_dir", str(tmp_path / "obj"),
                                   "--log", str(tmp_path / "log.txt")])
    return cm.value.code


def _flow_to_drc_and_lvs_inputs(tmp_path: Path) -> None:
    obj = tmp_path / "obj"
    assert _run(tmp_path, "syn") == 0
    assert _run(tmp_path, "syn-to-par", str(obj / "syn-rundir" / "syn-output-full.json")) == 0
    assert _run(tmp_path, "par", str(tmp_path / "syn-to-par-out.json")) == 0
    assert _run(tmp_path, "par-to-drc", str(obj / "par-rundir" / "par-output-full.json")) == 0
    assert _run(tmp_path, "par-to-lvs", str(obj / "par-rundir" / "par-output-full.json")) == 0


def test_every_stage_runs_without_sledgehammer_machinery(tmp_path, calls) -> None:
    _config(tmp_path)
    obj = tmp_path / "obj"
    for _ in range(2):
        _flow_to_drc_and_lvs_inputs(tmp_path)
        assert _run(tmp_path, "drc", str(tmp_path / "par-to-drc-out.json")) == 0
        assert _run(tmp_path, "lvs", str(tmp_path / "par-to-lvs-out.json")) == 0
        assert _run(tmp_path, "syn-par") == 0
        assert _run(tmp_path, "sim") == 0
        assert _run(tmp_path, "sram_generator") == 0
    assert calls == []
    for stage in ("syn", "par", "drc", "lvs", "sim"):
        assert (obj / f"{stage}-rundir" / f"{stage}-output-full.json").is_file(), stage
    assert (obj / "syn-rundir" / "par-input.json").is_file()
    assert (tmp_path / "sram_generator-out.json").is_file()


def test_failing_drc_and_lvs_leave_no_rerun_marks(tmp_path, monkeypatch, calls) -> None:
    _config(tmp_path)
    _flow_to_drc_and_lvs_inputs(tmp_path)
    monkeypatch.setattr(MockDRC, "fill_outputs", lambda self: False)
    monkeypatch.setattr(MockLVS, "fill_outputs", lambda self: False)
    assert _run(tmp_path, "drc", str(tmp_path / "par-to-drc-out.json")) == 1
    assert _run(tmp_path, "lvs", str(tmp_path / "par-to-lvs-out.json")) == 1
    assert calls == []
