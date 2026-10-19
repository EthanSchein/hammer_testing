import os
import shutil
import subprocess
import sys

import pytest

HELPER = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                      "scripts", "bashrc_drop_old_sledgehammer.sh")
OLD_BLOCK = """\
sledgehammer() {
    local repo; repo="$(git rev-parse --show-toplevel 2>/dev/null)"
    [ -n "$repo" ] && [ -f "$repo/scripts/airflow-standalone-ldap.py" ] || { echo "sledgehammer: cd into a hammer checkout first"; return 1; }
    ( cd "$repo" && source ./venv.sh && export PATH="$repo/.venv/bin:$PATH" && exec ./scripts/airflow-standalone-ldap.py "$@" )
}
"""
USER_FUNC = 'sledgehammer() {\n    echo mine\n}'
MARKER = "# sledgehammer() shell function retired by scripts/uv_setup.sh; the command is ~/.local/bin/sledgehammer\n"

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="the cleanup helper needs bash")


def _run(rc):
    with open(HELPER) as f:
        script = f.read().replace("\r\n", "\n")
    return subprocess.run(["bash", "-c", script, "helper", str(rc)], capture_output=True, text=True, check=True).stdout


def test_old_block_removed_with_backup(tmp_path) -> None:
    rc = tmp_path / ".bashrc"
    before = "export A=1\n\n" + OLD_BLOCK + "export B=2\n"
    rc.write_text(before)
    assert "removed" in _run(rc)
    assert rc.read_text() == "export A=1\n\nexport B=2\n" + MARKER
    assert (tmp_path / ".bashrc.sledgehammer.bak").read_text() == before


@pytest.mark.parametrize("text", [
    "export A=1\n" + USER_FUNC + "\n",
    "export A=1\n" + USER_FUNC,
    USER_FUNC + "\nexport A=1",
])
def test_user_function_left_byte_identical(tmp_path, text) -> None:
    rc = tmp_path / ".bashrc"
    rc.write_text(text)
    assert "left alone" in _run(rc)
    assert rc.read_text() == text
    assert not (tmp_path / ".bashrc.sledgehammer.bak").exists()


def test_missing_bashrc_is_not_an_error(tmp_path) -> None:
    assert "none found" in _run(tmp_path / ".bashrc")
    assert not (tmp_path / ".bashrc").exists()


def test_symlinked_bashrc_keeps_the_link(tmp_path) -> None:
    real = tmp_path / "dotfiles" / "bashrc"
    real.parent.mkdir()
    real.write_text(OLD_BLOCK)
    rc = tmp_path / ".bashrc"
    rc.symlink_to(real)
    _run(rc)
    assert rc.is_symlink()
    assert real.read_text() == MARKER


def test_rerun_is_a_noop(tmp_path) -> None:
    rc = tmp_path / ".bashrc"
    rc.write_text("export A=1\n" + OLD_BLOCK)
    _run(rc)
    after = rc.read_text()
    assert "none found" in _run(rc)
    assert rc.read_text() == after
    assert after.count(MARKER) == 1


def test_marker_blocks_the_old_guard(tmp_path) -> None:
    rc = tmp_path / ".bashrc"
    rc.write_text("export A=1\n")
    _run(rc)
    assert subprocess.run(["grep", "-q", "sledgehammer()", str(rc)]).returncode == 0
    assert subprocess.run(["grep", "-q", "^sledgehammer() {$", str(rc)]).returncode == 1


def test_user_function_gets_no_marker(tmp_path) -> None:
    rc = tmp_path / ".bashrc"
    rc.write_text(USER_FUNC + "\n")
    _run(rc)
    assert MARKER not in rc.read_text()


def test_marker_goes_on_its_own_line(tmp_path) -> None:
    rc = tmp_path / ".bashrc"
    rc.write_text("export A=1")
    _run(rc)
    assert rc.read_text() == "export A=1\n" + MARKER
