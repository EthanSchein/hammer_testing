"""
Time-saved tracking for the SledgeHammer PD cache.

This module is SledgeHammer-specific telemetry, kept out of the cache wrapper
(pd_cache.py) and the build-system generator (hammer_build_systems.py) on
purpose. It owns everything about *recording* and *reporting* how much time
the cache saves:

  * per-event recording: every cache decision (HIT / MISS_STORE / SKIP_*)
    becomes one event, written to a per-run JSONL file (feeds the exit_ task's
    one-run summary) and to the durable Postgres ledger
    (hammer_poc.pd_cache_events, via pd_store).
  * the recording switch (HAMMER_PD_CACHE_LEDGER / vlsi.pd_cache.ledger_enabled)
    and the project tag (HAMMER_PD_PROJECT / vlsi.pd_cache.project).
  * the per-run summary the exit_ task prints (read_run_cache_summary).
  * cross-run aggregation and the time-saved report behind
    `studio time-saved` and scripts/report_time_saved.py.

The measuring itself (timing the tool around run_fn) stays in pd_cache.py,
where the tool actually runs; this module only records and reports. See
readme_documents/TIME_SAVED_TRACKER.md for usage and attribution semantics.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hammer.vlsi import legacy_hammer, pd_store


# Recording switch for the durable time-saved ledger (the pd_cache_events
# Postgres table). Separate from the cache itself: the cache can be on while
# ledger recording is off (you still get HITs, just no durable savings rows).
LEDGER_ENV_VAR = "HAMMER_PD_CACHE_LEDGER"
LEDGER_SETTING_KEY = "vlsi.pd_cache.ledger_enabled"

# Optional human-assigned project label stamped onto each ledger row, for
# bucketing several designs/dags under one tapeout. Env var wins; the config
# key is a fallback so a design can pin its project in yml.
PROJECT_ENV_VAR = "HAMMER_PD_PROJECT"
PROJECT_SETTING_KEY = "vlsi.pd_cache.project"


def is_ledger_enabled(driver: Optional[Any] = None) -> bool:
    """True if the durable time-saved ledger should record cache events.

    This is the tracker's recording switch. It gates ONLY the durable Postgres
    rows (pd_store.record_cache_event); the per-run JSONL summary that feeds the
    exit_ task is unaffected. Defaults to on.

    Turn it off with ``HAMMER_PD_CACHE_LEDGER=0`` (or ``=false/no/off``), or in
    config with ``vlsi.pd_cache.ledger_enabled: false``. The env var wins; the
    config key is consulted only when the env var is unset and a driver is
    given. Set the env var in the Airflow worker env (e.g. venv.sh) to control
    DAG runs centrally. Always off under vlsi.core.build_system: hammer.
    """
    if legacy_hammer.is_legacy_hammer(driver):
        return False
    env = os.environ.get(LEDGER_ENV_VAR)
    if env is not None:
        return env.strip().lower() not in ("", "0", "false", "no", "off")
    if driver is not None:
        from hammer.vlsi import sledge_settings
        return sledge_settings.flag(driver, LEDGER_SETTING_KEY, True)
    return True


def stamp_project_from_config(driver: Optional[Any]) -> None:
    """If no project env var is set, pull one from config (vlsi.pd_cache.project).

    Stamps HAMMER_PD_PROJECT so record_event (which reads the env) tags rows
    with it. A shell/worker env var or the DAG trigger conf both win over the
    config key, since either would already have set the env var.
    """
    if os.environ.get(PROJECT_ENV_VAR) or driver is None:
        return
    try:
        from hammer.vlsi import sledge_settings
        val = sledge_settings.text(driver, PROJECT_SETTING_KEY)
        if val:
            os.environ[PROJECT_ENV_VAR] = str(val)
    except Exception:
        pass


def make_would_rerun(driver: Optional[Any], output_file) -> Optional[bool]:
    """Reproduce legacy make's rerun decision for a stage, at skip time.

    Legacy hammer's only whole-stage skip is the hammer.d Makefile rule: rerun
    when any prerequisite (every project/env config plus the RTL input files)
    has a newer mtime than the stage's output file. When our content-aware
    dependency check skips a stage, this tells us whether legacy would have
    skipped it too (False -> no credit) or rerun the tool (True -> the skip is
    a SledgeHammer-only saving, e.g. a config was rewritten but the settings
    that matter didn't change).

    Returns None when the verdict can't be computed (no driver, missing
    output file, stat errors); callers should record None as "unknown" and
    reporting treats unknown conservatively (no credit).
    """
    if driver is None:
        return None
    try:
        out = Path(output_file)
        if not out.exists():
            # No target on disk: make unconditionally reruns.
            return True
        prereqs: List[str] = []
        opts = getattr(driver, "options", None)
        if opts is not None:
            prereqs += list(getattr(opts, "project_configs", []) or [])
            prereqs += list(getattr(opts, "environment_configs", []) or [])
        try:
            rtl = driver.database.get_setting("synthesis.inputs.input_files", [])
            prereqs += [f for f in (rtl or []) if isinstance(f, str)]
        except Exception:
            pass
        if not prereqs:
            return None
        out_mtime = out.stat().st_mtime
        for p in prereqs:
            try:
                if os.path.getmtime(p) > out_mtime:
                    return True
            except OSError:
                # make treats a missing prerequisite as an error/rebuild;
                # count it as a rerun rather than silently skipping it.
                return True
        return False
    except Exception:
        return None


def stage_module(driver: Optional[Any], stage_tag: str) -> Optional[str]:
    """The block a stage is running on, for tagging events.

    Reads <stage>.inputs.top_module from the live config, which hierarchical
    flows swap per module (syn-SubModA runs with synthesis.inputs.top_module =
    SubModA) and flat flows leave at the design top. Works before the tool is
    loaded, so both the cache path and the dep-check skip path can use it.
    """
    if driver is None:
        return None
    prefix = {"synthesis": "synthesis", "syn": "synthesis"}.get(stage_tag, stage_tag)
    for key in (f"{prefix}.inputs.top_module", "synthesis.inputs.top_module"):
        try:
            val = driver.database.get_setting(key, nullvalue=None)
            if val:
                return str(val)
        except Exception:
            continue
    return None


def _format_duration(seconds: float) -> str:
    """Pretty-print a wall-clock duration. Short units for short durations."""
    if seconds < 1.0:
        return f"{seconds*1000:.0f}ms"
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    if seconds < 3600.0:
        m, s = divmod(int(round(seconds)), 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(int(round(seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def _format_savings(
    original_duration: Optional[float],
    restore_seconds: float,
    original_cpu: Optional[float] = None,
) -> str:
    """Build the '(saved ~Xs vs original Ys tool run)' suffix, or '' if unknown.

    If ``original_cpu`` is set, also report the CPU time saved (which for
    multi-threaded tools like Innovus is often much bigger than wall-clock
    saved).
    """
    if original_duration is None or original_duration <= 0:
        return ""
    saved = max(0.0, original_duration - restore_seconds)
    msg = (
        f" Saved ~{_format_duration(saved)} wall "
        f"(restore {_format_duration(restore_seconds)} "
        f"vs original {_format_duration(original_duration)} tool run)"
    )
    if original_cpu is not None and original_cpu > 0:
        # Restore is single-threaded tar untar, so CPU cost is ~= wall cost
        # for that side. Subtract it from the cached CPU figure.
        cpu_saved = max(0.0, original_cpu - restore_seconds)
        msg += (
            f"; ~{_format_duration(cpu_saved)} CPU "
            f"(original tool consumed {_format_duration(original_cpu)} CPU)"
        )
    return msg + "."


# ----- Per-DAG-run event log (for the exit_ task summary) -----

def _events_dir() -> Optional[Path]:
    """Where to write per-run cache event JSONL files."""
    airflow_home = os.environ.get("AIRFLOW_HOME")
    if not airflow_home:
        return None
    p = Path(airflow_home) / "cache_events"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    return p


def _events_file_for_run(run_id: str) -> Optional[Path]:
    d = _events_dir()
    if d is None or not run_id:
        return None
    # Sanitize run_id for filename use (slashes, colons, plus signs from
    # Airflow's manual__<isotimestamp>+00:00 format would otherwise break).
    safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in run_id)
    return d / f"{safe}.jsonl"


def record_event(
    stage_tag: str,
    outcome: str,
    *,
    saved_seconds: Optional[float] = None,
    tool_seconds: Optional[float] = None,
    restore_seconds: Optional[float] = None,
    store_seconds: Optional[float] = None,
    saved_cpu_seconds: Optional[float] = None,
    tool_cpu_seconds: Optional[float] = None,
    make_would_rerun: Optional[bool] = None,
    module: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> None:
    """
    Append a single cache event for the current DAG run.

    Outcomes: ``HIT``, ``MISS_STORE``, ``SKIP_LOCAL`` (dep-check skip,
    local files were already on disk), ``SKIP_RESTORED`` (dep-check skip,
    files missing so restored from tarball), ``SKIP_NO_BLOB`` (dep-check
    skip, no local files AND no cache blob - downstream will likely fail).

    The ``*_cpu_seconds`` pair mirrors the wall-clock pair: for MISS_STORE
    we record what the tool just burned; for HIT/SKIP we record the saved
    figure pulled from the cached blob.

    Silently no-ops outside an Airflow run (no AIRFLOW_HOME or no run_id).
    """
    run_id = os.environ.get("HAMMER_AIRFLOW_RUN_ID", "")
    dag_id = os.environ.get("HAMMER_AIRFLOW_DAG_ID", "")
    design = os.environ.get("HAMMER_AIRFLOW_DESIGN") or os.environ.get("design") or None
    project = os.environ.get("HAMMER_PD_PROJECT") or None
    # 1. Per-run JSONL log -- feeds the exit_ task's one-run summary, then gets
    #    deleted by clear_run_cache_events. Only written inside an Airflow run.
    #    Carries design/project so the exit_ summary can show which project
    #    this run was tagged under.
    f = _events_file_for_run(run_id)
    if f is not None:
        event = {
            "ts":                time.time(),
            "dag_id":            dag_id,
            "run_id":            run_id,
            "stage_tag":         stage_tag,
            "outcome":           outcome,
            "saved_seconds":     saved_seconds,
            "tool_seconds":      tool_seconds,
            "restore_seconds":   restore_seconds,
            "store_seconds":     store_seconds,
            "saved_cpu_seconds": saved_cpu_seconds,
            "tool_cpu_seconds":  tool_cpu_seconds,
            "design":            design,
            "project":           project,
            "make_would_rerun":  make_would_rerun,
            "module":            module,
        }
        try:
            with f.open("a") as fh:
                fh.write(json.dumps(event) + "\n")
        except Exception:
            # Telemetry must never fail the run.
            pass
    # 2. Durable Postgres ledger -- survives the exit_ cleanup so the time-saved
    #    tracker can total savings across every run of a tapeout. Gated by the
    #    ledger switch (default on; HAMMER_PD_CACHE_LEDGER=0 to disable). Best
    #    effort only: the DB may be unreachable, or a direct shell run may have
    #    no Postgres password, so any failure here is swallowed instead of
    #    stalling the run.
    if enabled is None:
        enabled = is_ledger_enabled()
    if not enabled:
        return
    try:
        pd_store.record_cache_event(
            stage_tag, outcome,
            saved_seconds=saved_seconds,
            tool_seconds=tool_seconds,
            restore_seconds=restore_seconds,
            store_seconds=store_seconds,
            saved_cpu_seconds=saved_cpu_seconds,
            tool_cpu_seconds=tool_cpu_seconds,
            triggering_user=os.environ.get("HAMMER_AIRFLOW_TRIGGERING_USER") or None,
            dag_id=dag_id or None,
            dag_run_id=run_id or None,
            workspace=os.environ.get("HAMMER_AIRFLOW_WORKSPACE") or None,
            design=design,
            project=project,
            make_would_rerun=make_would_rerun,
            module=module,
        )
    except Exception:
        pass


def read_run_cache_summary(run_id: str) -> str:
    """
    Read all cache events for ``run_id`` and return a formatted multi-line
    summary suitable for printing in the ``exit_`` task.

    Returns an empty string (no header, no table) if no events file exists.
    """
    f = _events_file_for_run(run_id)
    if f is None or not f.exists():
        return ""
    events = []
    try:
        with f.open("r") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return ""
    if not events:
        return ""

    # Track savings by source so we can report cache vs dep-check separately.
    # HIT              -> cache saved us (the dep-check said "run", cache said "no need")
    # SKIP_RESTORED    -> cache saved us (dep-check said skip, but local files
    #                     were missing - without the cache this would have
    #                     forced a re-run, so credit goes to cache)
    # SKIP_LOCAL       -> dep-check saved us (files were already on disk).
    #                     Split by the make verdict: if legacy make's mtime rule
    #                     would have rerun the stage, the skip is SledgeHammer's;
    #                     if make would have skipped too (or unknown), no credit.
    # MISS_STORE       -> no savings; the tool actually ran
    # SKIP_NO_BLOB     -> no savings; the run probably failed
    cache_saved = 0.0       # HIT + SKIP_RESTORED  (wall-clock)
    depsh_saved = 0.0       # SKIP_LOCAL, make would rerun     (wall-clock)
    deplegacy_saved = 0.0   # SKIP_LOCAL, make would skip too  (wall-clock)
    resume_saved = 0.0      # RESUME, skipped completed steps  (wall-clock)
    total_ran = 0.0         # MISS_STORE           (wall-clock)
    failed_ran = 0.0        # MISS_FAIL            (wall-clock)
    failed_count = 0
    cache_saved_cpu = 0.0   # HIT + SKIP_RESTORED  (CPU)
    depsh_saved_cpu = 0.0
    deplegacy_saved_cpu = 0.0
    resume_saved_cpu = 0.0
    total_ran_cpu = 0.0     # MISS_STORE           (CPU)
    failed_ran_cpu = 0.0    # MISS_FAIL            (CPU)

    def _wc(s: Optional[float]) -> str:
        """Format 'wall / cpu' pair for the detail column."""
        return _format_duration(s) if s is not None else "-"

    lines = []
    # Echo which design/project this run recorded under, so a UI-triggered run
    # can be checked from this task's log (set via conf {"project": ...} or config).
    design = next((e.get("design") for e in events if e.get("design")), None)
    project = next((e.get("project") for e in events if e.get("project")), None)
    lines.append(f"  Design:  {design or '(unset)'}")
    lines.append(f"  Project: {project or '(no project set)'}")
    lines.append("")
    header = (
        f"{'stage':<12}  {'outcome':<14}  {'attributed to':<14}  "
        f"{'wall':<10}  {'cpu':<10}  detail"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for ev in events:
        stage = ev.get("stage_tag", "?")
        mod = ev.get("module")
        outcome = ev.get("outcome", "?")
        saved = ev.get("saved_seconds")
        tool = ev.get("tool_seconds")
        restore = ev.get("restore_seconds")
        saved_cpu = ev.get("saved_cpu_seconds")
        tool_cpu = ev.get("tool_cpu_seconds")
        attribution = ""
        wall_col = "-"
        cpu_col = "-"
        detail = ""
        if outcome == "HIT" and saved is not None:
            attribution = "cache"
            wall_col = _wc(saved)
            cpu_col = _wc(saved_cpu)
            detail = f"restore {_format_duration(restore or 0)}"
            cache_saved += saved
            if saved_cpu is not None:
                cache_saved_cpu += saved_cpu
        elif outcome == "SKIP_LOCAL" and saved is not None:
            attribution = "dep-check"
            wall_col = _wc(saved)
            cpu_col = _wc(saved_cpu)
            if ev.get("make_would_rerun") is True:
                detail = "files on disk; legacy make would have rerun"
                depsh_saved += saved
                if saved_cpu is not None:
                    depsh_saved_cpu += saved_cpu
            else:
                detail = "files on disk; legacy make would skip too"
                deplegacy_saved += saved
                if saved_cpu is not None:
                    deplegacy_saved_cpu += saved_cpu
        elif outcome == "SKIP_RESTORED" and saved is not None:
            attribution = "cache"
            wall_col = _wc(saved)
            cpu_col = _wc(saved_cpu)
            detail = "dep-check skip, restored from cache"
            cache_saved += saved
            if saved_cpu is not None:
                cache_saved_cpu += saved_cpu
        elif outcome == "RESUME" and saved is not None:
            attribution = "resume"
            wall_col = _wc(saved)
            cpu_col = _wc(saved_cpu)
            detail = "resumed from checkpoint; skipped completed steps"
            resume_saved += saved
            if saved_cpu is not None:
                resume_saved_cpu += saved_cpu
        elif outcome == "MISS_STORE" and tool is not None:
            attribution = "—"
            wall_col = _wc(tool)
            cpu_col = _wc(tool_cpu)
            detail = "ran, stored to cache"
            total_ran += tool
            if tool_cpu is not None:
                total_ran_cpu += tool_cpu
        elif outcome == "MISS_FAIL":
            attribution = "—"
            wall_col = _wc(tool)
            cpu_col = _wc(tool_cpu)
            detail = "FAILED (counted separately, not in savings)"
            if tool is not None:
                failed_ran += tool
                failed_count += 1
            if tool_cpu is not None:
                failed_ran_cpu += tool_cpu
        elif outcome == "SKIP_NO_BLOB":
            attribution = "—"
            detail = "WARNING: dep-check skip but no cache blob — downstream may fail"
        elif saved is None and outcome in ("HIT", "SKIP_LOCAL", "SKIP_RESTORED"):
            # Cache blob predates the duration-tracking feature; we know
            # we skipped a tool run but can't quantify how much we saved.
            attribution = "cache" if outcome != "SKIP_LOCAL" else "dep-check"
            detail = "skipped tool run (original duration not recorded)"
        if mod:
            detail = f"[{mod}] {detail}" if detail else f"[{mod}]"
        lines.append(
            f"{stage:<12}  {outcome:<14}  {attribution:<14}  "
            f"{wall_col:<10}  {cpu_col:<10}  {detail}"
        )
    lines.append("-" * len(header))
    sledge_saved = cache_saved + depsh_saved + resume_saved
    sledge_saved_cpu = cache_saved_cpu + depsh_saved_cpu + resume_saved_cpu
    total_saved = sledge_saved + deplegacy_saved
    total_saved_cpu = sledge_saved_cpu + deplegacy_saved_cpu

    def _pair(wall: float, cpu: float) -> str:
        return (
            f"wall {_format_duration(wall):<10}  "
            f"cpu {_format_duration(cpu):<10}"
        )

    lines.append(f"  Saved by cache:                     {_pair(cache_saved, cache_saved_cpu)}")
    lines.append(f"  Saved by dep-check (vs make rerun): {_pair(depsh_saved, depsh_saved_cpu)}")
    if resume_saved or resume_saved_cpu:
        lines.append(f"  Saved by substep resume:            {_pair(resume_saved, resume_saved_cpu)}")
    lines.append(f"  SledgeHammer time saved:            {_pair(sledge_saved, sledge_saved_cpu)}")
    lines.append(f"  Legacy-equivalent skips:            {_pair(deplegacy_saved, deplegacy_saved_cpu)}")
    lines.append(f"  Total skipped work:                 {_pair(total_saved, total_saved_cpu)}")
    lines.append(f"  Stages that actually ran:           {_pair(total_ran, total_ran_cpu)}")
    return "\n".join(lines)


def clear_run_cache_events(run_id: str) -> None:
    """Delete the events file for a run (call after summary is printed)."""
    f = _events_file_for_run(run_id)
    if f is not None and f.exists():
        try:
            f.unlink()
        except Exception:
            pass


# ----- Cross-run savings aggregation (the time-saved tracker) -----
#
# read_run_cache_summary (above) tallies ONE run from its JSONL file. The
# helpers below tally MANY runs -- from the durable Postgres ledger
# (pd_store.fetch_cache_events) and/or the on-disk JSONL files -- so we can
# answer "how much wall-clock / CPU time did the PD cache save across this
# whole tapeout?". The per-event attribution here is the same as
# read_run_cache_summary's; keep the two in sync if the outcome rules change.


def _attribute_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """Classify one cache event into the savings buckets.

    Returns ``{attribution, wall_saved, cpu_saved, wall_ran, cpu_ran,
    quantified}``. ``attribution`` is one of ``cache`` / ``dep-sh`` /
    ``dep-legacy`` / ``ran`` / ``none``:

      * ``cache``      - HIT / SKIP_RESTORED. Needs the blob cache; legacy
                         hammer would have run the tool. SledgeHammer-only.
      * ``dep-sh``     - SKIP_LOCAL where make_would_rerun is True: legacy
                         make's mtime rule would have rerun the stage, so the
                         content-aware dep-check saved it. SledgeHammer-only.
      * ``dep-legacy`` - SKIP_LOCAL where make would have skipped too (False),
                         or where the verdict is unknown (None / old rows).
                         Conservatively given no SledgeHammer credit.
      * ``ran``        - MISS_STORE, the tool actually ran.

    ``quantified`` is False for a skip we know happened but whose original
    duration wasn't recorded (blob predates duration tracking), so callers can
    count it as a hit without inflating the saved total.
    """
    outcome = ev.get("outcome")
    saved = ev.get("saved_seconds")
    tool = ev.get("tool_seconds")
    saved_cpu = ev.get("saved_cpu_seconds")
    tool_cpu = ev.get("tool_cpu_seconds")
    restore = ev.get("restore_seconds")
    dep_kind = "dep-sh" if ev.get("make_would_rerun") is True else "dep-legacy"
    res = {"attribution": "none", "wall_saved": 0.0, "cpu_saved": 0.0,
           "wall_ran": 0.0, "cpu_ran": 0.0, "quantified": False}
    if outcome == "RESUME" and saved is not None:
        # a failed/paused run picked up from its last confirmed checkpoint
        # instead of starting over; saved = measured time of the skipped steps
        res.update(attribution="resume", wall_saved=float(saved),
                   cpu_saved=float(saved_cpu or 0.0), quantified=True)
    elif outcome in ("HIT", "SKIP_RESTORED") and saved is not None:
        # saved_seconds is already NET of the restore overhead: pd_cache stores
        # saved = original_duration - restore_seconds, where restore_seconds
        # covers fetching the blob from Postgres, decompressing, and untarring.
        # So a stage whose restore costs more than a rerun (tiny stage, huge
        # blob) is already floored at 0 there -- do not subtract restore again.
        res.update(attribution="cache", wall_saved=float(saved),
                   cpu_saved=float(saved_cpu or 0.0), quantified=True)
    elif outcome == "SKIP_LOCAL" and saved is not None:
        res.update(attribution=dep_kind, wall_saved=float(saved),
                   cpu_saved=float(saved_cpu or 0.0), quantified=True)
    elif outcome == "MISS_STORE" and tool is not None:
        res.update(attribution="ran", wall_ran=float(tool),
                   cpu_ran=float(tool_cpu or 0.0), quantified=True)
    elif outcome == "MISS_FAIL" and tool is not None:
        # Tool time spent on a stage that failed. Its own attribution so it
        # stays out of both the savings totals and "actually ran" -- those
        # describe work that completed.
        res.update(attribution="failed", wall_ran=float(tool),
                   cpu_ran=float(tool_cpu or 0.0), quantified=True)
    elif outcome in ("HIT", "SKIP_RESTORED", "SKIP_LOCAL") and saved is None:
        # We skipped a tool run but the blob predates duration tracking.
        res.update(attribution=(dep_kind if outcome == "SKIP_LOCAL" else "cache"),
                   quantified=False)
    return res


def iter_jsonl_cache_events(events_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read every event from every ``*.jsonl`` under the cache_events dir.

    Defaults to ``$AIRFLOW_HOME/cache_events``. These files are per-run and the
    exit_ task deletes them after summarizing, so this only sees runs that
    haven't been cleaned up yet -- the durable Postgres ledger
    (pd_store.fetch_cache_events) is the complete cross-run source.
    """
    if events_dir is not None:
        d: Optional[Path] = Path(events_dir)
    else:
        d = _events_dir()
    if d is None or not d.is_dir():
        return []
    events: List[Dict[str, Any]] = []
    for jf in sorted(d.glob("*.jsonl")):
        try:
            with jf.open("r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            continue
    return events


def _group_key(ev: Dict[str, Any], group_by: str) -> str:
    if group_by in ("stage", "stage_tag"):
        return ev.get("stage_tag") or "(unknown)"
    if group_by in ("dag", "dag_id"):
        return ev.get("dag_id") or "(none)"
    if group_by == "design":
        return ev.get("design") or ev.get("dag_id") or "(unknown)"
    if group_by == "project":
        return ev.get("project") or "(unassigned)"
    if group_by == "module":
        return ev.get("module") or "(unknown)"
    if group_by in ("run", "run_id"):
        return ev.get("run_id") or ev.get("dag_run_id") or "(none)"
    return "all"


def _empty_bucket() -> Dict[str, Any]:
    return {"events": 0, "hits": 0, "misses": 0, "skips": 0, "warnings": 0,
            "unquantified": 0, "cache_wall": 0.0, "cache_cpu": 0.0,
            "depsh_wall": 0.0, "depsh_cpu": 0.0,
            "deplegacy_wall": 0.0, "deplegacy_cpu": 0.0,
            "resume_wall": 0.0, "resume_cpu": 0.0,
            "ran_wall": 0.0, "ran_cpu": 0.0,
            "failed_wall": 0.0, "failed_cpu": 0.0, "failures": 0}


def _accumulate(bucket: Dict[str, Any], ev: Dict[str, Any]) -> None:
    bucket["events"] += 1
    outcome = ev.get("outcome")
    if outcome == "MISS_STORE":
        bucket["misses"] += 1
    elif outcome == "HIT":
        bucket["hits"] += 1
    elif outcome in ("SKIP_LOCAL", "SKIP_RESTORED"):
        bucket["skips"] += 1
    elif outcome == "SKIP_NO_BLOB":
        bucket["warnings"] += 1
    elif outcome == "MISS_FAIL":
        bucket["failures"] = bucket.get("failures", 0)
    a = _attribute_event(ev)
    if a["attribution"] == "cache":
        bucket["cache_wall"] += a["wall_saved"]
        bucket["cache_cpu"] += a["cpu_saved"]
    elif a["attribution"] == "dep-sh":
        bucket["depsh_wall"] += a["wall_saved"]
        bucket["depsh_cpu"] += a["cpu_saved"]
    elif a["attribution"] == "dep-legacy":
        bucket["deplegacy_wall"] += a["wall_saved"]
        bucket["deplegacy_cpu"] += a["cpu_saved"]
    elif a["attribution"] == "resume":
        bucket["resume_wall"] += a["wall_saved"]
        bucket["resume_cpu"] += a["cpu_saved"]
    elif a["attribution"] == "ran":
        bucket["ran_wall"] += a["wall_ran"]
        bucket["ran_cpu"] += a["cpu_ran"]
    elif a["attribution"] == "failed":
        bucket["failed_wall"] += a["wall_ran"]
        bucket["failed_cpu"] += a["cpu_ran"]
        bucket["failures"] += 1
    if a["attribution"] in ("cache", "dep-sh", "dep-legacy") and not a["quantified"]:
        bucket["unquantified"] += 1


def aggregate_savings(events: List[Dict[str, Any]],
                      group_by: str = "stage") -> Dict[str, Any]:
    """Total cache savings over a list of events, with a per-group breakdown."""
    totals = _empty_bucket()
    groups: Dict[str, Dict[str, Any]] = {}
    runs = set()
    dags = set()
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    for ev in events:
        _accumulate(totals, ev)
        gk = _group_key(ev, group_by)
        _accumulate(groups.setdefault(gk, _empty_bucket()), ev)
        rid = ev.get("run_id") or ev.get("dag_run_id")
        if rid:
            runs.add(rid)
        if ev.get("dag_id"):
            dags.add(ev.get("dag_id"))
        ts = ev.get("ts")
        if isinstance(ts, (int, float)):
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
    return {"totals": totals, "groups": groups, "group_by": group_by,
            "n_runs": len(runs), "n_dags": len(dags),
            "first_ts": first_ts, "last_ts": last_ts}


def _event_wall(ev: Dict[str, Any]) -> float:
    """Actual wall-clock a task occupied: the tool run, else a cache restore."""
    for k in ("tool_seconds", "restore_seconds"):
        v = ev.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return 0.0


def _airflow_task_windows(run_ids: List[str]) -> Dict[str, List[Tuple[float, float]]]:
    """Per-run (start, end) epoch pairs for each substantial task, read from
    Airflow's task_instance table.

    Parallelism cannot be measured from the cache ledger alone: only stages
    that consult the PD cache leave events there (syn, par), and those run
    one after another. The stages that actually overlap -- drc beside lvs,
    sibling modules in a hierarchical flow -- never appear. Airflow records
    every task's true window, so that is the honest source.

    Tasks shorter than a minute are skipped: the bridges, start and exit_
    bookkeeping tasks all overlap trivially and would inflate the credit
    with time no tool ever spent. Returns {} when the metadata DB is
    unreachable, in which case the caller falls back to ledger windows.

    Failed tasks count too. A module that ran three hours beside its siblings
    and then failed still had those three hours masked by the parallel flow --
    the wall-clock saving happened whether or not the stage succeeded, and a
    hierarchical run that dies in one leaf is exactly where parallelism pays.
    Skipped and upstream_failed tasks never executed, so they stay excluded
    (the one-minute floor drops them anyway).
    """
    if not run_ids:
        return {}
    import os
    try:
        import psycopg2
        from hammer.vlsi import pd_store
    except Exception:
        return {}

    # A ledger outlives the Airflow instance that produced it: runs migrate
    # between servers, and a merged ledger spans several. Querying only the
    # currently-configured metadata DB silently scores every run from every
    # other instance as perfectly sequential. SLEDGE_EXTRA_METADATA_CONNS is a
    # comma-separated list of extra postgres URIs to union in.
    sources = []
    try:
        primary = pd_store.airflow_metadata_conn_settings()
        if primary:
            sources.append(primary)
    except Exception:
        pass
    for uri in (os.environ.get("SLEDGE_EXTRA_METADATA_CONNS") or "").split(","):
        uri = uri.strip()
        if not uri:
            continue
        try:
            sources.append(pd_store._parse_conn_uri(uri))
        except Exception:
            continue
    if not sources:
        return {}

    # (run_id, start, end) deduped: the same run can exist in more than one
    # metadata DB after a migration, and counting its tasks twice would
    # invent overlap that never happened.
    seen: set = set()
    out: Dict[str, List[Tuple[float, float]]] = {}
    for settings in sources:
        try:
            conn = psycopg2.connect(connect_timeout=20, **settings)
        except Exception:
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT run_id, task_id, start_date, end_date FROM task_instance "
                    "WHERE run_id = ANY(%s) AND state IN ('success', 'failed') "
                    "AND start_date IS NOT NULL AND end_date IS NOT NULL "
                    "AND EXTRACT(EPOCH FROM (end_date - start_date)) >= 60",
                    (list(run_ids),))
                for run_id, task_id, start, end in cur.fetchall():
                    key = (run_id, start.timestamp(), end.timestamp())
                    if key in seen:
                        continue
                    seen.add(key)
                    out.setdefault(run_id, []).append(
                        (start.timestamp(), end.timestamp(), task_id))
        except Exception:
            pass
        finally:
            conn.close()
    return out


def parallel_savings(events: List[Dict[str, Any]],
                     group_by: Optional[str] = None) -> Dict[str, Any]:
    """Wall-clock saved by running module tasks concurrently (task-level
    parallelism in a hierarchical flow).

    Per run, the sequential-equivalent time is the sum of every task's wall
    duration; the actual time is the run's wall span (earliest start to latest
    end). Their difference is the time masked by parallelism -- the non-
    critical-path modules that overlapped the longest one. Top-level
    integration runs after the leaves, so its window does not overlap the leaf
    windows and is not counted as saved (it cancels out), which is why this
    needs no leaf-vs-top bookkeeping. Parallelism overlaps work rather than
    removing it, so CPU-seconds saved is zero by definition.

    Returns {parallel_wall_s, parallel_cpu_s, parallel_runs}; with group_by,
    also {groups: {key: wall_saved}} attributing each run to its longest task.
    """
    from collections import defaultdict
    runs: Dict[str, list] = defaultdict(list)
    for ev in events:
        rk = ev.get("run_id") or ev.get("dag_run_id")
        if rk:
            runs[rk].append(ev)
    total = 0.0
    n_par = 0
    groups: Dict[str, float] = defaultdict(float)
    task_windows = _airflow_task_windows(list(runs.keys()))
    for run_key, evs in runs.items():
        windows = []
        # Airflow's task windows cover every stage; the ledger only covers
        # the cached ones, so prefer the former and keep the latter as the
        # fallback for runs the metadata DB no longer has.
        for start, end, task_id in task_windows.get(run_key, []):
            # attribution info: the module is in the task name
            # (module_<name>.<stage>); fall back to the run's first event.
            ev = dict(evs[0])
            if task_id.startswith("module_"):
                ev["module"] = task_id[len("module_"):].split(".")[0]
            windows.append((start, end, ev))
        if len(windows) < 2:
            windows = []
            for ev in evs:
                dur = _event_wall(ev)
                ts = ev.get("ts")
                if dur > 0 and isinstance(ts, (int, float)):
                    windows.append((ts - dur, ts, ev))
        if len(windows) < 2:
            continue
        sum_dur = sum(w[1] - w[0] for w in windows)
        span = max(w[1] for w in windows) - min(w[0] for w in windows)
        saved = sum_dur - span
        if saved <= 0:      # sequential, or idle gaps outweigh any overlap
            continue
        total += saved
        n_par += 1
        if group_by:
            crit = max(windows, key=lambda w: w[1] - w[0])[2]
            groups[_group_key(crit, group_by)] += saved
    out = {"parallel_wall_s": total, "parallel_cpu_s": 0.0, "parallel_runs": n_par}
    if group_by:
        out["groups"] = dict(groups)
    return out


def time_saved_summary(**filters: Any) -> Dict[str, Any]:
    """One call for the paper numbers: query the ledger and return the full
    turnaround-time breakdown as a dict.

    Accepts the same filters as `studio time-saved` (project=, design=, dag=,
    stage=, module=, user=, since=, until=, source=, limit=). Returns:

      dep_management_wall_s / dep_management_cpu_s
      caching_wall_s        / caching_cpu_s
      checkpointing_wall_s  / checkpointing_cpu_s
      parallel_wall_s       / parallel_cpu_s
      total_wall_saved_s    / total_cpu_saved_s
      events, runs, dags, source

    Same attribution as the report and the CSV: dep management counts only
    skips legacy make would have rerun, checkpointing CPU is a floor (resume
    savings are wall-clock measured), and parallel-execution CPU is zero
    (concurrency overlaps work, it does not remove it).
    """
    events, source = collect_savings_events(**filters)
    agg = aggregate_savings(events, group_by="none")
    par = parallel_savings(events)
    t = agg["totals"]
    return {
        "dep_management_wall_s": t["depsh_wall"],
        "dep_management_cpu_s": t["depsh_cpu"],
        "caching_wall_s": t["cache_wall"],
        "caching_cpu_s": t["cache_cpu"],
        "checkpointing_wall_s": t["resume_wall"],
        "checkpointing_cpu_s": t["resume_cpu"],
        "parallel_wall_s": par["parallel_wall_s"],
        "parallel_cpu_s": par["parallel_cpu_s"],
        "total_wall_saved_s": t["depsh_wall"] + t["cache_wall"] + t["resume_wall"] + par["parallel_wall_s"],
        "total_cpu_saved_s": t["depsh_cpu"] + t["cache_cpu"] + t["resume_cpu"],
        "events": t["events"], "runs": agg["n_runs"], "dags": agg["n_dags"],
        "source": source,
    }


def savings_csv(events: List[Dict[str, Any]], group_by: str = "project") -> str:
    """The paper's turnaround-time table as CSV: one row per group plus TOTAL.

    Eight numeric columns, wall-clock and compute (CPU) seconds for each of
    the three savings categories and their sums:

      legacy skip     - SKIP_LOCAL events legacy make would ALSO have
                        skipped: real time not spent, but no credit, so it
                        is reported beside the totals rather than inside them
      ran             - tool time that actually executed (successful stages)

      dep management  - SKIP_LOCAL events where legacy make's mtime rule
                        would have rerun the stage (make_would_rerun=True)
      caching         - HIT / SKIP_RESTORED blob restores
      checkpointing   - RESUME events (sub-step resume)
      parallel        - wall masked by running module tasks concurrently
                        (sum of task durations minus the run's wall span)

    The totals match the report's SLEDGEHAMMER TIME SAVED definition
    (dep-legacy skips get no credit). Checkpointing CPU is a floor: resume
    savings are measured from checkpoint timestamps, which give wall-clock
    only, so unknown CPU counts as zero rather than an estimate. Parallel CPU
    is zero by definition -- concurrency overlaps work, it does not remove it.
    """
    agg = aggregate_savings(events, group_by=group_by)
    par = parallel_savings(events, group_by=group_by)
    par_groups = par.get("groups", {})
    label = group_by if group_by != "none" else "scope"
    cols = ["dep_management_wall_s", "dep_management_cpu_s",
            "caching_wall_s", "caching_cpu_s",
            "checkpointing_wall_s", "checkpointing_cpu_s",
            "parallel_wall_s", "parallel_cpu_s",
            "total_wall_saved_s", "total_cpu_saved_s",
            "legacy_skip_wall_s", "legacy_skip_cpu_s",
            "ran_wall_s", "ran_cpu_s"]
    lines = [",".join([label] + cols)]

    def _row(name: str, b: Dict[str, Any], par_w: float) -> str:
        dep_w, dep_c = b["depsh_wall"], b["depsh_cpu"]
        cache_w, cache_c = b["cache_wall"], b["cache_cpu"]
        ck_w, ck_c = b["resume_wall"], b["resume_cpu"]
        vals = [dep_w, dep_c, cache_w, cache_c, ck_w, ck_c, par_w, 0.0,
                dep_w + cache_w + ck_w + par_w, dep_c + cache_c + ck_c,
                b.get("deplegacy_wall", 0.0), b.get("deplegacy_cpu", 0.0),
                b.get("ran_wall", 0.0), b.get("ran_cpu", 0.0)]
        safe = str(name).replace(",", ";")
        return ",".join([safe] + [f"{v:.1f}" for v in vals])

    if group_by != "none":
        for name in sorted(agg["groups"]):
            lines.append(_row(name, agg["groups"][name], par_groups.get(name, 0.0)))
    lines.append(_row("TOTAL", agg["totals"], par["parallel_wall_s"]))
    return "\n".join(lines) + "\n"


def airflow_failed_time() -> Optional[float]:
    """Wall-clock seconds burned by FAILED module tasks, from Airflow.

    The ledger records completed stages only -- a par that dies at hour three
    writes nothing -- so "time that actually ran" is productive tool time.
    This measures the failures from the metadata DBs (primary plus
    SLEDGE_EXTRA_METADATA_CONNS), deduped like the parallel-savings windows.
    Returns None when no metadata DB is reachable.
    """
    import os
    try:
        import psycopg2
        from hammer.vlsi import pd_store
    except Exception:
        return None
    sources = []
    try:
        primary = pd_store.airflow_metadata_conn_settings()
        if primary:
            sources.append(primary)
    except Exception:
        pass
    for uri in (os.environ.get("SLEDGE_EXTRA_METADATA_CONNS") or "").split(","):
        uri = uri.strip()
        if uri:
            try:
                sources.append(pd_store._parse_conn_uri(uri))
            except Exception:
                pass
    if not sources:
        return None
    seen: set = set()
    total = 0.0
    got_any = False
    for settings in sources:
        try:
            conn = psycopg2.connect(connect_timeout=20, **settings)
        except Exception:
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT run_id, task_id, "
                    "EXTRACT(EPOCH FROM (end_date - start_date)) "
                    "FROM task_instance WHERE state = 'failed' "
                    "AND task_id LIKE 'module%%' "
                    "AND start_date IS NOT NULL AND end_date IS NOT NULL "
                    "AND EXTRACT(EPOCH FROM (end_date - start_date)) >= 60")
                for run_id, task_id, sec in cur.fetchall():
                    key = (run_id, task_id, round(sec))
                    if key in seen:
                        continue
                    seen.add(key)
                    total += float(sec)
            got_any = True
        except Exception:
            pass
        finally:
            conn.close()
    return total if got_any else None


def format_savings_report(events: List[Dict[str, Any]],
                          group_by: str = "stage",
                          source: str = "") -> str:
    """Render a human-readable cross-run time-saved report."""
    agg = aggregate_savings(events, group_by=group_by)
    par = parallel_savings(events)
    par_wall = par["parallel_wall_s"]
    t = agg["totals"]
    if t["events"] == 0:
        return "(no cache events found)"

    sledge_wall = t["cache_wall"] + t["depsh_wall"] + t["resume_wall"] + par_wall
    sledge_cpu = t["cache_cpu"] + t["depsh_cpu"] + t["resume_cpu"]
    total_saved_wall = sledge_wall + t["deplegacy_wall"]
    total_saved_cpu = sledge_cpu + t["deplegacy_cpu"]
    decided = t["hits"] + t["misses"]
    hit_rate = (100.0 * t["hits"] / decided) if decided else 0.0

    label = {"stage": "stage", "stage_tag": "stage", "dag": "dag",
             "dag_id": "dag", "design": "design", "project": "project",
             "module": "module", "run": "run", "run_id": "run",
             "none": "all"}.get(group_by, group_by)

    lines: List[str] = []
    src = f"  (source: {source})" if source else ""
    lines.append(f"PD cache time saved{src}")
    if agg["first_ts"] and agg["last_ts"]:
        span = (f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(agg['first_ts']))}"
                f"  ->  {time.strftime('%Y-%m-%d %H:%M', time.localtime(agg['last_ts']))}")
        lines.append(f"  span: {span}   runs: {agg['n_runs']}   dags: {agg['n_dags']}")
    # Three savings columns, by what legacy hammer could have done:
    #   cache saved  - blob-cache restores; legacy would have run the tool.
    #   depchk saved - dep-check skips where legacy make's mtime rule would
    #                  have rerun (a config was touched but the settings that
    #                  matter didn't change). SledgeHammer-only, like cache.
    #   legacy skip  - dep-check skips make would also have skipped (or the
    #                  make verdict is unknown). No SledgeHammer credit.
    header = (f"{label:<30}  {'events':>6}  {'hits':>5}  {'miss':>5}  "
              f"{'cache saved':>12}  {'depchk saved':>12}  {'legacy skip':>12}  "
              f"{'lgcy skip cpu':>13}  {'wall ran':>12}  {'cpu ran':>12}")
    lines.append("")
    lines.append(header)
    lines.append("-" * len(header))
    for gk in sorted(agg["groups"], key=lambda k: -(agg["groups"][k]["cache_wall"]
                                                     + agg["groups"][k]["depsh_wall"])):
        b = agg["groups"][gk]
        lines.append(
            f"{_short(gk, 30):<30}  {b['events']:>6}  {b['hits']:>5}  {b['misses']:>5}  "
            f"{_format_duration(b['cache_wall']):>12}  {_format_duration(b['depsh_wall']):>12}  "
            f"{_format_duration(b['deplegacy_wall']):>12}  "
            f"{_format_duration(b['deplegacy_cpu']):>13}  "
            f"{_format_duration(b['ran_wall']):>12}  "
            f"{_format_duration(b['ran_cpu']):>12}")
    lines.append("-" * len(header))

    def _pair(wall: float, cpu: float) -> str:
        return f"wall {_format_duration(wall):<12}  cpu {_format_duration(cpu):<12}"

    lines.append(f"  Saved by cache:                     {_pair(t['cache_wall'], t['cache_cpu'])}")
    lines.append(f"  Saved by dep-check (make would rerun): {_pair(t['depsh_wall'], t['depsh_cpu'])}")
    if t["resume_wall"] or t["resume_cpu"]:
        lines.append(f"  Saved by substep resume:            {_pair(t['resume_wall'], t['resume_cpu'])}")
    if par_wall:
        lines.append(f"  Saved by parallel flow execution:   {_pair(par_wall, 0.0)}")
    lines.append(f"  SLEDGEHAMMER TIME SAVED:            {_pair(sledge_wall, sledge_cpu)}")
    lines.append(f"  Legacy-equivalent skips (no credit): {_pair(t['deplegacy_wall'], t['deplegacy_cpu'])}")
    lines.append(f"  Total skipped work (incl. legacy):  {_pair(total_saved_wall, total_saved_cpu)}")
    # ran_wall is the sum of every stage's own tool time. Stages that ran side
    # by side overlapped on the clock, so the wall time a person waited is that
    # sum minus the overlap the parallel bucket measured. Legacy would have run
    # the same stages back to back, so the sum itself is the sequential
    # baseline; the overlap must not be added on top of it a second time.
    elapsed_wall = max(0.0, t["ran_wall"] - par_wall)
    lines.append(f"  Tool time executed (stage sum):     {_pair(t['ran_wall'], t['ran_cpu'])}")
    if par_wall:
        lines.append(f"  Wall clock elapsed (after overlap): wall {_format_duration(elapsed_wall)}")
    failed_s = airflow_failed_time()
    if failed_s:
        lines.append(f"  Failed-run time (not in any bucket): wall {_format_duration(failed_s)}"
                     f"  (from airflow; legacy would burn these failures too)")
    # Turnaround-time efficiency: the share of the legacy-equivalent schedule
    # that sledgehammer removed. The baseline is the stage sum plus the work
    # the cache, dep-check and resume never ran, minus skips legacy would have
    # made anyway; the actual is the elapsed wall clock.
    tat_baseline = t["ran_wall"] + (sledge_wall - par_wall) - t["deplegacy_wall"]
    if tat_baseline > 0:
        tat = 1.0 - (elapsed_wall / tat_baseline)
        lines.append(f"  TAT efficiency improvement:         {tat * 100:.1f}%"
                     f"  ({_format_duration(elapsed_wall)} elapsed vs {_format_duration(tat_baseline)} legacy-equivalent)")
    # Same ratio on CPU time. Parallel execution contributes nothing here --
    # concurrency shortens the schedule without removing work -- so this
    # measures only the compute the cache and dep-check never spent.
    ctat_baseline = t["ran_cpu"] + sledge_cpu - t["deplegacy_cpu"]
    if ctat_baseline > 0:
        ctat = 1.0 - (t["ran_cpu"] / ctat_baseline)
        lines.append(f"  Compute TAT efficiency improvement: {ctat * 100:.1f}%"
                     f"  ({_format_duration(t['ran_cpu'])} cpu ran vs {_format_duration(ctat_baseline)} legacy-equivalent)")
    lines.append(f"  Cache hits / misses:                {t['hits']} hit, {t['misses']} miss "
                 f"({hit_rate:.0f}% hit rate over {decided} decided stages)")
    if t["unquantified"]:
        lines.append(f"  Note: {t['unquantified']} hit(s) had no recorded duration "
                     f"(blob predates duration tracking) and are excluded from the saved total.")
    return "\n".join(lines)


def _short(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 3] + "..."


def exclude_depcheck_skips(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop SKIP_LOCAL events so a report counts only cache-delivered savings.

    For strict SledgeHammer-vs-legacy accounting: HIT and SKIP_RESTORED need the
    Postgres blob cache and have no legacy equivalent, but a SKIP_LOCAL stage
    would often have been skipped by make's timestamp dependencies in a legacy
    flow too. Excluding SKIP_LOCAL gives the conservative headline number.
    """
    return [e for e in events if e.get("outcome") != "SKIP_LOCAL"]


def _jsonl_matches(ev: Dict[str, Any], *, since: Optional[float], until: Optional[float],
                   dag: Optional[str], design: Optional[str], stage: Optional[str],
                   user: Optional[str], project: Optional[str] = None,
                   module: Optional[str] = None) -> bool:
    """Apply the same filters fetch_cache_events applies in SQL, to a JSONL event."""
    ts = ev.get("ts")
    if since is not None and isinstance(ts, (int, float)) and ts < since:
        return False
    if until is not None and isinstance(ts, (int, float)) and ts > until:
        return False
    if dag and dag.lower() not in str(ev.get("dag_id") or "").lower():
        return False
    if design and design.lower() not in str(ev.get("design") or "").lower():
        return False
    if stage and stage.lower() not in str(ev.get("stage_tag") or "").lower():
        return False
    if project and project.lower() not in str(ev.get("project") or "").lower():
        return False
    if module and module.lower() not in str(ev.get("module") or "").lower():
        return False
    if user:
        u = user.lower()
        if u not in str(ev.get("triggering_user") or "").lower() \
           and u not in str(ev.get("owner") or "").lower():
            return False
    return True


def collect_savings_events(
    *,
    source: str = "auto",
    since: Optional[float] = None,
    until: Optional[float] = None,
    dag: Optional[str] = None,
    design: Optional[str] = None,
    stage: Optional[str] = None,
    user: Optional[str] = None,
    project: Optional[str] = None,
    module: Optional[str] = None,
    limit: Optional[int] = None,
    events_dir: Optional[str] = None,
):
    """Gather cache events for the time-saved report. Returns (events, label).

    ``source`` is one of:
      * ``auto`` (default) -- read the durable Postgres ledger; if it's
        unreachable or empty, fall back to the on-disk JSONL files.
      * ``db``   -- Postgres ledger only (raises if unreachable).
      * ``jsonl``-- on-disk JSONL files only (no DB needed).
      * ``both`` -- concatenate DB + JSONL (may double-count a run that is in
        the ledger AND still has an un-cleared JSONL file).
    """
    def _db():
        return pd_store.fetch_cache_events(
            since=since, until=until, dag=dag, design=design,
            stage=stage, user=user, project=project, module=module, limit=limit)

    def _jsonl():
        evs = iter_jsonl_cache_events(events_dir)
        return [e for e in evs if _jsonl_matches(
            e, since=since, until=until, dag=dag, design=design,
            stage=stage, user=user, project=project, module=module)]

    if source == "jsonl":
        return _jsonl(), "jsonl files"
    if source == "db":
        return _db(), "postgres ledger"
    if source == "both":
        try:
            db, label = _db(), "postgres ledger + jsonl files"
        except Exception as e:
            db, label = [], f"jsonl files (DB unavailable: {e})"
        return db + _jsonl(), label
    # auto
    try:
        db = _db()
    except Exception as e:
        return _jsonl(), f"jsonl files (DB unavailable: {e})"
    if db:
        return db, "postgres ledger"
    return _jsonl(), "jsonl files (ledger empty)"


# ---------------------------------------------------------------------------
# Failed-stage accounting
#
# Deliberately separate from the savings path: nothing here is called by
# pd_cache, and none of it feeds the savings totals or the TAT figures, which
# describe work that completed. Call record_stage_failure() from wherever a
# failure is observed, and format_failure_report() to read the tally back.
#
# Caveat worth keeping in mind when reading the numbers: a stale config or
# stale collateral can fail an entire campaign, so a large failure total says
# something about the design's health, not about the cache.
# ---------------------------------------------------------------------------


def record_stage_failure(
    stage_tag: str,
    tool_seconds: Optional[float] = None,
    tool_cpu_seconds: Optional[float] = None,
    module: Optional[str] = None,
    enabled: bool = True,
) -> None:
    """Record tool time spent on a stage that failed, as a MISS_FAIL event."""
    record_event(
        stage_tag, "MISS_FAIL",
        tool_seconds=tool_seconds,
        tool_cpu_seconds=tool_cpu_seconds,
        module=module,
        enabled=enabled,
    )


def aggregate_failures(events: List[Dict[str, Any]],
                       group_by: str = "stage") -> Dict[str, Any]:
    """Failure totals only: {failures, failed_wall, failed_cpu} plus groups."""
    agg = aggregate_savings(events, group_by=group_by)
    keep = ("failures", "failed_wall", "failed_cpu")
    return {
        "totals": {k: agg["totals"].get(k, 0) for k in keep},
        "groups": {g: {k: b.get(k, 0) for k in keep}
                   for g, b in agg.get("groups", {}).items()
                   if b.get("failures")},
    }


def format_failure_report(events: List[Dict[str, Any]],
                          group_by: str = "stage") -> str:
    """Standalone report of tool time lost to failed stages."""
    agg = aggregate_savings(events, group_by=group_by)
    t = agg["totals"]
    failures = t.get("failures", 0)
    failed_wall = t.get("failed_wall", 0.0)
    failed_cpu = t.get("failed_cpu", 0.0)
    lines = ["FAILED STAGES (separate from the savings report)"]
    if not failures and not failed_wall:
        lines.append("  No failed stages recorded.")
        return "\n".join(lines)
    lines.append(f"  Stages that failed:          {failures}")
    lines.append(f"  Tool time spent on failures: wall {_format_duration(failed_wall):<12}"
                 f"  cpu {_format_duration(failed_cpu)}")
    burn = t.get("ran_wall", 0.0) + failed_wall
    if burn > 0:
        lines.append(f"  Share of tool time wasted:   {failed_wall / burn * 100:.1f}%"
                     f"  ({_format_duration(failed_wall)} failed"
                     f" / {_format_duration(burn)} total tool time)")
    groups = {g: b for g, b in agg.get("groups", {}).items() if b.get("failures")}
    if groups:
        lines.append("")
        lines.append(f"  {'group':<30} {'failures':>9} {'wall':>12} {'cpu':>12}")
        for g, b in sorted(groups.items(), key=lambda kv: -kv[1].get("failed_wall", 0.0)):
            lines.append(f"  {str(g)[:30]:<30} {b.get('failures', 0):>9} "
                         f"{_format_duration(b.get('failed_wall', 0.0)):>12} "
                         f"{_format_duration(b.get('failed_cpu', 0.0)):>12}")
    return "\n".join(lines)
