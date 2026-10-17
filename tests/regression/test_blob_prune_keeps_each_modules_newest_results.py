import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from hammer.config import HammerJSONEncoder
from hammer.shell import pd_store_cli
from hammer.vlsi import HammerDriver, HammerDriverOptions, pd_cache, pd_store, rtl_check

T0 = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)


def row(sha, minutes, size=93 * 2**20):
    return (sha, "par", size, 1800.0, 7200.0, "t1a", "alice", "sledgehammer_gcd_alice", "gcd", "/w",
            T0 + timedelta(minutes=minutes))


class FakeDB:
    """Answers the prune's listing with ``old`` and keeps the table as {sha: created_at} for its deletes."""

    def __init__(self, old):
        self.old = old
        self.table = {r[0]: r[-1] for r in old}
        self.chunks = {r[0] for r in old}
        self.log = []
        self.commits = 0
        self.fail_update = False

    def cursor(self):
        db = self

        class Cur:
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                sql = " ".join(sql.split())
                db.log.append((sql, params))
                if sql.startswith(f"DELETE FROM {pd_store.FQ_BLOB} "):
                    sha, created = params
                    self.rowcount = int(db.table.get(sha) == created)
                    if self.rowcount:
                        del db.table[sha]
                elif sql.startswith(f"DELETE FROM {pd_store.FQ_BLOB_CHUNK} "):
                    db.chunks.discard(params[0])
                elif sql.startswith(f"UPDATE {pd_store.FQ_BLOB} SET module") and db.fail_update:
                    raise RuntimeError('column "module" of relation "pd_blobs" does not exist')

            def fetchall(self):
                return list(db.old)

        return Cur()

    def commit(self):
        self.commits += 1

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    box = SimpleNamespace(db=None)
    monkeypatch.setattr(pd_store, "_connect", lambda: box.db)
    monkeypatch.setattr(pd_store, "_ensure_schema", lambda conn, quiet=False: None)
    monkeypatch.setattr(pd_store, "psycopg2", SimpleNamespace(Binary=bytes, Error=RuntimeError,
                                                         OperationalError=ConnectionError))

    def use(old):
        box.db = FakeDB(old)
        return box.db
    return use


def test_the_listing_ranks_each_stage_design_module_and_owner_newest_first(db) -> None:
    fake = db([])
    pd_store.prunable_stage_blobs(3, stage="par")
    sql, params = fake.log[0]
    assert "row_number() OVER (PARTITION BY stage, design, module, owner ORDER BY created_at DESC, sha256) AS newest" \
        in sql
    assert f"FROM {pd_store.FQ_BLOB} WHERE stage = %s) ranked WHERE newest > %s" in sql
    assert params == ["par", 3]
    fake = db([])
    pd_store.prune_stage_blobs(2)
    assert fake.log[0][1] == [2] and "WHERE stage" not in fake.log[0][0]


def test_a_dry_run_deletes_nothing(db) -> None:
    fake = db([row("a" * 64, 0), row("b" * 64, 1)])
    assert [r[0] for r in pd_store.prune_stage_blobs(1, dry_run=True)] == ["a" * 64, "b" * 64]
    assert len(fake.table) == 2 and len(fake.log) == 1


def test_each_blob_goes_with_its_chunks_in_its_own_transaction(db) -> None:
    fake = db([row("a" * 64, 0), row("b" * 64, 1)])
    assert [r[0] for r in pd_store.prune_stage_blobs(1)] == ["a" * 64, "b" * 64]
    assert fake.table == {} and fake.chunks == set()
    assert fake.commits == 2


def test_a_blob_rewritten_since_the_listing_keeps_its_chunks(db) -> None:
    fake = db([row("a" * 64, 0), row("b" * 64, 1)])
    fake.table["a" * 64] = T0 + timedelta(hours=2)
    assert [r[0] for r in pd_store.prune_stage_blobs(1)] == ["b" * 64]
    assert "a" * 64 in fake.table and fake.chunks == {"a" * 64}


def test_keep_must_be_at_least_one(db) -> None:
    db([])
    with pytest.raises(ValueError, match="at least 1"):
        pd_store.prunable_stage_blobs(0)


def test_the_command_shows_then_deletes(db, capsys) -> None:
    fake = db([row("a" * 64, 0), row("b" * 64, 1)])
    assert pd_store_cli.main(["blob-prune", "--keep", "2", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "2 blob(s), 186.0MB, are older than the newest 2 of their stage, design, module and owner:" in out
    assert "Deleted 2 blob(s), 186.0MB." in out and fake.table == {}


def test_the_command_deletes_only_the_blobs_it_showed(db, monkeypatch, capsys) -> None:
    fake = db([row("a" * 64, 0)])

    def confirm(prompt, assume_yes):
        fake.old.append(row("c" * 64, 2))
        fake.table["c" * 64] = fake.old[-1][-1]
        return True
    monkeypatch.setattr(pd_store_cli, "_confirm", confirm)
    assert pd_store_cli.main(["blob-prune", "--keep", "1"]) == 0
    assert "Deleted 1 blob(s)" in capsys.readouterr().out
    assert list(fake.table) == ["c" * 64]


def test_the_command_dry_run(db, capsys) -> None:
    fake = db([row("a" * 64, 0)])
    assert pd_store_cli.main(["blob-prune", "--keep", "1", "--dry-run"]) == 0
    assert "[dry-run] would delete 1 blob(s)" in capsys.readouterr().out and fake.table


def test_the_command_with_nothing_to_prune(db, capsys) -> None:
    db([])
    assert pd_store_cli.main(["blob-prune", "--keep", "5"]) == 0
    assert "nothing to prune" in capsys.readouterr().out


@pytest.mark.parametrize("keep", ["0", "-1", "two"])
def test_the_command_wants_a_positive_keep(keep, capsys) -> None:
    with pytest.raises(SystemExit):
        pd_store_cli.main(["blob-prune", "--keep", keep])
    assert f"expected a whole number of at least 1, got '{keep}'" in capsys.readouterr().err


@pytest.mark.parametrize("fail", [False, True])
def test_a_store_labels_its_module_and_still_lands_on_a_server_without_the_column(db, fail) -> None:
    fake = db([])
    fake.fail_update = fail
    pd_store.store_stage_blob("synthesis", "s" * 64, b"tarball", module="SubModA")
    sqls = [sql for sql, _ in fake.log]
    assert ("UPDATE " + pd_store.FQ_BLOB + " SET module = %s WHERE sha256 = %s", ("SubModA", "s" * 64)) in fake.log
    assert sqls[-1] == ("ROLLBACK TO SAVEPOINT pd_blob_module" if fail else "RELEASE SAVEPOINT pd_blob_module")
    assert fake.commits == 1


def test_a_store_without_a_module_runs_no_update(db) -> None:
    fake = db([])
    pd_store.store_stage_blob("synthesis", "s" * 64, b"tarball")
    assert not any("module" in sql for sql, _ in fake.log)


def test_the_cache_stores_each_result_with_its_module(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rtl_check, "digest_units", lambda *a, **k: ("0" * 64, []))
    monkeypatch.setattr(rtl_check, "digest_files", lambda *a, **k: ("0" * 64, []))
    monkeypatch.setenv("HAMMER_PD_CACHE", "1")
    monkeypatch.setenv("HAMMER_DB_CHECKPOINTS", "0")
    monkeypatch.setattr(pd_cache, "_record_cache_event", lambda *a, **k: None)
    monkeypatch.setattr(pd_cache, "_stamp_project_from_config", lambda *a, **k: None)
    monkeypatch.setattr(pd_store, "load_stage_blob", lambda key: None)
    stores = []
    monkeypatch.setattr(pd_store, "store_stage_blob", lambda stage, key, data, **kw: stores.append(kw))
    rtl = tmp_path / "top.v"
    rtl.write_text("module SubModA(input a, output b); assign b = a; endmodule\n")
    (tmp_path / "config.json").write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.synthesis_tool": "hammer.synthesis.mocksynth",
        "vlsi.core.par_tool": "hammer.par.nop",
        "vlsi.inputs.hierarchical.config_source": "none",
        "vlsi.technology.extra_macro_sizes": [],
        "synthesis.inputs.top_module": "SubModA",
        "synthesis.inputs.input_files": [str(rtl)],
    }, cls=HammerJSONEncoder))
    driver = HammerDriver(HammerDriverOptions(environment_configs=[], project_configs=[str(tmp_path / "config.json")],
                                              log_file=str(tmp_path / "log.txt"), obj_dir=str(tmp_path / "obj")))
    rundir = tmp_path / "obj" / "syn-SubModA"
    rundir.mkdir(parents=True)
    assert pd_cache.cache_or_run(driver, "synthesis", str(rundir), "syn-output.json", run_fn=lambda: (True, {})) \
        == (True, {})
    assert [kw["module"] for kw in stores] == ["SubModA"]


def test_the_command_explains_a_missing_module_column(db, monkeypatch, capsys) -> None:
    db([])

    class UndefinedColumn(Exception):
        pgcode = "42703"

    def missing(keep, stage=None):
        raise UndefinedColumn('column "module" does not exist')
    monkeypatch.setattr(pd_store, "prunable_stage_blobs", missing)
    assert pd_store_cli.main(["blob-prune", "--keep", "2", "--yes"]) == 1
    assert "their owner adds it by running `studio init` with this version" in capsys.readouterr().err
