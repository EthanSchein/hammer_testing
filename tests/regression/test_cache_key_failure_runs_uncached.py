import json
import os
from types import SimpleNamespace

import pytest

from hammer.vlsi import pd_cache, pd_store


@pytest.fixture
def cache(monkeypatch):
    monkeypatch.setenv("HAMMER_PD_CACHE", "1")
    monkeypatch.setattr(pd_cache, "_stamp_project_from_config", lambda driver: None)
    monkeypatch.setattr(pd_cache, "_stage_module", lambda driver, stage: None)
    monkeypatch.setattr(pd_cache, "_record_cache_event", lambda *a, **k: None)
    streamed, stored = [], []
    monkeypatch.setattr(pd_cache, "_run_with_checkpoint_stream",
                        lambda driver, tag, rundir, fn: streamed.append((tag, rundir)) or fn())
    monkeypatch.setattr(pd_store, "store_stage_blob", lambda *a, **k: stored.append(a))
    return streamed, stored


def _boom(*a, **k):
    raise RuntimeError("boom")


def _cache_or_run(rundir):
    return pd_cache.cache_or_run(SimpleNamespace(log=None), "par", str(rundir), "par-output.json",
                                 run_fn=lambda: (True, {"par.outputs.x": 1}))


def test_key_failure_streams_checkpoints_and_never_stores(tmp_path, cache, monkeypatch) -> None:
    streamed, stored = cache
    monkeypatch.setattr(pd_cache, "_build_cache_key", _boom)
    monkeypatch.setattr(pd_store, "load_stage_blob", _boom)
    assert _cache_or_run(tmp_path) == (True, {"par.outputs.x": 1})
    assert streamed == [("par", str(tmp_path))]
    assert stored == []


def test_lookup_failure_streams_checkpoints_and_stores_the_result(tmp_path, cache, monkeypatch) -> None:
    streamed, stored = cache
    monkeypatch.setattr(pd_cache, "_build_cache_key", lambda driver, stage: "k")
    monkeypatch.setattr(pd_store, "load_stage_blob", _boom)
    assert _cache_or_run(tmp_path) == (True, {"par.outputs.x": 1})
    assert streamed == [("par", str(tmp_path))]
    assert [a[:2] for a in stored] == [("par", "k")]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs file permissions that bind")
def test_unreadable_stage_input_fails_the_key(tmp_path) -> None:
    obj = tmp_path / "obj"
    netlist = obj / "syn-rundir" / "top.v"
    netlist.parent.mkdir(parents=True)
    netlist.write_text("module top; endmodule\n")
    db = {"par.inputs.input_files": [str(netlist)], "par.inputs.top_module": "top"}
    driver = SimpleNamespace(database=SimpleNamespace(get_database_json=lambda: json.dumps(db)),
                             obj_dir=str(obj))
    assert pd_cache._build_cache_key(driver, "par")
    netlist.chmod(0)
    try:
        with pytest.raises(PermissionError):
            pd_cache._build_cache_key(driver, "par")
    finally:
        netlist.chmod(0o644)
