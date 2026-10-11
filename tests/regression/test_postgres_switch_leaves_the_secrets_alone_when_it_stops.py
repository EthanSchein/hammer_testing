import os
import shutil
import sys

import pytest

from tests.utils import fake_postgres
from tests.utils.postgres_switch import PASSPHRASE, SECRETS, Sandbox

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

CONFIGURE = [PASSPHRASE, "db2.example.edu", "6543", "", "", "cache_x", "pw2", "pw2"]


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    sandbox.db.serve("db2.example.edu", 6543, "pw2", "airflow_alice", "cache_x")
    yield sandbox
    sandbox.restore()


def assert_untouched(box) -> None:
    assert box.secrets.read_bytes() == SECRETS.encode()
    assert sorted(os.listdir(box.secrets.parent)) == [box.secrets.name]


def test_new_secrets_that_do_not_decrypt_back_are_not_kept(box, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_GPG_GARBLE", "1")
    rc, out, err = box.run("configure", answers=[*CONFIGURE, "y", "y"])
    assert rc == 1
    assert "[sledgehammer] the secrets are unchanged: the newly encrypted file did not decrypt back to the new " \
           "secrets" in err
    assert_untouched(box)


@pytest.mark.parametrize("answers", [[KeyboardInterrupt()], ["y", KeyboardInterrupt()]])
def test_ctrl_c_at_either_question_changes_nothing(box, answers) -> None:
    rc, out, err = box.run("configure", answers=[*CONFIGURE, *answers])
    assert rc == 1
    assert err.endswith("[sledgehammer] stopped; nothing changed\n")
    assert_untouched(box)
