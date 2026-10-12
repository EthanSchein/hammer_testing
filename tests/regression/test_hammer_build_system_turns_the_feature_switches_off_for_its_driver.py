import json

import pytest

from hammer.config import HammerJSONEncoder
from hammer.vlsi import HammerDriver, HammerDriverOptions, error_scan, pd_cache, substep_resume, time_tracking

SWITCHES = {
    "pd_cache.is_cache_enabled": pd_cache.is_cache_enabled,
    "substep_resume.is_enabled": substep_resume.is_enabled,
    "substep_resume._db_enabled": substep_resume._db_enabled,
    "error_scan.is_enabled": error_scan.is_enabled,
    "time_tracking.is_ledger_enabled": time_tracking.is_ledger_enabled,
}


@pytest.fixture(autouse=True)
def _every_feature_exported_on(monkeypatch):
    """The caller's shell turns every feature on, and nothing calls legacy_hammer.apply to override it."""
    for name in ("HAMMER_PD_CACHE", "HAMMER_PD_CACHE_LEDGER", "HAMMER_SUBSTEP_RESUME", "HAMMER_DB_CHECKPOINTS",
                 "HAMMER_ERROR_SCAN"):
        monkeypatch.setenv(name, "1")


def _driver(tmp_path, build_system: str) -> HammerDriver:
    config = tmp_path / f"{build_system}.json"
    config.write_text(json.dumps({
        "vlsi.core.technology": "hammer.technology.nop",
        "vlsi.core.build_system": build_system,
        "vlsi.inputs.hierarchical.config_source": "none",
    }, cls=HammerJSONEncoder, indent=4))
    return HammerDriver(HammerDriverOptions(environment_configs=[], project_configs=[str(config)],
                                            log_file=str(tmp_path / "log.txt"), obj_dir=str(tmp_path / "obj")))


def test_every_switch_is_off_for_a_hammer_driver(tmp_path) -> None:
    driver = _driver(tmp_path, "hammer")
    assert {name: switch(driver) for name, switch in SWITCHES.items()} == {name: False for name in SWITCHES}


def test_the_same_switches_stay_on_for_a_make_driver(tmp_path) -> None:
    driver = _driver(tmp_path, "make")
    assert {name: switch(driver) for name, switch in SWITCHES.items()} == {name: True for name in SWITCHES}
