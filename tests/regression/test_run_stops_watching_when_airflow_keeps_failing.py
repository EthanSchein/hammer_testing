import getpass

import pytest

from hammer.shell import sledgehammer_cli

DOWN = (1, "", "Traceback (most recent call last):\n  File \"airflow/utils/session.py\", line 100, in wrapper\n"
              "sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) connection to server at \"db\" "
              "(10.0.0.5), port 5432 failed: Connection refused\n\tIs the server running on that host?\n\n"
              "(Background on this error at: https://sqlalche.me/e/20/e3q8)\n")


def listed(state):
    return 0, f"dag_id run_id state\nsledgehammer_Top_{getpass.getuser()} cli_1 {state}\n", ""


@pytest.fixture
def watch(tmp_path, monkeypatch):
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / f"sledgehammer_Top_{getpass.getuser()}.py").write_text("'forceall': Param(default=False)")
    sleeps = []
    monkeypatch.setattr(sledgehammer_cli, "_dags_folder", lambda: (str(dags), "test"))
    monkeypatch.setattr(sledgehammer_cli.time, "sleep", sleeps.append)

    def run(answers):
        answers = iter(answers)

        def fake_airflow(*a, capture=True):
            if a[:2] == ("dags", "list-runs"):
                return next(answers)
            if a[:2] == ("tasks", "states-for-dag-run"):
                return (0, "[]", "")
            return 0, "", ""
        monkeypatch.setattr(sledgehammer_cli, "_airflow", fake_airflow)
        return sledgehammer_cli._cmd_run(["par", "--obj_dir", str(tmp_path / "Top"), "--run-id", "cli_1"])
    run.sleeps = sleeps
    return run


def test_airflow_that_keeps_failing_stops_the_watch_with_its_error(watch, capsys) -> None:
    assert watch([DOWN] * 10) == 1
    assert len(watch.sleeps) == 9
    out, err = capsys.readouterr()
    assert out.count("trying again every 30s") == 1
    assert ("stopped watching run cli_1 after 10 tries in a row: airflow could not list the runs: "
            "sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) connection to server at \"db\" "
            "(10.0.0.5), port 5432 failed: Connection refused") in err
    assert "sledgehammer status --design Top" in err


def test_a_run_airflow_no_longer_lists_stops_the_watch(watch, capsys) -> None:
    assert watch([(0, "dag_id run_id state\n", "")] * 10) == 1
    assert "airflow does not list run cli_1" in capsys.readouterr().err


def test_failures_that_clear_up_do_not_add_up(watch, capsys) -> None:
    assert watch([DOWN] * 9 + [listed("running")] + [DOWN] * 9 + [listed("success")]) == 0
    out, _ = capsys.readouterr()
    assert out.count("trying again every 30s") == 2 and "run cli_1: SUCCESS" in out


def test_a_failed_run_still_reports_failed(watch, capsys) -> None:
    assert watch([listed("queued"), listed("running"), listed("failed")]) == 1
    assert "run cli_1: FAILED" in capsys.readouterr().out
