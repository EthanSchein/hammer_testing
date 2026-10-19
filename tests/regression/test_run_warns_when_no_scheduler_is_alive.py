import getpass

import pytest

from hammer.shell import sledgehammer_cli

ALIVE = (0, "Found one alive job.\n", "")
NONE_ALIVE = (1, "", "No alive jobs found.\n")
DB_DOWN = (1, "", "Traceback (most recent call last):\n  File \"airflow/utils/session.py\", line 100, in wrapper\n"
                  "sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) connection to server at \"db\" "
                  "(10.0.0.5), port 5432 failed: Connection refused\n")
LIST_DOWN = (1, "", "sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) connection refused\n")

WARNING = ("[sledgehammer] no Airflow scheduler has been alive for over a minute, so run cli_1 starts no new stage.\n"
           "  A stage that is already running keeps going.\n"
           "  The lite stack restarts a stuck or crashed scheduler by itself.\n"
           "  If the stack is not running, start it with:  sledgehammer\n"
           "  If the run does not recover, stop it and free its directory with:  sledgehammer cancel cli_1\n"
           "  Still watching; Ctrl-C here only detaches.\n")
BACK = "[sledgehammer] an Airflow scheduler is alive again, so run cli_1 can move on\n"


def listed(state):
    return 0, f"dag_id run_id state\nsledgehammer_Top_{getpass.getuser()} cli_1 {state}\n", ""


def running_then_success(polls):
    return [listed("running")] * polls + [listed("success")]


@pytest.fixture
def watch(tmp_path, monkeypatch):
    """Runs `sledgehammer run par` against a fake airflow; watch.checked lists the polls that looked for a scheduler."""
    dags = tmp_path / "dags"
    dags.mkdir()
    (dags / f"sledgehammer_Top_{getpass.getuser()}.py").write_text("'forceall': Param(default=False)")
    monkeypatch.setattr(sledgehammer_cli, "_dags_folder", lambda: (str(dags), "test"))
    monkeypatch.setattr(sledgehammer_cli.time, "sleep", lambda s: None)
    checked = []

    def run(runs, schedulers):
        runs, schedulers = iter(runs), iter(schedulers)
        polls = []

        def fake_airflow(*a, capture=True):
            if a[:2] == ("dags", "list-runs"):
                polls.append(a)
                return next(runs)
            if a[:2] == ("jobs", "check"):
                assert a == ("jobs", "check", "--job-type", "SchedulerJob")
                checked.append(len(polls))
                return next(schedulers)
            if a[:2] == ("tasks", "states-for-dag-run"):
                return 0, "[]", ""
            return 0, "", ""
        monkeypatch.setattr(sledgehammer_cli, "_airflow", fake_airflow)
        return sledgehammer_cli._cmd_run(["par", "--obj_dir", str(tmp_path / "Top"), "--run-id", "cli_1"])
    run.checked = checked
    return run


def test_a_live_scheduler_is_checked_on_the_first_poll_then_every_fourth(watch, capsys) -> None:
    assert watch(running_then_success(9), [ALIVE] * 3) == 0
    assert watch.checked == [1, 5, 9]
    out = capsys.readouterr().out
    assert "starts no new stage" not in out and "alive again" not in out


def test_three_polls_without_a_scheduler_warn_once_and_keep_watching(watch, capsys) -> None:
    assert watch(running_then_success(8), [NONE_ALIVE] * 8) == 0
    assert watch.checked == [1, 2, 3, 4, 5, 6, 7, 8]
    out, err = capsys.readouterr()
    assert out.count(WARNING) == 1 and out.endswith(WARNING + "[sledgehammer] run cli_1: SUCCESS\n")
    assert "stopped watching" not in out + err


def test_the_third_poll_without_a_scheduler_is_the_one_that_warns(watch, capsys) -> None:
    assert watch(running_then_success(3), [NONE_ALIVE] * 3) == 0
    assert capsys.readouterr().out.endswith(WARNING + "[sledgehammer] run cli_1: SUCCESS\n")


def test_a_short_scheduler_restart_is_not_reported(watch, capsys) -> None:
    assert watch(running_then_success(6), [NONE_ALIVE] * 2 + [ALIVE]) == 0
    assert watch.checked == [1, 2, 3]
    out = capsys.readouterr().out
    assert "starts no new stage" not in out and "alive again" not in out


def test_a_scheduler_that_comes_back_is_reported_and_checked_less_often_again(watch, capsys) -> None:
    assert watch(running_then_success(10), [NONE_ALIVE] * 3 + [ALIVE] * 2) == 0
    assert watch.checked == [1, 2, 3, 4, 8]
    out = capsys.readouterr().out
    assert out.count(WARNING) == 1 and out.count(BACK) == 1 and out.index(WARNING) < out.index(BACK)


def test_a_scheduler_lost_again_after_coming_back_is_reported_again(watch, capsys) -> None:
    assert watch(running_then_success(10), [NONE_ALIVE] * 3 + [ALIVE] + [NONE_ALIVE] * 3) == 0
    assert watch.checked == [1, 2, 3, 4, 8, 9, 10]
    out = capsys.readouterr().out
    assert out.count(WARNING) == 2 and out.count(BACK) == 1


def test_an_unreachable_database_is_not_taken_for_a_missing_scheduler(watch, capsys) -> None:
    assert watch(running_then_success(6), [NONE_ALIVE] + [DB_DOWN] * 5) == 0
    assert watch.checked == [1, 2, 3, 4, 5, 6]
    assert "starts no new stage" not in capsys.readouterr().out


def test_polls_that_cannot_list_the_run_do_not_check(watch, capsys) -> None:
    assert watch([LIST_DOWN] * 2 + running_then_success(5), [ALIVE] * 2) == 0
    assert watch.checked == [3, 7]
    assert capsys.readouterr().out.count("trying again every 30s") == 1


def test_the_watch_still_stops_after_ten_polls_that_cannot_list_the_run(watch, capsys) -> None:
    assert watch([listed("running")] * 3 + [LIST_DOWN] * 10, [NONE_ALIVE] * 3) == 1
    assert watch.checked == [1, 2, 3]
    out, err = capsys.readouterr()
    assert out.count(WARNING) == 1
    assert "stopped watching run cli_1 after 10 tries in a row: airflow could not list the runs" in err
