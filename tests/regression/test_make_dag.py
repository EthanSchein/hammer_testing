import os
import sys
from pathlib import Path

from hammer.shell import pd_store_cli

DRIVER = """\
#!/usr/bin/env python3
from hammer.vlsi import CLIDriver

if __name__ == "__main__":
    CLIDriver().main()
"""


def _repo(tmp_path: Path) -> Path:
    e2e = tmp_path / "repo" / "e2e"
    files = {
        "configs-env/x-env.yml": f'synthesis.mocksynth.temp_folder: "{tmp_path / "mock"}"\n',
        "configs-pdk/nop.yml": 'vlsi.core.technology: "hammer.technology.nop"\n',
        "configs-tool/nop.yml": 'vlsi.core.synthesis_tool: "hammer.synthesis.mocksynth"\n',
        "configs-design/d/common.yml": 'synthesis.inputs.top_module: "top"\n'
                                       'synthesis.inputs.input_files: ["src/top.v"]\n',
        "src/top.v": "module top; endmodule\n",
        "example-driver": DRIVER,
    }
    for rel, text in files.items():
        (e2e / rel).parent.mkdir(parents=True, exist_ok=True)
        (e2e / rel).write_text(text)
    (tmp_path / "mock").mkdir()
    return e2e.parent


def _make_dag(tmp_path: Path, repo: Path, *extra: str) -> int:
    return pd_store_cli.main([
        "make-dag", "--design", "d", "--repo-root", str(repo),
        "--design-dir", str(repo / "e2e" / "configs-design" / "d"),
        "--pdk", "nop", "--tools", "nop", "--env", "x", "--skip-augment",
        "--dags-folder", str(tmp_path / "dags"), *extra,
    ])


def test_dag_runs_the_driver_from_e2e(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    argv0 = sys.argv[0]
    assert _make_dag(tmp_path, repo) == 0
    assert sys.argv[0] == argv0 and os.getcwd() == str(tmp_path)
    e2e = os.path.realpath(repo / "e2e")
    driver = os.path.join(e2e, "example-driver")
    obj_dir = repo / "e2e" / "build-nop-nop" / "d"
    dag = (obj_dir / "hammer_dag.py").read_text()
    assert f'HAMMER_EXEC = "{driver}"' in dag
    assert f'WORK_DIR = "{e2e}"' in dag
    assert f"HAMMER_EXEC ?= {driver}" in (obj_dir / "hammer.d").read_text()


def test_explicit_driver(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    mine = tmp_path / "my-driver"
    mine.write_text(DRIVER)
    monkeypatch.chdir(tmp_path)
    assert _make_dag(tmp_path, repo, "--driver", "my-driver") == 0
    dag = (repo / "e2e" / "build-nop-nop" / "d" / "hammer_dag.py").read_text()
    assert f'HAMMER_EXEC = "{os.path.realpath(mine)}"' in dag


def test_missing_driver_fails_before_generating(tmp_path, monkeypatch, capsys) -> None:
    repo = _repo(tmp_path)
    (repo / "e2e" / "example-driver").unlink()
    monkeypatch.chdir(tmp_path)
    assert _make_dag(tmp_path, repo) == 2
    assert "--driver" in capsys.readouterr().err
    assert not (repo / "e2e" / "build-nop-nop").exists()
