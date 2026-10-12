from pathlib import Path

import pytest

from hammer.shell import pd_store_cli
from hammer.vlsi import hammer_build_systems


@pytest.fixture
def dags(monkeypatch):
    """Record build_airflow_dag calls instead of writing a DAG."""
    calls = []
    monkeypatch.setattr(hammer_build_systems, "build_airflow_dag", lambda driver, errs, **kw: calls.append(kw) or {})
    return calls


def _make_dag(tmp_path: Path, build_system: str) -> int:
    e2e = tmp_path / "repo" / "e2e"
    for d in ("configs-env", "configs-pdk", "configs-tool"):
        (e2e / d).mkdir(parents=True)
    (e2e / "configs-env" / "t-env.yml").write_text("{}\n")
    (e2e / "configs-pdk" / "nop.yml").write_text("vlsi.core.technology: hammer.technology.nop\n")
    (e2e / "configs-tool" / "mock.yml").write_text("vlsi.core.synthesis_tool: hammer.synthesis.mocksynth\n")
    (e2e / "example-driver").write_text("#!/usr/bin/env python3\n")
    design = tmp_path / "design"
    design.mkdir()
    (design / "common.yml").write_text(f"vlsi.core.build_system: {build_system}\n"
                                       "vlsi.inputs.hierarchical.config_source: none\n"
                                       "synthesis.inputs.top_module: dummy\n")
    return pd_store_cli.main(["make-dag", "--design", "dummy", "--design-dir", str(design),
                              "--repo-root", str(tmp_path / "repo"), "--obj-dir", str(tmp_path / "obj"),
                              "--dags-folder", str(tmp_path / "dags"), "--pdk", "nop", "--tools", "mock",
                              "--env", "t", "--skip-augment"])


def test_make_dag_generates_no_dag_for_a_hammer_design(tmp_path, capsys, dags) -> None:
    assert _make_dag(tmp_path, "hammer") == 2
    err = capsys.readouterr().err
    assert "vlsi.core.build_system is hammer" in err and "make" in err
    assert dags == []
    assert not (tmp_path / "obj" / "hammer.log").exists()  # refused before a driver was built


@pytest.mark.parametrize("build_system", ["make", "sledgehammer", "airflow"])
def test_make_dag_still_generates_the_dag_for_other_build_systems(tmp_path, capsys, dags, build_system) -> None:
    assert _make_dag(tmp_path, build_system) == 0
    assert "vlsi.core.build_system is hammer" not in capsys.readouterr().err
    assert len(dags) == 1
