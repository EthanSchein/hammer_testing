import io
import sys
import tarfile

import pytest

from hammer.shell import pd_store_cli
from hammer.vlsi import pd_store

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX rename semantics")


def _tar(members) -> bytes:
    """gzip tar of {member name: text}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in members.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def pull(tmp_path, monkeypatch):
    """`studio stage-pull synthesis --rundir obj/syn-copy --overwrite`, where obj/syn-copy already holds a run."""
    obj = tmp_path / "obj"
    (obj / "syn-copy").mkdir(parents=True)
    (obj / "syn-copy" / "mine.txt").write_text("previous run\n")
    (tmp_path / "master.json").write_text("{}")

    def run(blob: bytes) -> int:
        monkeypatch.setattr(pd_store, "load_stage_blob", lambda sha: ("synthesis", blob, None, None))
        return pd_store_cli.main(["stage-pull", "synthesis", "--rundir", str(obj / "syn-copy"),
                                  "--master", str(tmp_path / "master.json"), "--overwrite"])

    run.obj = obj
    return run


@pytest.mark.parametrize("blob", [
    b"not a tarball",
    _tar({"a/x.txt": "1", "b/y.txt": "2"}),
    _tar({"syn-rundir/ok.txt": "1", "../escape.txt": "2"}),
], ids=["corrupt", "two-top-level-entries", "member-outside-the-rundir"])
def test_a_blob_that_fails_to_extract_keeps_the_old_rundir(pull, blob) -> None:
    with pytest.raises((tarfile.TarError, ValueError)):
        pull(blob)
    assert [p.name for p in (pull.obj / "syn-copy").iterdir()] == ["mine.txt"]
    assert [p.name for p in pull.obj.iterdir()] == ["syn-copy"]


def test_a_good_blob_replaces_the_rundir_and_leaves_nothing_behind(pull) -> None:
    assert pull(_tar({"syn-rundir/genus.log": "from the blob\n"})) == 0
    assert [p.name for p in (pull.obj / "syn-copy").iterdir()] == ["genus.log"]
    assert [p.name for p in pull.obj.iterdir()] == ["syn-copy"]
