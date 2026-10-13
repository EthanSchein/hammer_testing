import importlib.util
import sys
import types

import pytest

from tests.utils import fake_postgres


def _views(monkeypatch, tmp_path, **env):
    """The pgAdmin plugin's nav items, loaded with nothing listening on the pgAdmin ports."""
    monkeypatch.setitem(sys.modules, "airflow", types.ModuleType("airflow"))
    monkeypatch.setitem(sys.modules, "airflow.plugins_manager",
                        types.SimpleNamespace(AirflowPlugin=type("AirflowPlugin", (), {})))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PGADMIN_PORT", str(fake_postgres.free_port()))
    for key in ("SLEDGE_LITE", "PGADMIN_URL"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    spec = importlib.util.spec_from_file_location("pgadmin_link_under_test",
                                                  str(fake_postgres.REPO / "plugins" / "pgadmin_link.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PgAdminLinkPlugin.external_views


def test_the_lite_stack_shows_no_link_to_a_pgadmin_that_is_not_there(monkeypatch, tmp_path) -> None:
    assert _views(monkeypatch, tmp_path, SLEDGE_LITE="1") == []


def test_the_lite_stack_keeps_an_explicit_pgadmin_url(monkeypatch, tmp_path) -> None:
    views = _views(monkeypatch, tmp_path, SLEDGE_LITE="1", PGADMIN_URL="http://db.example:5050/browser/")
    assert [v["href"] for v in views] == ["http://db.example:5050/browser/"]


@pytest.mark.parametrize("lite", ["", "0"])
def test_the_full_stack_always_shows_the_link(monkeypatch, tmp_path, lite) -> None:
    assert len(_views(monkeypatch, tmp_path, SLEDGE_LITE=lite)) == 1
