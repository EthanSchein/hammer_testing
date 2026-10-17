import os
import types

import pytest

from hammer.vlsi import pd_store

MIB = 1024 * 1024


class FakeDB:
    """Just enough of psycopg2 for the blob and checkpoint tables, recording every statement's parameters."""

    def __init__(self):
        self.blobs, self.blob_chunks, self.ckpts, self.ckpt_chunks = {}, {}, {}, {}
        self.params = []
        self.named = []

    def connect(self, **kwargs):
        return FakeConn(self)


class FakeConn:
    def __init__(self, db):
        self.db = db

    def cursor(self, name=None):
        return FakeCursor(self.db, name)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeCursor:
    def __init__(self, db, name):
        self.db, self.name, self.rows, self.itersize = db, name, [], 2000
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        params = [bytes(p) if isinstance(p, (memoryview, bytearray)) else p for p in params]
        self.db.params.append(params)
        db, flat = self.db, " ".join(sql.split())
        if flat.startswith(f"INSERT INTO {pd_store.FQ_BLOB} "):
            db.blobs[params[0]] = (params[1], params[2], params[4], params[5], params[3])
        elif flat.startswith(f"DELETE FROM {pd_store.FQ_BLOB_CHUNK}"):
            db.blob_chunks = {k: v for k, v in db.blob_chunks.items() if k[0] != params[0]}
        elif flat.startswith(f"INSERT INTO {pd_store.FQ_BLOB_CHUNK}"):
            db.blob_chunks[(params[0], params[1])] = params[2]
        elif flat.startswith("SELECT stage, data, duration_seconds"):
            row = db.blobs.get(params[0])
            self.rows = [row] if row else []
        elif flat.startswith(f"SELECT data FROM {pd_store.FQ_BLOB_CHUNK}"):
            self.rows = [(v,) for (sha, _), v in sorted(db.blob_chunks.items()) if sha == params[0]]
        elif flat.startswith(f"INSERT INTO {pd_store.FQ_CHECKPOINT} "):
            db.ckpts[(params[0], params[2])] = dict(id=1, stage_key=params[0], stage=params[1], step=params[2],
                                                   data=params[3], size_bytes=params[4], is_dir=params[5],
                                                   saved_seconds=params[6], saved_cpu_seconds=params[7])
        elif flat.startswith(f"DELETE FROM {pd_store.FQ_CHECKPOINT_CHUNK}"):
            db.ckpt_chunks = {k: v for k, v in db.ckpt_chunks.items() if k[:2] != (params[0], params[1])}
        elif flat.startswith(f"INSERT INTO {pd_store.FQ_CHECKPOINT_CHUNK}"):
            db.ckpt_chunks[(params[0], params[1], params[2])] = params[3]
        elif flat.startswith("SELECT id, stage_key"):
            rows = [r for (k, _), r in db.ckpts.items() if k == params[0]]
            cols = ("id", "stage_key", "stage", "step", "data", "size_bytes", "is_dir", "saved_seconds",
                    "saved_cpu_seconds")
            self.description = [(c,) for c in cols]
            self.rows = [tuple(r[c] for c in cols) for r in rows]
        elif flat.startswith(f"SELECT data FROM {pd_store.FQ_CHECKPOINT_CHUNK}"):
            self.rows = [(v,) for (k, s, _), v in sorted(db.ckpt_chunks.items()) if (k, s) == tuple(params[:2])]
        else:
            raise AssertionError(f"unexpected SQL: {flat[:80]}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        assert self.name is None, "a chunk query must not fetch everything at once"
        return list(self.rows)

    def __iter__(self):
        assert self.name and self.itersize == 1, "chunks are read one row at a time"
        self.db.named.append(self.name)
        for row in self.rows:
            yield (memoryview(row[0]),)


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    driver = types.SimpleNamespace(connect=fake.connect, Binary=lambda b: b, Error=Exception)
    monkeypatch.setattr(pd_store, "psycopg2", driver)
    monkeypatch.setattr(pd_store, "_connect", lambda: FakeConn(fake))
    monkeypatch.setattr(pd_store, "_ensure_schema", lambda conn, quiet=False: None)
    return fake


def _largest_param(db) -> int:
    return max((len(p) for params in db.params for p in params if isinstance(p, bytes)), default=0)


def test_a_par_sized_blob_goes_up_in_8_mib_chunks_and_comes_back_whole(db) -> None:
    data = os.urandom(93 * MIB + 5)
    pd_store.store_stage_blob("par", "a" * 64, data, duration_seconds=900.0)
    assert db.blobs["a" * 64][1] == b""
    assert len([k for k in db.blob_chunks if k[0] == "a" * 64]) == 12
    assert _largest_param(db) == 8 * MIB
    stage, blob, duration, _ = pd_store.load_stage_blob("a" * 64)
    assert (stage, duration) == ("par", 900.0) and blob == data
    assert db.named == ["pd_chunk_reader"]


def test_a_small_blob_stays_inline(db) -> None:
    data = os.urandom(3 * MIB)
    pd_store.store_stage_blob("syn", "b" * 64, data)
    assert db.blobs["b" * 64][1] == data and not db.blob_chunks
    assert pd_store.load_stage_blob("b" * 64)[1] == data
    assert db.named == []


def test_blobs_stored_with_bigger_chunks_still_load(db) -> None:
    old = [os.urandom(128 * MIB // 64), os.urandom(1000)]
    db.blobs["c" * 64] = ("par", b"", None, None, len(old[0]) + len(old[1]))
    db.blob_chunks.update({("c" * 64, 0): old[0], ("c" * 64, 1): old[1]})
    assert pd_store.load_stage_blob("c" * 64)[1] == old[0] + old[1]


def test_a_blob_whose_chunks_go_between_the_two_reads_is_a_miss(db) -> None:
    pd_store.store_stage_blob("par", "d" * 64, os.urandom(20 * MIB))
    db.blob_chunks = {k: v for k, v in db.blob_chunks.items() if k[0] != "d" * 64}
    assert pd_store.load_stage_blob("d" * 64) is None


def test_checkpoints_use_the_same_chunks(db, tmp_path) -> None:
    payload = tmp_path / "pre_route"
    payload.write_bytes(os.urandom(20 * MIB))
    size = pd_store.store_checkpoint("k" * 64, "par", "route", payload)
    assert _largest_param(db) <= 8 * MIB and len(db.ckpt_chunks) == 3
    rec = pd_store.fetch_checkpoint("k" * 64)
    assert len(rec["data"]) == size
    assert rec["data"] == bytes(b"".join(db.ckpt_chunks[k] for k in sorted(db.ckpt_chunks)))
    assert db.named == ["pd_chunk_reader"]
