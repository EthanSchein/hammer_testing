import os
import shutil
import stat
import subprocess
import sys

import pytest

from tests.utils import fake_postgres

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")


def _setup(tmp_path, *existing, **env):
    """Run a copy of uv_setup.sh until its first uv call, which a stand-in uv fails; returns the checkout."""
    repo = fake_postgres.checkout(tmp_path / "checkout", "uv_setup.sh")
    for name in existing:
        (repo / name).mkdir(parents=True, exist_ok=True)
    fake_postgres.executable(tmp_path / "bin" / "uv", "#!/bin/sh\nexit 1\n")
    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["bash", str(repo / "scripts" / "uv_setup.sh")], capture_output=True, text=True, timeout=60,
                   cwd=str(home), env={"HOME": str(home), "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", **env})
    return repo


def test_a_fresh_checkout_defaults_to_lite(tmp_path) -> None:
    repo = _setup(tmp_path)
    launch = repo / ".sledgehammer" / "launch.env"
    assert "SLEDGE_LITE=1" in launch.read_text().splitlines()
    assert stat.S_IMODE(os.stat(repo / ".sledgehammer").st_mode) == 0o700


@pytest.mark.parametrize("existing", [".venv", ".sledgehammer"])
def test_a_checkout_set_up_before_keeps_the_full_stack(tmp_path, existing) -> None:
    repo = _setup(tmp_path, existing)
    assert not (repo / ".sledgehammer" / "launch.env").exists()


def test_the_lab_profile_records_no_launch_default(tmp_path) -> None:
    repo = _setup(tmp_path, SLEDGE_LAB="1")
    assert not (repo / ".sledgehammer").exists()
