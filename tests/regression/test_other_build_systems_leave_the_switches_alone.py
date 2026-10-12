import json
import os

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No SledgeHammer switch or Airflow run from the caller's shell; tests/conftest.py keeps the memos off."""
    for name in ("HAMMER_PD_CACHE", "HAMMER_PD_CACHE_LEDGER", "HAMMER_SUBSTEP_RESUME", "HAMMER_DB_CHECKPOINTS",
                 "HAMMER_CHECKPOINT_STREAM", "HAMMER_ERROR_SCAN", "HAMMER_AIRFLOW_DESIGN", "HAMMER_AIRFLOW_RUN_ID",
                 "AIRFLOW_CTX_DAG_RUN_ID"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


@pytest.mark.parametrize("build_system", ["make", "legacy", "airflow", "sledgehammer", "none"])
def test_only_hammer_switches_the_process_to_legacy_hammer(tmp_path, monkeypatch, capsys, build_system) -> None:
    exported = {"HAMMER_PD_CACHE": "1", "HAMMER_SUBSTEP_RESUME": "1", "HAMMER_ERROR_SCAN": "1",
                "HAMMER_CHECKPOINT_STREAM": "1"}
    for name, value in exported.items():
        monkeypatch.setenv(name, value)
    (tmp_path / "config.json").write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.build_system": build_system,
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.pd_cache.enabled": True,
    }, cls=HammerJSONEncoder, indent=4))
    out = tmp_path / "dump.json"
    with pytest.raises(SystemExit) as cm:
        CLIDriver().main(args=["dump", "-p", str(tmp_path / "config.json"), "-o", str(out),
                               "--obj_dir", str(tmp_path / "obj"), "--log", str(tmp_path / "log.txt")])
    assert cm.value.code == 0
    assert {name: os.environ.get(name) for name in exported} == exported
    assert "legacy Hammer" not in capsys.readouterr().out
    assert json.loads(out.read_text())["vlsi.pd_cache.enabled"] is True
