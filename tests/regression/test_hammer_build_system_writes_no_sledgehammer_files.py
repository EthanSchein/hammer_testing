import json
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver, code_fingerprints


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No SledgeHammer switch or Airflow run from the caller's shell, and the fingerprint memos left at
    their default (on) with XDG_CACHE_HOME in tmp_path, so a fingerprint would leave a memo there."""
    for name in ("HAMMER_PD_CACHE", "HAMMER_PD_CACHE_LEDGER", "HAMMER_SUBSTEP_RESUME", "HAMMER_DB_CHECKPOINTS",
                 "HAMMER_CHECKPOINT_STREAM", "HAMMER_ERROR_SCAN", "HAMMER_RTL_FP_CACHE", "HAMMER_CODE_FP_CACHE",
                 "HAMMER_AIRFLOW_DESIGN", "HAMMER_AIRFLOW_RUN_ID", "AIRFLOW_CTX_DAG_RUN_ID"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    # the make contrast below must not reach for Postgres checkpoints
    monkeypatch.setenv("HAMMER_DB_CHECKPOINTS", "0")
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "airflow"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    # empty in-process digest caches, so a code fingerprint here would reach the on-disk memo
    for cache in ("_SNAPSHOT_DIGEST", "_FIRST", "_DIGEST_MEMO"):
        monkeypatch.setattr(code_fingerprints, cache, {})


def _flow(tmp_path: Path, build_system: str) -> Path:
    rtl = tmp_path / "top.v"
    rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
    (tmp_path / "mock").mkdir()
    (tmp_path / "config.json").write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.build_system": build_system,
        "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
        "vlsi.core.par_tool": "hammer.par.nop",
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.technology.extra_macro_sizes": [],
        "synthesis.inputs.top_module": "dummy",
        "synthesis.inputs.input_files": [str(rtl)],
        "synthesis.mocksynth.temp_folder": str(tmp_path / "mock"),
    }, cls=HammerJSONEncoder, indent=4))
    obj = tmp_path / "obj"

    def run(action: str, *configs: str) -> None:
        args = [action]
        for c in configs or (str(tmp_path / "config.json"),):
            args += ["-p", c]
        with pytest.raises(SystemExit) as cm:
            CLIDriver().main(args=args + ["-o", str(tmp_path / f"{action}-out.json"), "--obj_dir", str(obj),
                                          "--log", str(tmp_path / "log.txt")])
        assert cm.value.code == 0, action

    for _ in range(2):
        run("syn")
        run("syn-to-par", str(obj / "syn-rundir" / "syn-output-full.json"))
        run("par", str(tmp_path / "syn-to-par-out.json"))
        run("syn-par")
    return obj


def _sledgehammer_files(root: Path) -> list:
    names = ("master_database.json", ".substep_resume.json", ".sledgehammer-run.lock", "hammer_dag.py")
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.name in names or p.name.endswith((".cpustamp", ".jsonl")))


def test_hammer_writes_only_what_upstream_hammer_writes(tmp_path) -> None:
    obj = _flow(tmp_path, "hammer")
    assert _sledgehammer_files(tmp_path) == []
    assert not (tmp_path / "xdg").exists()
    assert not (tmp_path / "airflow").exists()
    for stage in ("syn", "par"):
        assert (obj / f"{stage}-rundir" / f"{stage}-output.json").is_file()
        assert (obj / f"{stage}-rundir" / f"{stage}-output-full.json").is_file()


def test_make_still_keeps_its_master_database(tmp_path) -> None:
    obj = _flow(tmp_path, "make")
    assert (obj / "master_database.json").is_file()
    assert (obj / "syn-rundir" / ".substep_resume.json").is_file()
    assert any(p.is_file() for p in (tmp_path / "xdg" / "sledgehammer").rglob("*"))
