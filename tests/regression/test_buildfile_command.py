import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hammer.shell import sledgehammer_cli


@pytest.fixture
def buildfile(tmp_path, monkeypatch):
    """main() with argv ['sledgehammer', 'buildfile', ...] from tmp_path/vlsi; returns the exit code."""
    vlsi = tmp_path / "vlsi"
    vlsi.mkdir()
    monkeypatch.chdir(vlsi)
    monkeypatch.setenv("HAMMER_DAGS_FOLDER", str(tmp_path / "dags"))
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path))
    monkeypatch.delenv("SLEDGE_DRYRUN", raising=False)
    monkeypatch.setattr(sledgehammer_cli, "_load_stack_env", lambda: None)

    def run(*argv: str) -> int:
        monkeypatch.setattr(sys, "argv", ["sledgehammer", "buildfile", *argv])
        return sledgehammer_cli.main()

    run.vlsi = vlsi
    return run


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("make") is None, reason="needs make")
class TestBuildfile:
    def test_buildfile_runs_make_with_venv_first(self, buildfile, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        (buildfile.vlsi / "Makefile").write_text(
            "buildfile:\n\tcommand -v python3 > py.txt; echo $(design) > design.txt\n")
        assert buildfile("design=foo") == 0
        py = (buildfile.vlsi / "py.txt").read_text().strip()
        assert os.path.dirname(py) == os.path.dirname(sys.executable)
        assert (buildfile.vlsi / "design.txt").read_text().strip() == "foo"

    def test_buildfile_uses_build_target_when_only_build_exists(self, buildfile) -> None:
        (buildfile.vlsi / "Makefile").write_text("build:\n\ttouch built\nbuild_dir := x\n")
        assert buildfile() == 0
        assert (buildfile.vlsi / "built").exists()

    def test_buildfile_defaults_to_buildfile_when_no_rule_is_visible(self, buildfile) -> None:
        (buildfile.vlsi / "rules.mk").write_text("buildfile:\n\ttouch made\n")
        (buildfile.vlsi / "Makefile").write_text("build := 1\ninclude rules.mk\n")
        assert buildfile() == 0
        assert (buildfile.vlsi / "made").exists()

    def test_buildfile_target_override(self, buildfile) -> None:
        (buildfile.vlsi / "Makefile").write_text("buildfile:\n\tfalse\nother:\n\ttouch other\n")
        assert buildfile("--target", "other") == 0
        assert (buildfile.vlsi / "other").exists()

    def test_buildfile_without_makefile_exits(self, buildfile) -> None:
        with pytest.raises(SystemExit, match="vlsi directory"):
            buildfile()

    def test_buildfile_propagates_make_exit_code(self, buildfile) -> None:
        (buildfile.vlsi / "Makefile").write_text("buildfile:\n\tfalse\n")
        assert buildfile() != 0

    def test_buildfile_drops_conda_ld_library_path(self, buildfile, monkeypatch) -> None:
        monkeypatch.delenv("CONDA_PREFIX", raising=False)
        monkeypatch.setenv("LD_LIBRARY_PATH", os.pathsep.join(["/x/conda/lib", "/usr/lib64"]))
        (buildfile.vlsi / "Makefile").write_text("buildfile:\n\techo $$LD_LIBRARY_PATH > ld.txt\n")
        assert buildfile() == 0
        assert (buildfile.vlsi / "ld.txt").read_text().strip() == "/usr/lib64"

    def test_buildfile_dryrun_prints_command(self, buildfile, monkeypatch, capsys) -> None:
        monkeypatch.setenv("SLEDGE_DRYRUN", "1")
        (buildfile.vlsi / "Makefile").write_text("buildfile:\n\ttouch made\n")
        assert buildfile("-j", "4") == 0
        assert "make buildfile -j 4" in capsys.readouterr().out
        assert not (buildfile.vlsi / "made").exists()

    def test_buildfile_names_the_registered_dag(self, buildfile, capsys) -> None:
        import getpass
        dag = buildfile.vlsi / "build" / "Top" / "hammer_dag.py"
        (buildfile.vlsi / "Makefile").write_text(
            f"buildfile:\n\tmkdir -p $(HAMMER_DAGS_FOLDER) build/Top && "
            f"echo 'OBJ_DIR = \"{dag.parent}\"' > {dag} && "
            f"ln -sfn {dag} $(HAMMER_DAGS_FOLDER)/sledgehammer_Top_{getpass.getuser()}.py\n")
        assert buildfile() == 0
        out = capsys.readouterr().out
        assert f"registered sledgehammer_Top_{getpass.getuser()}" in out
        assert "next: sledgehammer syn" in out

    def _preregister(self, buildfile, design: str) -> None:
        import getpass
        dags = buildfile.vlsi.parent / "dags"
        dags.mkdir(exist_ok=True)
        (dags / f"sledgehammer_{design}_{getpass.getuser()}.py").write_text(
            f'WORK_DIR = "{buildfile.vlsi}"\nOBJ_DIR = "{buildfile.vlsi / "build" / design}"\n')

    def test_buildfile_reports_only_the_dag_it_built(self, buildfile, capsys) -> None:
        import getpass
        self._preregister(buildfile, "gcd")
        dag = buildfile.vlsi / "build" / "pass" / "hammer_dag.py"
        (buildfile.vlsi / "Makefile").write_text(
            f"buildfile:\n\tmkdir -p build/pass && "
            f"printf 'WORK_DIR = \"{buildfile.vlsi}\"\\nOBJ_DIR = \"{dag.parent}\"\\n' > {dag} && "
            f"ln -sfn {dag} $(HAMMER_DAGS_FOLDER)/sledgehammer_pass_{getpass.getuser()}.py\n")
        assert buildfile() == 0
        out = capsys.readouterr().out
        assert f"registered sledgehammer_pass_{getpass.getuser()}" in out
        assert "sledgehammer_gcd_" not in out
        assert "next: sledgehammer syn -t pass" in out

    def test_buildfile_with_nothing_to_do_names_what_is_registered(self, buildfile, capsys) -> None:
        import getpass
        self._preregister(buildfile, "gcd")
        (buildfile.vlsi / "Makefile").write_text("buildfile:\n\ttrue\n")
        assert buildfile() == 0
        out = capsys.readouterr().out
        assert "make generated no DAG; registered for this directory:" in out
        assert f"sledgehammer_gcd_{getpass.getuser()}" in out and "next:" not in out

    def test_buildfile_with_no_dag_at_all_points_at_build_system(self, buildfile, capsys) -> None:
        (buildfile.vlsi / "Makefile").write_text("buildfile:\n\ttrue\n")
        assert buildfile() == 0
        assert "vlsi.core.build_system is sledgehammer" in capsys.readouterr().out

    def test_rebuilding_a_registered_dag_reports_it_again(self, buildfile, capsys) -> None:
        import getpass
        dag = buildfile.vlsi / "build" / "Top" / "hammer_dag.py"
        link = f"$(HAMMER_DAGS_FOLDER)/sledgehammer_Top_{getpass.getuser()}.py"
        # Relinks the way the real linker does (remove, then symlink), which can reuse the link's inode.
        (buildfile.vlsi / "Makefile").write_text(
            f"buildfile:\n\tmkdir -p $(HAMMER_DAGS_FOLDER) build/Top && "
            f"echo 'OBJ_DIR = \"{dag.parent}\"' > {dag} && rm -f {link} && ln -s {dag} {link}\n")
        assert buildfile() == 0
        capsys.readouterr()
        assert buildfile() == 0
        assert f"registered sledgehammer_Top_{getpass.getuser()}" in capsys.readouterr().out

    def test_buildfile_reads_a_non_utf8_makefile(self, buildfile) -> None:
        (buildfile.vlsi / "Makefile").write_bytes(b"# caf\xe9\nbuild:\n\ttouch built\n")
        assert buildfile() == 0
        assert (buildfile.vlsi / "built").exists()
