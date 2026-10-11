import shutil
import sys

import pytest

from hammer.shell import sledgehammer_cli
from tests.utils import fake_postgres
from tests.utils.postgres_switch import SECRETS, Sandbox

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    return fake_postgres.system_tools(tmp_path_factory.mktemp("tools"))


@pytest.fixture
def box(tmp_path, monkeypatch, capsys, tools):
    sandbox = Sandbox(tmp_path, monkeypatch, capsys, tools)
    yield sandbox
    sandbox.restore()


@pytest.mark.parametrize("command", [["use", "local"], ["configure"]])
def test_without_a_terminal_a_switch_refuses_in_one_line(box, command) -> None:
    rc, out, err = box.run(*command, tty=False)
    assert rc == 1
    assert out == ""
    assert err == f"[sledgehammer] postgres {' '.join(command)} needs a terminal to ask you to confirm the " \
                  "switch; nothing changed\n"
    assert box.prompts == [] and box.gpg_calls() == []
    assert box.secrets.read_text() == SECRETS and not box.target.exists()


def test_postgres_is_its_own_command_and_db_stays_airflows(box, monkeypatch) -> None:
    rc, out, _ = box.run("--help")
    assert rc == 0
    assert out.startswith("sledgehammer postgres: see and change which Postgres SledgeHammer uses.")
    rc, _, err = box.run("use", "barney")
    assert rc == 2 and "sledgehammer postgres configure" in err
    monkeypatch.setenv("SLEDGE_DRYRUN", "1")
    monkeypatch.setattr(sys, "argv", ["sledgehammer", "db", "migrate"])
    assert sledgehammer_cli.main() == 0
    assert "[dryrun] passthrough" in box.capsys.readouterr().out
