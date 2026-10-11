import os

import pytest

from hammer.vlsi import pd_store

CONN = "postgresql+psycopg2://alice:s%40cret@db.example.edu:5544/airflow_db"
PG_VARS = ("HAMMER_PG_HOST", "HAMMER_PG_PORT", "HAMMER_PG_DB", "HAMMER_PG_USER", "HAMMER_PG_PASSWORD",
           "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    monkeypatch.setattr(pd_store, "_parse_airflow_cfg_conn", lambda: {})
    for var in PG_VARS:
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)


def test_settings_follow_the_airflow_conn_in_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", CONN)
    s = pd_store._pg_settings()
    assert (s["host"], s["port"], s["user"], s["password"]) == ("db.example.edu", 5544, "alice", "s@cret")
    assert s["dbname"] == "sledgehammer_studio"


def test_hammer_pg_vars_win_over_the_airflow_conn(monkeypatch) -> None:
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", CONN)
    monkeypatch.setenv("HAMMER_PG_HOST", "cache.example.edu")
    monkeypatch.setenv("HAMMER_PG_PASSWORD", "other")
    s = pd_store._pg_settings()
    assert (s["host"], s["port"], s["password"]) == ("cache.example.edu", 5544, "other")


def test_a_sqlite_task_conn_falls_through_to_airflow_cfg(monkeypatch) -> None:
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "sqlite:////tmp/decoy.db")
    monkeypatch.setattr(pd_store, "_parse_airflow_cfg_conn", lambda: {"host": "cfg.example.edu", "password": "x"})
    assert pd_store._pg_settings()["host"] == "cfg.example.edu"


def test_export_fills_only_unset_vars(monkeypatch) -> None:
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", CONN)
    monkeypatch.setenv("HAMMER_PG_USER", "bob")
    pd_store.export_conn_defaults()
    assert os.environ["HAMMER_PG_HOST"] == "db.example.edu"
    assert os.environ["HAMMER_PG_PORT"] == "5544"
    assert os.environ["HAMMER_PG_USER"] == "bob"
    assert "HAMMER_PG_PASSWORD" not in os.environ


def test_export_without_a_postgres_conn_sets_nothing(monkeypatch) -> None:
    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "sqlite:////tmp/decoy.db")
    pd_store.export_conn_defaults()
    assert not any(os.environ.get(v) for v in ("HAMMER_PG_HOST", "HAMMER_PG_PORT", "HAMMER_PG_USER"))
