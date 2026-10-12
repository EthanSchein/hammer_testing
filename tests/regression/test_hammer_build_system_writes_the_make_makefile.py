import json
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver
from hammer.vlsi.hammer_build_systems import BuildSystems, build_makefile


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No SledgeHammer switch or Airflow run from the caller's shell; tests/conftest.py keeps the memos off."""
    for name in ("HAMMER_PD_CACHE", "HAMMER_PD_CACHE_LEDGER", "HAMMER_SUBSTEP_RESUME", "HAMMER_DB_CHECKPOINTS",
                 "HAMMER_CHECKPOINT_STREAM", "HAMMER_ERROR_SCAN", "HAMMER_AIRFLOW_DESIGN", "HAMMER_AIRFLOW_RUN_ID",
                 "AIRFLOW_CTX_DAG_RUN_ID"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def _build(tmp_path: Path, build_system: str) -> int:
    rtl = tmp_path / "top.v"
    rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
    (tmp_path / "config.json").write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.build_system": build_system,
        "vlsi.inputs.hierarchical.config_source": "none",
        "synthesis.inputs.top_module": "dummy",
        "synthesis.inputs.input_files": [str(rtl)],
    }, cls=HammerJSONEncoder, indent=4))
    with pytest.raises(SystemExit) as cm:
        CLIDriver().main(args=["build", "-p", str(tmp_path / "config.json"), "-o", str(tmp_path / "out.json"),
                               "--obj_dir", str(tmp_path / "obj"), "--log", str(tmp_path / "log.txt")])
    return cm.value.code


def test_hammer_is_the_makefile_build_system() -> None:
    assert BuildSystems["hammer"] is build_makefile
    assert BuildSystems["make"] is build_makefile


def test_hammer_build_writes_the_same_hammer_d_as_make(tmp_path) -> None:
    assert _build(tmp_path, "make") == 0
    make_d = (tmp_path / "obj" / "hammer.d").read_text()
    (tmp_path / "obj" / "hammer.d").unlink()

    assert _build(tmp_path, "hammer") == 0
    assert (tmp_path / "obj" / "hammer.d").read_text() == make_d
    assert "redo-syn:" in make_d
    assert not (tmp_path / "obj" / "hammer_dag.py").exists()
