"""`sledgehammer status` and `runs` ask about each DAG once, even when `airflow dags list` repeats it."""
import pytest

from hammer.shell import sledgehammer_cli

DAGS = ("dag_id\n" + "sledgehammer_build_ab\n" * 7 + "sledgehammer_core_ab\n" * 11 + "sledgehammer_build_abc\n")


@pytest.fixture
def airflow(monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "ab")
    asked = []

    def run(*a, capture=True):
        if a[:2] == ("dags", "list"):
            return 0, DAGS, ""
        asked.append(a[2])
        return 0, f"dag_id run_id state\n{a[2]} manual__1 success\n", ""

    states = []
    monkeypatch.setattr(sledgehammer_cli, "_airflow", run)
    monkeypatch.setattr(sledgehammer_cli, "_task_states",
                        lambda dag_id, run_id: states.append(dag_id) or {"module_syn": "success"})
    return asked, states


def test_status_reports_each_dag_once(airflow, capsys) -> None:
    asked, states = airflow
    assert sledgehammer_cli._cmd_status([]) == 0
    assert asked == states == ["sledgehammer_build_ab", "sledgehammer_core_ab"]
    out = capsys.readouterr().out
    assert out.count("sledgehammer_build_ab  run manual__1: success") == 1
    assert out.count("sledgehammer_core_ab  run manual__1: success") == 1


def test_runs_lists_each_dag_once(airflow, capsys) -> None:
    asked, states = airflow
    assert sledgehammer_cli._cmd_status([], list_only=True) == 0
    assert asked == ["sledgehammer_build_ab", "sledgehammer_core_ab"] and states == []
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == ["build", "core"]
