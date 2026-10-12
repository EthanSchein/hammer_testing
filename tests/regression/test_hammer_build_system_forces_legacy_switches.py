import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver, HammerTool, legacy_hammer

# What the spec asks for, written out here rather than read from legacy_hammer.
EXPECTED = {
    "HAMMER_PD_CACHE": "0",
    "HAMMER_PD_CACHE_LEDGER": "0",
    "HAMMER_SUBSTEP_RESUME": "0",
    "HAMMER_DB_CHECKPOINTS": "0",
    "HAMMER_CHECKPOINT_STREAM": "0",
    "HAMMER_ERROR_SCAN": "0",
    "HAMMER_RTL_FP_CACHE": "off",
    "HAMMER_CODE_FP_CACHE": "off",
}

SEEN = {}


def _child_env(x: HammerTool) -> bool:
    """A step hook that asks a child process what it sees, the way a tool or script would."""
    code = "import json, os, sys; json.dump({k: os.environ.get(k) for k in sys.argv[1:]}, sys.stdout)"
    out = subprocess.check_output([sys.executable, "-c", code, *EXPECTED])
    SEEN.update(json.loads(out))
    return True


class ProbeDriver(CLIDriver):
    def get_extra_synthesis_hooks(self):
        return [HammerTool.make_post_insertion_hook("step1", _child_env)]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No SledgeHammer switch or Airflow run from the caller's shell; tests/conftest.py keeps the memos off."""
    for name in ("HAMMER_PD_CACHE", "HAMMER_PD_CACHE_LEDGER", "HAMMER_SUBSTEP_RESUME", "HAMMER_DB_CHECKPOINTS",
                 "HAMMER_CHECKPOINT_STREAM", "HAMMER_ERROR_SCAN", "HAMMER_AIRFLOW_DESIGN", "HAMMER_AIRFLOW_RUN_ID",
                 "AIRFLOW_CTX_DAG_RUN_ID"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    SEEN.clear()


def _syn(tmp_path: Path, capsys) -> str:
    rtl = tmp_path / "top.v"
    rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
    (tmp_path / "mock").mkdir(exist_ok=True)
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
    capsys.readouterr()
    with pytest.raises(SystemExit) as cm:
        ProbeDriver().main(args=["syn", "-p", str(tmp_path / "config.json"), "-o", str(tmp_path / "out.json"),
                                 "--obj_dir", str(tmp_path / "obj"), "--log", str(tmp_path / "log.txt")])
    out = capsys.readouterr().out
    assert cm.value.code == 0, out
    return out


def test_exported_switches_are_forced_to_their_legacy_values(tmp_path, monkeypatch, capsys) -> None:
    for name in ("HAMMER_PD_CACHE", "HAMMER_PD_CACHE_LEDGER", "HAMMER_SUBSTEP_RESUME", "HAMMER_DB_CHECKPOINTS",
                 "HAMMER_CHECKPOINT_STREAM", "HAMMER_ERROR_SCAN"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("HAMMER_RTL_FP_CACHE", str(tmp_path / "rtl-memo"))
    monkeypatch.setenv("HAMMER_CODE_FP_CACHE", "on")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    out = _syn(tmp_path, capsys)

    assert SEEN == EXPECTED
    lines = out.splitlines()
    info = [line for line in lines if "running plain legacy Hammer. Forced" in line]
    assert len(info) == 1
    for name, value in EXPECTED.items():
        assert f"{name}={value}" in info[0]
    assert "vlsi.substep_resume.enabled=false" in info[0]
    warning = [line for line in lines if "exported SledgeHammer switches are ignored" in line]
    assert len(warning) == 1
    for name in ("HAMMER_PD_CACHE", "HAMMER_PD_CACHE_LEDGER", "HAMMER_SUBSTEP_RESUME", "HAMMER_DB_CHECKPOINTS",
                 "HAMMER_CHECKPOINT_STREAM", "HAMMER_ERROR_SCAN"):
        assert f"{name}=1" in warning[0]
    assert f"HAMMER_RTL_FP_CACHE={tmp_path / 'rtl-memo'}" in warning[0]
    assert "HAMMER_CODE_FP_CACHE=on" in warning[0]
    assert not (tmp_path / "rtl-memo").exists()


def test_switches_that_are_already_off_draw_no_warning(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HAMMER_PD_CACHE", "0")
    monkeypatch.setenv("HAMMER_ERROR_SCAN", "off")
    out = _syn(tmp_path, capsys)

    assert SEEN == EXPECTED
    assert len([line for line in out.splitlines() if "running plain legacy Hammer. Forced" in line]) == 1
    assert "exported SledgeHammer switches are ignored" not in out


def test_the_module_table_covers_every_switch() -> None:
    assert EXPECTED.items() <= legacy_hammer.LEGACY_ENV.items()
