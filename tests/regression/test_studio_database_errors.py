from types import SimpleNamespace

import pytest

from hammer.shell import pd_store_cli
from hammer.vlsi import pd_store, time_tracking


@pytest.fixture
def no_driver(monkeypatch):
    monkeypatch.setattr(pd_store, "psycopg2", None)


def test_connect_timeout(monkeypatch) -> None:
    monkeypatch.setenv("HAMMER_PG_PASSWORD", "x")
    monkeypatch.delenv("HAMMER_PG_CONNECT_TIMEOUT", raising=False)
    assert pd_store._pg_settings()["connect_timeout"] == 10
    monkeypatch.setenv("HAMMER_PG_CONNECT_TIMEOUT", "3")
    assert pd_store._pg_settings()["connect_timeout"] == 3
    for value, seconds in (("1.5", 2), ("0.2", 1), ("soon", 10), ("0", 0), ("-4", 0), ("nan", 10),
                           ("2147483647", 2147483647), ("2147483648", 0), ("1e10", 0), ("inf", 0)):
        monkeypatch.setenv("HAMMER_PG_CONNECT_TIMEOUT", value)
        assert pd_store._pg_settings()["connect_timeout"] == seconds


@pytest.mark.parametrize("call", [
    lambda: pd_store.delete_stage_blobs(),
    lambda: pd_store.delete_master_databases(),
    lambda: pd_store.list_user_workspaces(),
    lambda: pd_store.delete_blobs(stage="par"),
])
def test_direct_callers_go_through_connect(no_driver, call) -> None:
    with pytest.raises(pd_store.DatabaseUnavailable, match="psycopg2 is not installed"):
        call()


def test_missing_password_is_database_unavailable(monkeypatch, tmp_path) -> None:
    for var in ("HAMMER_PG_PASSWORD", "PGPASSWORD", "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "none"))
    monkeypatch.setenv("SLEDGE_PG_DIR", str(tmp_path / "no-postgres"))
    monkeypatch.setattr(pd_store, "_parse_airflow_cfg_conn", lambda: {})
    with pytest.raises(pd_store.DatabaseUnavailable):
        pd_store._pg_settings()


def test_studio_reports_a_down_database_in_one_line(no_driver, capsys) -> None:
    assert pd_store_cli.main(["workspace-list"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("studio: database unavailable: psycopg2 is not installed")
    assert "Traceback" not in err


def test_cache_status_exits_nonzero_when_unreachable(no_driver, capsys) -> None:
    assert pd_store_cli.main(["cache-status"]) == 1
    assert "unreachable" in capsys.readouterr().out


@pytest.mark.parametrize("source", ["auto", "both"])
def test_time_saved_says_the_db_was_down(no_driver, tmp_path, capsys, source) -> None:
    assert pd_store_cli.main(["time-saved", "--source", source, "--events-dir", str(tmp_path)]) == 1
    assert "DB unavailable" in capsys.readouterr().err


def test_both_keeps_the_db_error_in_its_label(no_driver, tmp_path) -> None:
    events, label = time_tracking.collect_savings_events(source="both", events_dir=str(tmp_path))
    assert events == [] and "DB unavailable" in label


@pytest.fixture
def fake_driver(monkeypatch):
    class OperationalError(Exception):
        pass

    monkeypatch.setattr(pd_store, "psycopg2", SimpleNamespace(OperationalError=OperationalError))
    return OperationalError


def test_every_line_of_a_connection_error_is_shown(fake_driver, monkeypatch, capsys) -> None:
    def down(*a, **kw):
        raise fake_driver("could not connect to server: Connection refused\n\tIs the server running on host?\n")

    monkeypatch.setattr(pd_store, "list_user_workspaces", down)
    assert pd_store_cli.main(["workspace-list"]) == 1
    err = capsys.readouterr().err.splitlines()
    assert err[0] == "studio: database unavailable: could not connect to server: Connection refused"
    assert err[1] == "  Is the server running on host?"


def test_other_errors_are_not_called_unavailable(fake_driver, monkeypatch) -> None:
    def broken(*a, **kw):
        raise ValueError("a bug, not a down database")

    monkeypatch.setattr(pd_store, "list_user_workspaces", broken)
    with pytest.raises(ValueError):
        pd_store_cli.main(["workspace-list"])


def test_a_server_side_error_keeps_its_traceback(fake_driver, monkeypatch) -> None:
    def timed_out(*a, **kw):
        e = fake_driver("canceling statement due to statement timeout")
        e.pgcode = "57014"
        raise e

    monkeypatch.setattr(pd_store, "list_user_workspaces", timed_out)
    with pytest.raises(fake_driver):
        pd_store_cli.main(["workspace-list"])
