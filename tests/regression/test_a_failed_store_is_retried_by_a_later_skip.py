import json
import os
from pathlib import Path

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import CLIDriver, pd_cache, pd_store, rtl_check


def _fixed_digest(paths, include_dirs=(), defines=(), top_module=None):
    return "0" * 64, []


@pytest.fixture
def cache(tmp_path, monkeypatch, capsys):
    """A syn flow on mocksynth against a cache whose entries, store failures and outages the test sets."""
    monkeypatch.setattr(rtl_check, "digest_units", _fixed_digest)
    monkeypatch.setattr(rtl_check, "digest_files", _fixed_digest)
    monkeypatch.setenv("HAMMER_PD_CACHE", "1")
    monkeypatch.setenv("HAMMER_DB_CHECKPOINTS", "0")
    monkeypatch.delenv("HAMMER_AIRFLOW_DESIGN", raising=False)
    monkeypatch.setattr(pd_cache, "_record_cache_event", lambda *a, **k: None)
    monkeypatch.setattr(pd_cache, "_stamp_project_from_config", lambda *a, **k: None)
    state = type("Cache", (), {})()
    state.entries, state.stores, state.tars, state.fail, state.down = {}, [], [], [], False
    state.provenance = []
    state.before_store = lambda: None

    def store(stage, key, data, duration_seconds=None, cpu_seconds=None, **kw):
        state.before_store()
        if state.down:
            raise pd_store.DatabaseUnavailable("the cache database at db:5432 did not answer")
        if state.fail:
            raise state.fail.pop(0)
        state.stores.append((key, duration_seconds, cpu_seconds))
        state.provenance.append(kw)
        state.entries[key] = (duration_seconds, cpu_seconds)

    def load(key):
        if state.down:
            raise pd_store.DatabaseUnavailable("the cache database at db:5432 did not answer")

    def runtime(key):
        if state.down:
            raise pd_store.DatabaseUnavailable("the cache database at db:5432 did not answer")
        return state.entries.get(key)

    real_tar = pd_store.tar_directory
    monkeypatch.setattr(pd_store, "tar_directory", lambda path: state.tars.append(path) or real_tar(path))
    monkeypatch.setattr(pd_store, "load_stage_blob", load)
    monkeypatch.setattr(pd_store, "store_stage_blob", store)
    monkeypatch.setattr(pd_store, "stage_blob_runtime", runtime)
    rtl = tmp_path / "top.v"
    rtl.write_text("module dummy(input a, output b); assign b = a; endmodule\n")
    (tmp_path / "mock").mkdir()
    cfg = {
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
        "vlsi.core.par_tool": "hammer.par.nop",
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.technology.extra_macro_sizes": [],
        "synthesis.inputs.top_module": "dummy",
        "synthesis.inputs.input_files": [str(rtl)],
        "synthesis.mocksynth.temp_folder": str(tmp_path / "mock"),
    }

    def configure(action: str, rundir: str, **extra) -> None:
        state.action = action
        state.pending = tmp_path / "obj" / f".{rundir}.pd-store-pending.json"
        (tmp_path / "config.json").write_text(json.dumps({**cfg, **extra}, cls=HammerJSONEncoder))
    configure("syn", "syn-rundir")
    state.configure = configure

    def syn(*flags: str) -> str:
        capsys.readouterr()
        with pytest.raises(SystemExit) as cm:
            CLIDriver().main(args=[state.action, *flags, "-o", str(tmp_path / "output.json"),
                                   "-p", str(tmp_path / "config.json"), "--obj_dir", str(tmp_path / "obj"),
                                   "--log", str(tmp_path / "log.txt")])
        assert cm.value.code == 0
        return capsys.readouterr().out
    state.syn = syn
    return state


def test_a_store_that_failed_is_made_by_the_next_skip_with_its_runtime(cache) -> None:
    cache.fail.append(RuntimeError("server closed the connection unexpectedly"))
    out = cache.syn()
    assert "store failed (server closed the connection unexpectedly); continuing" in out
    assert cache.stores == [] and cache.pending.exists()
    out = cache.syn()
    assert "can skip" in out.lower()
    (key, duration, cpu), = cache.stores
    assert duration is not None and duration > 0
    assert "stored the synthesis result an earlier run failed to store" in out
    assert not cache.pending.exists()
    assert pd_cache._pending_path(str(cache.pending.parent / "syn-rundir")) == cache.pending
    cache.syn()
    assert len(cache.stores) == 1


def test_a_cache_that_is_down_keeps_the_store_for_later(cache) -> None:
    cache.fail.append(RuntimeError("could not connect"))
    cache.syn()
    cache.down = True
    cache.syn()
    assert cache.stores == [] and cache.pending.exists()
    cache.down = False
    cache.syn()
    assert len(cache.stores) == 1 and not cache.pending.exists()


def test_a_step_flag_run_is_never_stored_by_a_later_skip(cache) -> None:
    cache.syn()
    cache.entries.clear()
    cache.syn("--to_step", "step4")
    out = cache.syn()
    assert "can skip" in out.lower()
    assert len(cache.stores) == 1 and cache.tars == [cache.tars[0]]


def test_a_step_flag_run_after_a_failed_store_drops_the_retry(cache) -> None:
    cache.fail.append(RuntimeError("server closed the connection unexpectedly"))
    cache.syn()
    assert cache.pending.exists()
    cache.syn("--to_step", "step4")
    assert not cache.pending.exists()
    cache.syn()
    assert cache.stores == []


def test_an_entry_deleted_on_purpose_stays_deleted(cache) -> None:
    cache.syn()
    cache.entries.clear()
    cache.syn()
    assert len(cache.stores) == 1 and len(cache.tars) == 1


def test_a_store_that_keeps_failing_is_given_up_after_three_skips(cache) -> None:
    cache.fail.extend(RuntimeError("permission denied for table pd_blobs") for _ in range(4))
    cache.syn()
    for tries in range(1, 4):
        out = cache.syn()
        assert "could not store the synthesis result an earlier run failed to store (permission denied" in out
    assert "3 times; leaving it out of the cache" in out
    assert not cache.pending.exists() and len(cache.tars) == 4
    cache.syn()
    assert len(cache.tars) == 4 and cache.stores == []


def test_a_cache_that_was_down_when_the_stage_started_gets_the_result_later(cache) -> None:
    cache.down = True
    out = cache.syn()
    assert "lookup failed (the cache database at db:5432 did not answer); running synthesis normally" in out
    assert cache.pending.exists()
    cache.down = False
    cache.syn()
    assert len(cache.stores) == 1 and not cache.pending.exists()


def test_a_run_with_the_cache_off_drops_the_retry(cache, monkeypatch) -> None:
    cache.fail.append(RuntimeError("server closed the connection unexpectedly"))
    cache.syn()
    monkeypatch.setenv("HAMMER_PD_CACHE", "0")
    cache.syn("--to_step", "step4")
    assert not cache.pending.exists()
    monkeypatch.setenv("HAMMER_PD_CACHE", "1")
    cache.syn()
    assert cache.stores == []


def test_a_rundir_changed_some_other_way_is_not_stored(cache) -> None:
    cache.fail.append(RuntimeError("server closed the connection unexpectedly"))
    cache.syn()
    (cache.pending.parent / "syn-rundir" / "extra.log").write_text("written by a run the cache never saw\n")
    out = cache.syn()
    assert "synthesis's rundir changed after its store failed; not storing it" in out
    assert cache.stores == [] and not cache.pending.exists()


def test_the_retry_records_the_run_that_made_the_result(cache, monkeypatch) -> None:
    monkeypatch.setenv("HAMMER_AIRFLOW_RUN_ID", "manual__first")
    cache.fail.append(RuntimeError("server closed the connection unexpectedly"))
    cache.syn()
    monkeypatch.setenv("HAMMER_AIRFLOW_RUN_ID", "manual__second")
    cache.syn()
    assert [kw["dag_run_id"] for kw in cache.provenance] == ["manual__first"]


REFUSED = ('connection to server at "db", port 5432 failed: Connection refused\n'
           "\tIs the server running on that host and accepting TCP/IP connections?\n")


def test_each_cache_warning_is_one_line(cache) -> None:
    cache.fail.extend([pd_store.DatabaseUnavailable(REFUSED), pd_store.DatabaseUnavailable(REFUSED)])
    out = cache.syn() + cache.syn()
    assert 'store failed (connection to server at "db", port 5432 failed: Connection refused); continuing' in out
    assert 'failed to store (connection to server at "db", port 5432 failed: Connection refused); a later' in out
    assert "Is the server running" not in out


def test_the_master_mirror_warning_is_one_line(monkeypatch, tmp_path, capsys) -> None:
    from hammer.config import HammerDatabase
    monkeypatch.setenv("HAMMER_PD_CACHE", "1")
    monkeypatch.setenv("HAMMER_AIRFLOW_DESIGN", "gcd")

    def refused(*a, **k):
        raise RuntimeError(REFUSED)
    monkeypatch.setattr(pd_store, "store_artifact", refused)
    HammerDatabase()._mirror_master_to_db(tmp_path / "master_database.json", "{}")
    assert capsys.readouterr().out.splitlines() == [
        '[master-db] WARNING: postgres mirror failed (connection to server at "db", port 5432 failed: Connection '
        f'refused); the on-disk file at {tmp_path / "master_database.json"} is the only copy of this write']


HIER = {
    "vlsi.inputs.hierarchical.top_module": "dummy",
    "vlsi.inputs.hierarchical.flat": "hierarchical",
    "vlsi.inputs.hierarchical.config_source": "manual",
    "vlsi.inputs.hierarchical.manual_modules": [{"mod1": ["m1s1"], "dummy": ["mod1"]}],
    "vlsi.inputs.hierarchical.manual_placement_constraints": [],
    "vlsi.inputs.hierarchical.constraints": [],
}


def test_a_hierarchical_modules_failed_store_is_made_by_its_next_skip(cache) -> None:
    cache.configure("syn-m1s1", "syn-m1s1", **HIER)
    cache.fail.append(RuntimeError("server closed the connection unexpectedly"))
    cache.syn()
    assert cache.pending.exists()
    out = cache.syn()
    assert "stored the synthesis result an earlier run failed to store" in out
    assert len(cache.stores) == 1 and not cache.pending.exists()


def test_an_odd_file_name_never_fails_a_finished_stage(cache) -> None:
    rundir = os.fsencode(cache.pending.parent / "syn-rundir")
    cache.before_store = lambda: open(os.path.join(rundir, b"report_\xe9.txt"), "wb").close()
    cache.fail.append(RuntimeError("server closed the connection unexpectedly"))
    cache.syn()
    assert cache.pending.exists()
    cache.before_store = lambda: None
    cache.syn()
    assert len(cache.stores) == 1


def test_an_entry_someone_else_stored_ends_the_retry(cache) -> None:
    cache.fail.append(RuntimeError("could not connect"))
    cache.syn()
    key = json.loads(cache.pending.read_text())["key"]
    cache.entries[key] = (5.0, 6.0)
    cache.syn()
    assert cache.stores == [] and not cache.pending.exists() and len(cache.tars) == 1
