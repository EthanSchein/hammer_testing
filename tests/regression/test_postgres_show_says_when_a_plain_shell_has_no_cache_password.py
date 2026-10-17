import os
import shutil
import sys

import pytest

from tests.utils import fake_postgres
from tests.utils.postgres_switch import PASSPHRASE, Sandbox, row

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

CONFIGURE = [PASSPHRASE, "db2.example.edu", "6543", "", "", "cache_x", "pw2", "pw2", "y", "y"]
NO_PASSWORD = ("in this shell {verb} no password for the cache, so {they} cannot log in; {they} would take one from "
               "HAMMER_PG_PASSWORD, which \"source venv.sh\" loads from the encrypted secrets, or from a line for "
               "{server} in {pgpass}")


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    sandbox.db.serve("db2.example.edu", 6543, "pw2", "airflow_alice", "cache_x")
    yield sandbox
    sandbox.restore()


def _local(box) -> str:
    rc, out, err = box.run("use", "local", answers=[PASSPHRASE, "y", "local"])
    assert rc == 0, out + err
    (box.pg_root / "data" / "postmaster.pid").write_text(f"{os.getpid()}\n")
    box.db.serve("localhost", box.port, box.local_password(), "airflow_alice", "sledgehammer_studio")
    return out


def _both(server: str, pgpass: str = "~/.pgpass") -> str:
    return "  studio and hammer-vlsi " + NO_PASSWORD.format(verb="have", they="they", server=server,
                                                            pgpass=pgpass) + "\n"


def test_after_use_local_a_plain_shell_logs_in_with_the_local_password_file(box) -> None:
    assert ("log in with HAMMER_PG_PASSWORD, a ~/.pgpass line or, on the host your local Postgres runs on, its "
            "password file.\n") in _local(box)
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, err
    assert "no password for the cache" not in out
    rc, out, err = box.run("show", tty=False)
    assert rc == 0, err
    assert row(out, "Cache", "password") == ("from", f"{box.pg_root / 'password'} (not shown)")
    assert row(out, "Cache", "reachable")[0] == "yes"
    assert "no password for the cache" not in out
    assert box.local_password() not in out + err


def _readable(box) -> str:
    (box.pg_root / "password").chmod(0o644)
    return "was not used because group or others have access to it (mode 644); chmod 600 it"


def _started_elsewhere(box) -> str:
    (box.pg_root / "host").write_text("otherhost\n")
    return f"was not used because your local Postgres was started on otherhost, not on {os.uname().nodename}"


@pytest.mark.parametrize("unusable", [_readable, _started_elsewhere], ids=["mode_644", "started_elsewhere"])
def test_show_says_why_a_plain_shell_cannot_use_the_local_password_file(box, unusable) -> None:
    _local(box)
    why = unusable(box)
    rc, out, err = box.run("show", answers=[PASSPHRASE])
    assert rc == 0, err
    assert row(out, "Cache", "reachable")[0] == "yes"
    assert _both(f"localhost:{box.port}") + f"  {box.pg_root / 'password'} {why}\n" in out
    assert box.local_password() not in out + err


def test_show_names_a_plain_shell_without_a_cache_password_exactly_when_it_has_none(box, monkeypatch) -> None:
    rc, out, err = box.run("configure", answers=CONFIGURE)
    assert rc == 0, out + err
    assert "log in with HAMMER_PG_PASSWORD or a ~/.pgpass line.\n" in out
    rc, out, _ = box.run("show", answers=[PASSPHRASE])
    assert row(out, "Cache", "reachable")[0] == "yes"
    assert _both("db2.example.edu:6543") in out
    assert "pw2" not in out
    rc, out, _ = box.run("show", tty=False)
    assert row(out, "Cache", "password")[0] == "none"
    assert _both("db2.example.edu:6543") in out

    monkeypatch.setenv("HAMMER_PG_PASSWORD", "pw2")
    assert "no password for the cache" not in box.run("show", answers=[PASSPHRASE])[1]
    monkeypatch.delenv("HAMMER_PG_PASSWORD")
    monkeypatch.setenv("PGPASSWORD", "pw2")
    assert "no password for the cache" not in box.run("show", answers=[PASSPHRASE])[1]
    monkeypatch.delenv("PGPASSWORD")
    (box.home / ".pgpass").write_text("db2.example.edu:6543:*:alice:pw2\n")
    assert "no password for the cache" not in box.run("show", answers=[PASSPHRASE])[1]
    (box.home / ".pgpass").unlink()
    monkeypatch.setenv("PGPASSFILE", str(box.tmp / "pgpass"))
    assert _both("db2.example.edu:6543", str(box.tmp / "pgpass")) in box.run("show", answers=[PASSPHRASE])[1]
    monkeypatch.delenv("PGPASSFILE")

    env_file = box.tmp / "env.sh"
    env_file.write_text("export HAMMER_PG_PASSWORD=pw2\n")
    monkeypatch.setenv("SLEDGE_ENV_FILE", str(env_file))
    rc, out, _ = box.run("show", answers=[PASSPHRASE])
    assert ("  hammer-vlsi in this shell has no password for the cache, so it cannot log in; it would take one from "
            f"HAMMER_PG_PASSWORD, which \"source {env_file}\" loads if that file sets it, or from a line for "
            "db2.example.edu:6543 in ~/.pgpass\n") in out
    assert "studio and hammer-vlsi in this shell have no password" not in out
    assert "pw2" not in out
