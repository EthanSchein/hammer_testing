import configparser
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_the_dag_processor_parses_with_one_process_every_two_minutes() -> None:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(REPO / "airflow.cfg")
    assert cfg.getint("dag_processor", "parsing_processes") == 1
    assert cfg.getint("dag_processor", "min_file_process_interval") == 120


def test_a_reparsed_dag_is_stored_at_once() -> None:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(REPO / "airflow.cfg")
    assert cfg.getint("core", "min_serialized_dag_update_interval") == 0
