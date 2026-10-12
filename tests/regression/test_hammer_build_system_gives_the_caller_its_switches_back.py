import json
import os
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver, HammerTool, error_scan, rtl_check, substep_resume

NAMES = ("HAMMER_PD_CACHE", "HAMMER_PD_CACHE_LEDGER", "HAMMER_SUBSTEP_RESUME", "HAMMER_DB_CHECKPOINTS",
         "HAMMER_CHECKPOINT_STREAM", "HAMMER_ERROR_SCAN", "HAMMER_RTL_FP_CACHE", "HAMMER_CODE_FP_CACHE")

# What the caller exported, as an in-process caller such as flowgraph or a test session would.
EXPORTED = {"HAMMER_SUBSTEP_RESUME": "1", "HAMMER_ERROR_SCAN": "1", "HAMMER_DB_CHECKPOINTS": "0",
            "HAMMER_RTL_FP_CACHE": "off", "HAMMER_CODE_FP_CACHE": "off"}

SEEN = []


def _probe(x: HammerTool) -> bool:
    SEEN.append({"substep_resume": substep_resume.is_enabled(), "error_scan": error_scan.is_enabled(),
                 "HAMMER_CHECKPOINT_STREAM": os.environ.get("HAMMER_CHECKPOINT_STREAM"),
                 "HAMMER_PD_CACHE": os.environ.get("HAMMER_PD_CACHE")})
    return True


class ProbeDriver(CLIDriver):
    def get_extra_synthesis_hooks(self):
        return [HammerTool.make_post_insertion_hook("step1", _probe)]


def _fixed_digest(paths, include_dirs=(), defines=(), top_module=None):
    return "0" * 64, []


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    for name in (*NAMES, "HAMMER_AIRFLOW_DESIGN", "HAMMER_AIRFLOW_RUN_ID", "AIRFLOW_CTX_DAG_RUN_ID"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    for name, value in EXPORTED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "airflow"))
    monkeypatch.setattr(rtl_check, "digest_units", _fixed_digest)
    monkeypatch.setattr(rtl_check, "digest_files", _fixed_digest)
    SEEN.clear()


def _syn(d: Path, build_system: str) -> None:
    d.mkdir()
    rtl = d / "top.v"
    rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
    (d / "mock").mkdir()
    (d / "config.json").write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.build_system": build_system,
        "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.technology.extra_macro_sizes": [],
        "synthesis.inputs.top_module": "dummy",
        "synthesis.inputs.input_files": [str(rtl)],
        "synthesis.mocksynth.temp_folder": str(d / "mock"),
    }, cls=HammerJSONEncoder, indent=4))
    with pytest.raises(SystemExit) as cm:
        ProbeDriver().main(args=["syn", "-p", str(d / "config.json"), "-o", str(d / "out.json"),
                                 "--obj_dir", str(d / "obj"), "--log", str(d / "log.txt")])
    assert cm.value.code == 0


def test_the_next_in_process_action_gets_the_callers_switches(tmp_path) -> None:
    before = {name: os.environ.get(name) for name in NAMES}
    _syn(tmp_path / "hammer", "hammer")
    assert SEEN == [{"substep_resume": False, "error_scan": False, "HAMMER_CHECKPOINT_STREAM": "0",
                     "HAMMER_PD_CACHE": "0"}]
    assert {name: os.environ.get(name) for name in NAMES} == before

    _syn(tmp_path / "make", "make")
    assert SEEN[1] == {"substep_resume": True, "error_scan": True, "HAMMER_CHECKPOINT_STREAM": None,
                       "HAMMER_PD_CACHE": None}
