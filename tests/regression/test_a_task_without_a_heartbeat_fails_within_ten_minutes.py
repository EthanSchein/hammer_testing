import configparser
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_the_scheduler_fails_a_task_ten_minutes_after_its_last_heartbeat() -> None:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(REPO / "airflow.cfg")
    assert cfg.getint("scheduler", "task_instance_heartbeat_timeout") == 600
    assert cfg.getint("workers", "max_failed_heartbeats") == 60
