import os
import sys
from types import SimpleNamespace

import pytest

from hammer.shell import dags_folder, pd_store_cli, sledgehammer_cli
from hammer.vlsi.hammer_build_systems import _link_dag_into_dags_folder

_VARS = ("HAMMER_DAGS_FOLDER", "AIRFLOW__CORE__DAGS_FOLDER", "AIRFLOW_CONFIG", "AIRFLOW_HOME")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "home"))


def _cfg(path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"[core]\ndags_folder = {value}\n")


class TestResolve:
    def test_order(self, tmp_path, monkeypatch) -> None:
        home = tmp_path / "home"
        assert dags_folder.resolve_dags_folder() == (str(home / "dags"), "$AIRFLOW_HOME/dags")
        _cfg(home / "airflow.cfg", "/from/cfg")
        assert dags_folder.resolve_dags_folder()[0] == "/from/cfg"
        monkeypatch.setenv("AIRFLOW__CORE__DAGS_FOLDER", "/from/airflow/env")
        assert dags_folder.resolve_dags_folder() == ("/from/airflow/env", "$AIRFLOW__CORE__DAGS_FOLDER")
        monkeypatch.setenv("HAMMER_DAGS_FOLDER", "/from/hammer/env")
        assert dags_folder.resolve_dags_folder() == ("/from/hammer/env", "$HAMMER_DAGS_FOLDER")

    def test_cfg_brace_placeholder_stays_literal_like_airflow(self, tmp_path) -> None:
        _cfg(tmp_path / "home" / "airflow.cfg", "{AIRFLOW_HOME}/my-dags")
        assert dags_folder.resolve_dags_folder()[0] == "{AIRFLOW_HOME}/my-dags"

    def test_airflow_config_points_at_another_file(self, tmp_path, monkeypatch) -> None:
        _cfg(tmp_path / "other.cfg", "/from/other")
        monkeypatch.setenv("AIRFLOW_CONFIG", str(tmp_path / "other.cfg"))
        assert dags_folder.resolve_dags_folder()[0] == "/from/other"

    @pytest.mark.parametrize("value,expected", [("%(root)s/dags", "/sched/dags"), ("/proj/100%%/dags", "/proj/100%/dags")])
    def test_cfg_interpolation_matches_airflow(self, tmp_path, value, expected) -> None:
        (tmp_path / "home").mkdir()
        (tmp_path / "home" / "airflow.cfg").write_text(f"[DEFAULT]\nroot = /sched\n[core]\ndags_folder = {value}\n")
        assert dags_folder.resolve_dags_folder()[0] == expected

    def test_default_is_the_checkout_when_airflow_home_is_unset(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("AIRFLOW_HOME")
        monkeypatch.setattr(dags_folder, "REPO", str(tmp_path / "checkout"))
        assert dags_folder.resolve_dags_folder()[0] == str(tmp_path / "checkout" / "dags")


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
def test_linker_and_run_agree_on_the_airflow_env_var(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIRFLOW__CORE__DAGS_FOLDER", str(tmp_path / "scheduler-dags"))
    (tmp_path / "obj").mkdir()
    (tmp_path / "obj" / "hammer_dag.py").write_text("")
    driver = SimpleNamespace(database=SimpleNamespace(get_setting=lambda key: None))
    _link_dag_into_dags_folder(driver, str(tmp_path / "obj" / "hammer_dag.py"), "sledgehammer_x_u")
    assert os.path.islink(tmp_path / "scheduler-dags" / "sledgehammer_x_u.py")
    monkeypatch.setattr(sledgehammer_cli, "_airflow", lambda *a, **k: pytest.fail("ran the airflow CLI"))
    assert sledgehammer_cli._dags_folder()[0] == str(tmp_path / "scheduler-dags")


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
def test_make_dag_links_where_run_looks_not_into_the_cwd(tmp_path, monkeypatch) -> None:
    e2e = tmp_path / "repo" / "e2e"
    files = {
        "configs-env/x-env.yml": f'synthesis.mocksynth.temp_folder: "{tmp_path / "mock"}"\n',
        "configs-pdk/nop.yml": 'vlsi.core.technology: "hammer.technology.nop"\n',
        "configs-tool/nop.yml": 'vlsi.core.synthesis_tool: "hammer.synthesis.mocksynth"\n',
        "configs-design/d/common.yml": 'synthesis.inputs.top_module: "top"\n'
                                       'synthesis.inputs.input_files: ["src/top.v"]\n',
        "src/top.v": "module top; endmodule\n",
        "example-driver": "#!/usr/bin/env python3\n",
    }
    for rel, text in files.items():
        (e2e / rel).parent.mkdir(parents=True, exist_ok=True)
        (e2e / rel).write_text(text)
    (tmp_path / "mock").mkdir()
    monkeypatch.setenv("AIRFLOW__CORE__DAGS_FOLDER", str(tmp_path / "scheduler-dags"))
    monkeypatch.chdir(tmp_path / "repo")
    assert pd_store_cli.main([
        "make-dag", "--design", "d", "--pdk", "nop", "--tools", "nop", "--env", "x", "--skip-augment",
        "--design-dir", str(e2e / "configs-design" / "d"),
    ]) == 0
    linked = [p.name for p in (tmp_path / "scheduler-dags").iterdir()]
    assert len(linked) == 1 and linked[0].startswith("sledgehammer_d_")
    assert not (tmp_path / "repo" / "dags").exists()


class TestExpansion:
    def test_env_values_expand_like_airflow(self, monkeypatch) -> None:
        monkeypatch.setenv("HOME", "/home/u")
        monkeypatch.setenv("AIRFLOW__CORE__DAGS_FOLDER", "~/dags")
        assert dags_folder.resolve_dags_folder()[0] == "/home/u/dags"

    def test_cfg_values_expand_vars_and_airflow_home(self, tmp_path, monkeypatch) -> None:
        home = tmp_path / "home"
        monkeypatch.setenv("SITE", "/srv/site")
        for value, want in (("${AIRFLOW_HOME}/a", f"{home}/a"), ("$AIRFLOW_HOME/b", f"{home}/b"),
                            ("$SITE/dags", "/srv/site/dags")):
            _cfg(home / "airflow.cfg", value)
            assert dags_folder.resolve_dags_folder()[0] == want


class TestExpansionLikeAirflow:
    def test_a_longer_variable_name_is_not_airflow_home(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("AIRFLOW_HOME_BASE", "/srv/base")
        _cfg(tmp_path / "home" / "airflow.cfg", "$AIRFLOW_HOME_BASE/dags")
        assert dags_folder.resolve_dags_folder()[0] == "/srv/base/dags"

    def test_airflow_home_and_config_expand_variables(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("BASE", str(tmp_path))
        monkeypatch.setenv("AIRFLOW_HOME", "$BASE/home")
        assert dags_folder.resolve_dags_folder()[0] == str(tmp_path / "home" / "dags")
        _cfg(tmp_path / "prod.cfg", "/from/prod")
        monkeypatch.setenv("AIRFLOW_CONFIG", "$BASE/prod.cfg")
        assert dags_folder.resolve_dags_folder()[0] == "/from/prod"

    def test_nested_variables_expand_until_stable(self, monkeypatch) -> None:
        monkeypatch.setenv("INNER", "/srv/inner")
        monkeypatch.setenv("OUTER", "$INNER/dags")
        monkeypatch.setenv("AIRFLOW__CORE__DAGS_FOLDER", "$OUTER")
        assert dags_folder.resolve_dags_folder()[0] == "/srv/inner/dags"
