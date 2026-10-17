import types

import pytest

from hammer.vlsi import pd_store


class OperationalError(Exception):
    pass


@pytest.fixture
def driver(monkeypatch):
    for var in ("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "HAMMER_PG_STATEMENT_TIMEOUT", "PGOPTIONS",
                "HAMMER_PG_CONNECT_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HAMMER_PG_HOST", "db.example.edu")
    monkeypatch.setenv("HAMMER_PG_PORT", "5432")
    monkeypatch.setenv("HAMMER_PG_USER", "t1a")
    monkeypatch.setenv("HAMMER_PG_DB", "team1_cache")
    monkeypatch.setenv("HAMMER_PG_PASSWORD", "pw")
    fake = types.SimpleNamespace(calls=[], down=set(), busy=set(), OperationalError=OperationalError, Error=Exception)

    def connect(**kw):
        fake.calls.append(kw)
        if kw["host"] in fake.down:
            clock[0] += kw["connect_timeout"]
            raise OperationalError(f'connection to server at "{kw["host"]}", port {kw["port"]} failed: '
                                   "timeout expired\n")
        if kw["host"] in fake.busy:
            raise OperationalError(f'connection to server at "{kw["host"]}", port {kw["port"]} failed: FATAL:  '
                                   'too many connections for role "t1a"\n')
        return object()

    fake.connect = connect
    monkeypatch.setattr(pd_store, "psycopg2", fake)
    clock = [1000.0]
    monkeypatch.setattr(pd_store, "_now", lambda: clock[0])
    fake.clock = clock
    return fake


def test_connections_keep_alive_and_bound_each_statement(driver, monkeypatch) -> None:
    pd_store._connect()
    kw = driver.calls[-1]
    keepalives = (kw["keepalives"], kw["keepalives_idle"], kw["keepalives_interval"], kw["keepalives_count"])
    assert keepalives == (1, 30, 10, 3)
    assert kw["options"] == "-c statement_timeout=600000"
    assert kw["connect_timeout"] == 10
    monkeypatch.setenv("HAMMER_PG_STATEMENT_TIMEOUT", "90")
    monkeypatch.setenv("PGOPTIONS", "-c search_path=hammer_poc")
    pd_store._connect()
    assert driver.calls[-1]["options"] == "-c search_path=hammer_poc -c statement_timeout=90000"
    monkeypatch.setenv("HAMMER_PG_STATEMENT_TIMEOUT", "0")
    monkeypatch.delenv("PGOPTIONS")
    pd_store._connect()
    assert "options" not in driver.calls[-1]


@pytest.mark.parametrize("pgoptions", ["-c statement_timeout=0", "-c statement-timeout=5min", "--Statement_Timeout=0"])
def test_a_statement_timeout_in_pgoptions_is_left_alone(driver, monkeypatch, pgoptions) -> None:
    monkeypatch.setenv("PGOPTIONS", pgoptions)
    pd_store._connect()
    assert "options" not in driver.calls[-1]


@pytest.mark.parametrize("value, ms", [("inf", 2147482000), ("1e12", 2147482000), ("nan", 600000), ("soon", 600000),
                                       ("0.5", 1000), ("-3", None)])
def test_odd_statement_timeouts_never_break_the_connect(driver, monkeypatch, value, ms) -> None:
    monkeypatch.setenv("HAMMER_PG_STATEMENT_TIMEOUT", value)
    pd_store._connect()
    assert driver.calls[-1].get("options") == (None if ms is None else f"-c statement_timeout={ms}")


def test_a_server_that_is_down_costs_one_timeout_a_minute(driver) -> None:
    driver.down.add("db.example.edu")
    with pytest.raises(OperationalError):
        pd_store._connect()
    assert len(driver.calls) == 1
    driver.clock[0] += 30
    for _ in range(5):
        with pytest.raises(OperationalError) as raised:
            pd_store._connect()
    assert len(driver.calls) == 1
    assert str(raised.value) == ('the cache database at db.example.edu:5432 did not answer (connection to server at '
                                 '"db.example.edu", port 5432 failed: timeout expired); trying again after 60s')
    driver.clock[0] += 31
    driver.down.clear()
    pd_store._connect()
    assert len(driver.calls) == 2
    pd_store._connect()
    assert len(driver.calls) == 3


def test_a_quick_refusal_is_tried_again_at_once(driver) -> None:
    driver.busy.add("db.example.edu")
    for _ in range(3):
        with pytest.raises(OperationalError, match="too many connections"):
            pd_store._connect()
    assert len(driver.calls) == 3
    driver.busy.clear()
    pd_store._connect()
    assert len(driver.calls) == 4


def test_another_server_is_still_tried(driver, monkeypatch) -> None:
    driver.down.add("db.example.edu")
    with pytest.raises(OperationalError):
        pd_store._connect()
    monkeypatch.setenv("HAMMER_PG_HOST", "db2.example.edu")
    pd_store._connect()
    assert [c["host"] for c in driver.calls] == ["db.example.edu", "db2.example.edu"]
