"""
Post-run tool-log error scan.

Innovus and genus treat many real failures as continuable: they print
**ERROR lines, keep going, and exit 0. Nothing downstream re-reads the log,
so a par run that could not legalize 150 instances looks identical to a
clean one. This scan runs after a stage the tool called successful, counts
the ERROR lines in its log, prints a verdict where you already look (the
stage log, which is also the Airflow task log), and can turn configured
error codes into hard stage failures.

Config:

  vlsi.error_scan.enabled: true          default on; HAMMER_ERROR_SCAN=0 wins
  vlsi.error_scan.ignore:  [IMPLF-24]    reviewed-and-accepted codes; counted
                                         separately, excluded from the verdict
  vlsi.error_scan.fail_on: [IMPSP-2021]  codes that fail the stage even though
                                         the tool exited cleanly

The scan is telemetry: it never raises and never fails a run except through
the explicit fail_on path.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from hammer.vlsi import legacy_hammer
from hammer.vlsi.substep_resume import _newest_log

ENABLE_ENV_VAR = "HAMMER_ERROR_SCAN"
ENABLE_SETTING_KEY = "vlsi.error_scan.enabled"
IGNORE_SETTING_KEY = "vlsi.error_scan.ignore"
FAIL_ON_SETTING_KEY = "vlsi.error_scan.fail_on"

# Cadence-style error lines: **ERROR: (CODE-123): message
_ERR_RE = re.compile(r"\*\*ERROR:?\s*(?:\(([A-Za-z0-9_-]+)\))?")


def is_enabled(driver: Optional[Any] = None) -> bool:
    if legacy_hammer.is_legacy_hammer(driver):
        return False
    env = os.environ.get(ENABLE_ENV_VAR)
    if env is not None:
        return env.strip().lower() not in ("", "0", "false", "no", "off")
    if driver is not None:
        from hammer.vlsi import sledge_settings
        return sledge_settings.flag(driver, ENABLE_SETTING_KEY, True)
    return True


def _setting_list(driver: Any, key: str) -> List[str]:
    try:
        val = driver.database.get_setting(key, nullvalue=None)
        if isinstance(val, list):
            return [str(v) for v in val]
    except Exception:
        pass
    return []


def scan_log(rundir: str, log_name: str) -> Optional[Dict[str, Any]]:
    """Count **ERROR lines in the newest rotation of the tool log.

    Returns ``{"log", "by_code", "samples"}`` or None if no log exists.
    Uncoded ERROR lines are grouped under "UNCODED".
    """
    log = _newest_log(rundir, log_name)
    if log is None:
        return None
    by_code: Dict[str, int] = {}
    samples: Dict[str, str] = {}
    try:
        with open(log, errors="ignore") as fh:
            for line in fh:
                m = _ERR_RE.search(line)
                if m:
                    code = m.group(1) or "UNCODED"
                    by_code[code] = by_code.get(code, 0) + 1
                    samples.setdefault(code, line.strip()[:200])
    except OSError:
        return None
    return {"log": log, "by_code": by_code, "samples": samples}


def scan_and_report(driver: Any, stage_tag: str, rundir: str,
                    log_name: str) -> Optional[Dict[str, Any]]:
    """Scan the stage's tool log and print a verdict via the driver's logger.

    Returns ``{"total", "ignored", "by_code", "fatal"}``; ``fatal`` is the
    list of counted codes that appear in vlsi.error_scan.fail_on (the caller
    fails the stage when it is non-empty). Never raises.
    """
    try:
        if not is_enabled(driver):
            return None
        res = scan_log(rundir, log_name)
        if res is None:
            return None
        log_fn_info = getattr(getattr(driver, "log", None), "info", lambda m: None)
        log_fn_warn = getattr(getattr(driver, "log", None), "warning", log_fn_info)
        log_fn_err = getattr(getattr(driver, "log", None), "error", log_fn_warn)

        ignore = set(_setting_list(driver, IGNORE_SETTING_KEY))
        fail_on = set(_setting_list(driver, FAIL_ON_SETTING_KEY))
        counted = {c: n for c, n in res["by_code"].items() if c not in ignore}
        ignored_n = sum(n for c, n in res["by_code"].items() if c in ignore)
        total = sum(counted.values())
        ign = f" (+{ignored_n} ignored per vlsi.error_scan.ignore)" if ignored_n else ""

        if total:
            top = ", ".join(f"{c} x{n}" for c, n in
                            sorted(counted.items(), key=lambda kv: -kv[1])[:3])
            log_fn_warn(
                f"Error scan: {stage_tag} succeeded but its tool log has "
                f"{total} ERROR line(s){ign} (top: {top}). "
                f"See {res['log']}")
        elif ignored_n:
            log_fn_info(f"Error scan: {stage_tag} tool log clean{ign}.")
        else:
            log_fn_info(f"Error scan: {stage_tag} tool log clean of ERROR lines.")

        fatal = sorted(c for c in counted if c in fail_on)
        if fatal:
            log_fn_err(
                f"Error scan: code(s) {', '.join(fatal)} are listed in "
                f"vlsi.error_scan.fail_on; failing the {stage_tag} stage "
                f"despite the tool's clean exit.")
        return {"total": total, "ignored": ignored_n,
                "by_code": counted, "fatal": fatal}
    except Exception:
        return None
