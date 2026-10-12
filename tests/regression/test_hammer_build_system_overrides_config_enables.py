import json
import os
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver, HammerTool, legacy_hammer, pd_cache, pd_store, rtl_check

# The six config-level enables and the legacy values the spec asks for, written out here.
EXPECTED = {
    "vlsi.pd_cache.enabled": False,
    "vlsi.pd_cache.ledger_enabled": False,
    "vlsi.substep_resume.enabled": False,
    "vlsi.substep_resume.db_checkpoints": False,
    "vlsi.error_scan.enabled": False,
    "vlsi.error_scan.fail_on": [],
}

SEEN = {}


def _fixed_digest(paths, include_dirs=(), defines=(), top_module=None):
    return "0" * 64, []


def _log_an_error(x: HammerTool) -> bool:
    """Leave the kind of ERROR line in genus.log that vlsi.error_scan.fail_on turns into a failure."""
    with open(os.path.join(x.run_dir, "genus.log"), "w") as f:
        f.write("**ERROR: (TEST-1): a continuable error the tool exited 0 after\n")
    SEEN.update({key: x.get_setting(key) for key in EXPECTED})
    return True


class ErrorDriver(CLIDriver):
    def get_extra_synthesis_hooks(self):
        return [HammerTool.make_post_insertion_hook("step4", _log_an_error)]


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
    SEEN.clear()


@pytest.fixture
def cache_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(pd_store, "load_stage_blob", lambda key: calls.append("load"))
    monkeypatch.setattr(pd_store, "store_stage_blob", lambda *a, **k: calls.append("store"))
    monkeypatch.setattr(pd_cache, "_record_cache_event", lambda *a, **k: calls.append("ledger"))
    monkeypatch.setattr(pd_cache, "_stamp_project_from_config", lambda *a, **k: None)
    return calls


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
        "vlsi.pd_cache.enabled": True,
        "vlsi.pd_cache.ledger_enabled": True,
        "vlsi.substep_resume.enabled": True,
        "vlsi.substep_resume.db_checkpoints": True,
        "vlsi.error_scan.enabled": True,
        "vlsi.error_scan.fail_on": ["TEST-1"],
    }, cls=HammerJSONEncoder, indent=4))
    with pytest.raises(SystemExit) as cm:
        ErrorDriver().main(args=["syn", "-p", str(tmp_path / "config.json"), "-o", str(tmp_path / "out.json"),
                                 "--obj_dir", str(tmp_path / "obj"), "--log", str(tmp_path / "log.txt")])
    return cm.value.code


def test_hammer_overrides_the_config_enables(tmp_path, cache_calls) -> None:
    assert _syn(tmp_path, "hammer") == 0
    assert SEEN == EXPECTED
    assert cache_calls == []
    assert (tmp_path / "obj" / "syn-rundir" / "syn-output.json").is_file()


def test_make_honours_the_same_config_enables(tmp_path, cache_calls) -> None:
    assert _syn(tmp_path, "make") == 1
    assert SEEN == {key: True for key in EXPECTED} | {"vlsi.error_scan.fail_on": ["TEST-1"]}
    assert cache_calls[0] == "load"


def test_the_module_table_covers_every_enable() -> None:
    assert EXPECTED.items() <= legacy_hammer.LEGACY_SETTINGS.items()
