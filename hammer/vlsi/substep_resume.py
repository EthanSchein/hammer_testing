"""
Automatic sub-step resume for partial tool runs.

Genus and innovus write a checkpoint database at every step boundary
(``write_db pre_<step>``) as the generated script executes. Upstream hammer
can resume from one, but only when the user diagnoses which step failed and
passes ``--from_step`` by hand. This module automates that: when a stage is
about to run and the previous attempt died partway (or was stopped with
``--to_step``), pick the newest checkpoint the tool CONFIRMED writing, check
the inputs haven't changed since that attempt, and resume from there instead
of starting over.

Trust rules, in order:

  * A checkpoint counts only if the tool's log confirms the write finished
    (genus prints "Finished exporting design database to file 'pre_X'"), the
    file is still on disk, and it has not been modified since that log was
    last written. Newest confirmed wins, not newest mtime, so a write the
    tool died inside of is never trusted.
  * The marker file's stage key (the same config+RTL fingerprint the PD cache
    uses) must match the current inputs. A mismatch means the checkpoints
    describe a different design state: they are deleted and the run starts
    from scratch. When the key cannot be computed or the marker file cannot
    be opened, nothing is resumed and nothing is deleted.
  * A resume that makes no progress burns its checkpoint: the next attempt
    uses the next older confirmed one, and when the ladder is exhausted the
    run starts from scratch. No resume loops on a corrupt database.

Explicit flow control (``--from_step`` and friends) and ``--force`` always
win over auto-resume. Gate with ``vlsi.substep_resume.enabled`` or
``HAMMER_SUBSTEP_RESUME=0`` (default on).
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from hammer.vlsi import legacy_hammer

MARKER_NAME = ".substep_resume.json"
ENABLE_ENV_VAR = "HAMMER_SUBSTEP_RESUME"
ENABLE_SETTING_KEY = "vlsi.substep_resume.enabled"
DB_ENABLE_ENV_VAR = "HAMMER_DB_CHECKPOINTS"
DB_ENABLE_SETTING_KEY = "vlsi.substep_resume.db_checkpoints"

# Tool-confirmed checkpoint write, per tool log.
#   genus prints an explicit completion line per export.
#   innovus prints when a write STARTS; completion is inferred (see
#   confirmed_checkpoints): a later write starting means the earlier one
#   finished, and the newest write is trusted only if the `latest` symlink
#   (repointed by the script right after write_db returns) points at it.
_CONFIRM_RE = {
    "genus.log": re.compile(r"Finished exporting design database to file 'pre_([A-Za-z0-9_]+)'"),
    "innovus.log": re.compile(r"Writing Binary DB to pre_([A-Za-z0-9_]+)/"),
}
_INFER_COMPLETION = {"innovus.log"}

# Latest step a resume may start from, per tool. Both genus and innovus
# fill_outputs require write_regs and the write steps after it to have run in
# the current invocation (ran_write_regs / ran_write_design / ran_write_ilm),
# so a later resume would finish the tool but fail hammer's bookkeeping. The
# steps from write_regs on are cheap.
_RESUME_CEILING = {
    "genus.log": "write_regs",
    "innovus.log": "write_regs",
}


def is_enabled(driver: Optional[Any] = None) -> bool:
    """True unless switched off by env var, config or build_system hammer. Defaults to on."""
    if legacy_hammer.is_legacy_hammer(driver):
        return False
    env = os.environ.get(ENABLE_ENV_VAR)
    if env is not None:
        return env.strip().lower() not in ("", "0", "false", "no", "off")
    if driver is not None:
        from hammer.vlsi import sledge_settings
        return sledge_settings.flag(driver, ENABLE_SETTING_KEY, True)
    return True


def _newest_log(rundir: str, log_name: str) -> Optional[str]:
    """The most recent tool log, accounting for Cadence-style rotation
    (genus.log, genus.log1, genus.log2, ...)."""
    cands = glob.glob(os.path.join(rundir, log_name + "*"))
    cands = [c for c in cands if re.fullmatch(re.escape(log_name) + r"\d*", os.path.basename(c))]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def _checkpoint_present(rundir: str, step: str) -> bool:
    """A checkpoint exists with content: a non-empty file (genus writes a
    single db file) or a non-empty directory (innovus writes a db dir)."""
    p = os.path.join(rundir, "pre_" + step)
    try:
        if os.path.isdir(p):
            return bool(os.listdir(p))
        return os.path.getsize(p) > 0
    except OSError:
        return False


def confirmed_checkpoints(rundir: str, log_name: str = "genus.log",
                          since: Optional[float] = None) -> List[str]:
    """Step names whose pre_<step> checkpoint the tool confirmed writing, in
    log order, filtered to checkpoints still present with content.

    For tools that only announce the START of a db write (innovus), completion
    is inferred: every write except the last is complete because a later write
    began after it; the last one counts only if the `latest` symlink points at
    it, since the generated script repoints `latest` immediately after
    write_db returns.

    Confirmations are collected across ALL log rotations, not just the newest:
    a resume attempt that dies loading its checkpoint confirms nothing in its
    own log, but the checkpoints proven by earlier attempts are still on disk
    and still valid (a stage-key change deletes them, so presence plus the
    marker key check is sufficient). A log vouches only for checkpoints no
    newer than itself (or than ``latest``, for the write that link confirms).
    The result is ordered by checkpoint mtime, oldest first, which is
    completion order. With ``since``, only logs and checkpoints modified
    after that time count."""
    pat = _CONFIRM_RE.get(log_name)
    if pat is None:
        return []
    cands = glob.glob(os.path.join(rundir, log_name + "*"))
    cands = [c for c in cands
             if re.fullmatch(re.escape(log_name) + r"\d*", os.path.basename(c))]
    if not cands:
        return []
    target = None
    latest_mtime = None
    try:
        target = os.path.basename(os.readlink(os.path.join(rundir, "latest")))
        latest_mtime = os.lstat(os.path.join(rundir, "latest")).st_mtime
    except OSError:
        pass
    def _log_mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0
    announced: List[str] = []  # first-seen announced order, oldest log first
    vouched: Dict[str, float] = {}
    for log in sorted(cands, key=_log_mtime):
        log_mtime = _log_mtime(log)
        if since is not None and log_mtime <= since:
            continue
        names: List[str] = []
        try:
            with open(log, errors="ignore") as fh:
                for line in fh:
                    m = pat.search(line)
                    if m:
                        names.append(m.group(1))
        except OSError:
            continue
        last_vouch = log_mtime
        # the drop-the-last-announced rule applies per attempt (per log)
        if names and log_name in _INFER_COMPLETION:
            if target != "pre_" + names[-1]:
                names = names[:-1]
            elif latest_mtime is not None:
                last_vouch = max(log_mtime, latest_mtime)
        for i, n in enumerate(names):
            if n == "dummy_step":
                continue
            if n not in announced:
                announced.append(n)
            v = last_vouch if i == len(names) - 1 else log_mtime
            vouched[n] = max(vouched.get(n, v), v)
    present = [n for n in announced
               if _checkpoint_present(rundir, n) and _ck_mtime(rundir, n) <= vouched[n]]
    if since is not None:
        present = [n for n in present if _ck_mtime(rundir, n) > since]

    # completion order: checkpoint mtime, announced order as the tiebreak
    # (synthetic same-second writes, coarse filesystems)
    def _ck_key(n: str):
        try:
            mt = os.path.getmtime(os.path.join(rundir, "pre_" + n))
        except OSError:
            mt = 0.0
        return (mt, announced.index(n))
    return sorted(present, key=_ck_key)


def _ck_mtime(rundir: str, step: str) -> float:
    try:
        return os.path.getmtime(os.path.join(rundir, "pre_" + step))
    except OSError:
        return 0.0


def _made_progress(rundir: str, log_name: str) -> bool:
    """Whether the last attempt confirmed a new checkpoint, timed against the marker it wrote just before running."""
    try:
        started = os.path.getmtime(_marker_path(rundir))
    except OSError:
        return False
    return bool(confirmed_checkpoints(rundir, log_name, since=started))


def announced_order(rundir: str, log_name: str = "genus.log") -> List[str]:
    """Step names in first-announced order across all log rotations.

    Once any attempt announced a step's boundary, its position here reflects
    true tool step order (a full run announces everything in order; partial
    runs re-announce prefixes). Used for the resume ceiling, where mtime
    order is wrong for mixed-generation rundirs: an old completed run's
    pre_write_regs has an EARLIER mtime than a fresh attempt's pre_clock_tree
    even though write_regs is a later step."""
    announced: List[str] = []
    for names in _log_announcements(rundir, log_name):
        for n in names:
            if n not in announced:
                announced.append(n)
    return announced


def _log_announcements(rundir: str, log_name: str) -> List[List[str]]:
    """Each log rotation's announced step names in log order, oldest log first."""
    pat = _CONFIRM_RE.get(log_name)
    if pat is None:
        return []
    cands = glob.glob(os.path.join(rundir, log_name + "*"))
    cands = [c for c in cands
             if re.fullmatch(re.escape(log_name) + r"\d*", os.path.basename(c))]

    def _log_mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0
    logs: List[List[str]] = []
    for log in sorted(cands, key=_log_mtime):
        names: List[str] = []
        try:
            with open(log, errors="ignore") as fh:
                for line in fh:
                    m = pat.search(line)
                    if m:
                        names.append(m.group(1))
        except OSError:
            pass
        logs.append(names)
    return logs



def _marker_path(rundir: str) -> str:
    return os.path.join(rundir, MARKER_NAME)


def read_marker(rundir: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_marker_path(rundir)) as fh:
            return json.load(fh)
    except Exception:
        return None


def _load_marker(rundir: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """The marker and its state: "ok", "absent", "unreadable" (an I/O error)
    or "corrupt" (read, but not a JSON object)."""
    try:
        with open(_marker_path(rundir)) as fh:
            text = fh.read()
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None
    try:
        data = json.loads(text)
    except ValueError:
        return "corrupt", None
    if not isinstance(data, dict):
        return "corrupt", None
    return "ok", data


def write_marker(rundir: str, data: Dict[str, Any]) -> None:
    """Replace the marker atomically, so a reader never sees a partial one."""
    tmp = None
    try:
        os.makedirs(rundir, exist_ok=True)
        tmp = f"{_marker_path(rundir)}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, _marker_path(rundir))
        tmp = None
    except Exception:
        # advisory state only; never fail the run over it
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _checkpoint_paths(rundir: str) -> List[str]:
    return [p for p in glob.glob(os.path.join(rundir, "pre_*"))
            if re.fullmatch(r"pre_[A-Za-z0-9_]+", os.path.basename(p))]


def _remove_checkpoint(path: str) -> None:
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.unlink(path)
    except OSError:
        pass


def clean_checkpoints(rundir: str) -> None:
    """Remove stale checkpoint dbs and the marker (inputs changed)."""
    for p in _checkpoint_paths(rundir):
        _remove_checkpoint(p)
    try:
        os.unlink(_marker_path(rundir))
    except OSError:
        pass


def _drop_foreign_checkpoints(rundir: str, resumed_from: Optional[str],
                              start_step: Optional[str], start_inclusive: bool,
                              log_name: str, step_names: Sequence[str] = ()) -> None:
    """Delete the checkpoints other inputs left, except the one this attempt
    starts from and those of earlier steps."""
    keep: Set[str] = set()
    if resumed_from is not None:
        keep.add(resumed_from)
    elif start_step is not None:
        logs = _log_announcements(rundir, log_name)
        starts = [start_step]
        if not start_inclusive:
            # the run reads the checkpoint of the step after start_step
            after = [names[names.index(start_step) + 1] for names in logs
                     if start_step in names[:-1]]
            static = list(step_names)
            if not after and start_step in static[:-1]:
                after = [static[static.index(start_step) + 1]]
            starts += after
        for s in starts:
            keep.add(s)
            # one log is one attempt, so its order is true step order
            for names in logs:
                if s in names:
                    keep.update(names[: names.index(s)])
    for p in _checkpoint_paths(rundir):
        if os.path.basename(p)[len("pre_"):] not in keep:
            _remove_checkpoint(p)


def _stage_key(driver: Any, stage_tag: str) -> Optional[str]:
    try:
        from hammer.vlsi.pd_cache import _build_cache_key
        return _build_cache_key(driver, stage_tag)
    except Exception:
        return None


def _db_enabled(driver: Optional[Any] = None) -> bool:
    if legacy_hammer.is_legacy_hammer(driver):
        return False
    env = os.environ.get(DB_ENABLE_ENV_VAR)
    if env is not None:
        return env.strip().lower() not in ("", "0", "false", "no", "off")
    if driver is not None:
        from hammer.vlsi import sledge_settings
        return sledge_settings.flag(driver, DB_ENABLE_SETTING_KEY, True)
    return True


def _provenance(driver: Any) -> dict:
    project = os.environ.get("HAMMER_PD_PROJECT")
    if not project:
        try:
            from hammer.vlsi import sledge_settings
            project = sledge_settings.text(driver, "vlsi.pd_cache.project")
        except Exception:
            project = None
    return {
        "triggering_user": os.environ.get("HAMMER_AIRFLOW_TRIGGERING_USER"),
        "dag_id": os.environ.get("HAMMER_AIRFLOW_DAG_ID"),
        "dag_run_id": os.environ.get("HAMMER_AIRFLOW_RUN_ID"),
        "workspace": os.environ.get("HAMMER_AIRFLOW_WORKSPACE"),
        "design": os.environ.get("HAMMER_AIRFLOW_DESIGN"),
        "project": project,
    }


def _log_info(driver: Any, msg: str) -> None:
    try:
        driver.log.info(msg)
    except Exception:
        pass


def _cpu_saved(rundir: str, first_step: str, resume_step: str):
    """CPU seconds the skipped steps burned, from the checkpoint CPU stamps.

    pd_cache's streamer writes pre_<step>.cpustamp beside each checkpoint as
    the tool confirms it, holding cumulative tool CPU at that moment. The
    difference between the first and the resume point is what a resume avoids
    re-running. Returns None when the stamps are absent (a run from before
    stamping, or a stage with no streamer), which the ledger records as
    unknown rather than zero.
    """
    def read(step):
        try:
            return float(open(os.path.join(rundir, f"pre_{step}.cpustamp")).read().strip())
        except (OSError, ValueError):
            return None
    a, b = read(first_step), read(resume_step)
    if a is None or b is None:
        return None
    return max(0.0, b - a)


def push_checkpoint_db(driver: Any, stage_tag: str, rundir: str,
                       log_name: str = "genus.log",
                       module: Optional[str] = None,
                       skip: Optional[str] = None) -> Optional[str]:
    """After a failed or paused run, upload the newest trusted checkpoint so
    another machine or a fresh checkout can resume this stage. Never raises.
    Returns the step it is done with: pushed, equal to skip (not uploaded
    again), or refused for good (the stage key moved, or the store failed for
    a reason other than I/O or the database connection, such as an oversized
    checkpoint or a missing privilege). Returns None when it tried nothing or
    hit an I/O or connection failure, so a later call may succeed."""
    try:
        if not is_enabled(driver) or not _db_enabled(driver):
            return None
        key = _stage_key(driver, stage_tag)
        if key is None:
            return None
        state, marker = _load_marker(rundir)
        if state == "unreadable":
            return None
        confirmed = confirmed_checkpoints(rundir, log_name)
        # a checkpoint past the resume ceiling can never seed a resume;
        # clamp by announced (step) order, not mtime position
        ceiling = _RESUME_CEILING.get(log_name)
        announced = announced_order(rundir, log_name)
        if ceiling and ceiling in announced:
            allowed = set(announced[: announced.index(ceiling) + 1])
            confirmed = [c for c in confirmed if c in allowed]
        if not confirmed:
            return None
        step = confirmed[-1]
        if step == skip or marker is None or marker.get("stage_key") != key:
            return step
        path = os.path.join(rundir, "pre_" + step)
        # same floor measurement the local resume uses (checkpoint mtime span),
        # carried with the row so a cross-machine resume credits the skipped
        # steps' measured time in the ledger too
        saved = None
        try:
            t0 = os.path.getmtime(os.path.join(rundir, "pre_" + confirmed[0]))
            saved = max(0.0, os.path.getmtime(path) - t0)
        except OSError:
            pass
        from hammer.vlsi import pd_store
        from pathlib import Path
        try:
            size = pd_store.store_checkpoint(key, stage_tag, step, Path(path),
                                             saved_seconds=saved,
                                             saved_cpu_seconds=_cpu_saved(
                                                 rundir, confirmed[0], step),
                                             module=module,
                                             **_provenance(driver))
        except Exception as exc:
            # say why the push was skipped (an oversized checkpoint is the
            # common case) instead of vanishing into the outer catch-all
            _log_info(driver, f"Checkpoint push skipped: {exc}")
            db = pd_store.psycopg2
            retry = (OSError,) if db is None else (OSError, db.OperationalError, db.InterfaceError)
            return None if isinstance(exc, retry) else step
        _log_info(driver, f"Pushed checkpoint pre_{step} ({size / 1e6:.1f} MB "
                          "compressed) to the database for cross-machine resume.")
        return step
    except Exception:
        if os.environ.get("HAMMER_CHECKPOINT_DEBUG"):
            import traceback
            traceback.print_exc()
        return None


def clear_checkpoint_db(driver: Any, stage_tag: str,
                        rundir: Optional[str] = None) -> int:
    """Drop this stage's database checkpoints once it commits successfully.

    Clears the current stage key, plus every key in this rundir's attempt
    lineage (the marker's key_history): the usual fix-the-config-then-succeed
    flow sweeps the rows its own broken predecessors pushed, while a teammate
    debugging a different config of the same design keeps their row (their
    keys are not in this rundir's lineage). Never raises; returns rows
    deleted."""
    try:
        if not _db_enabled(driver):
            return 0
        key = _stage_key(driver, stage_tag)
        if key is None:
            return 0
        keys = [key]
        if rundir:
            marker = read_marker(rundir)
            if marker:
                keys += [k for k in marker.get("key_history", []) if k not in keys]
        from hammer.vlsi import pd_store
        n = 0
        for k in keys:
            n += pd_store.delete_checkpoints(stage_key=k)
        if n:
            _log_info(driver, f"Cleared {n} database checkpoint(s) for the completed stage.")
        return n
    except Exception:
        if os.environ.get("HAMMER_CHECKPOINT_DEBUG"):
            import traceback
            traceback.print_exc()
        return 0


def _db_fallback_plan(driver: Any, stage_tag: str, rundir: str,
                      skip: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """No usable local checkpoint: try the database. Downloads the newest
    checkpoint stored for this exact stage key and materializes it into the
    rundir. The row's existence is its trust: it was log-confirmed and
    key-matched when pushed. Never raises."""
    try:
        if not _db_enabled(driver):
            return None
        key = _stage_key(driver, stage_tag)
        if key is None:
            return None
        from hammer.vlsi import pd_store
        rec = pd_store.fetch_checkpoint(key)
        if rec is None:
            return None
        step = rec["step"]
        if step in skip:
            return None
        # anti-loop: if the previous attempt already resumed from this very
        # checkpoint and confirmed nothing new, don't fetch it again
        marker = read_marker(rundir)
        if marker is not None and marker.get("stage_key") == key:
            if step in marker.get("burned", []) or step == marker.get("resumed_from"):
                return None
        from pathlib import Path
        pd_store.materialize_checkpoint(rec, Path(rundir))
        _log_info(driver, f"Fetched checkpoint pre_{step} "
                          f"({rec['size_bytes'] / 1e6:.1f} MB) from the database.")
        return {"step": step, "saved_seconds": rec.get("saved_seconds"),
                "saved_cpu_seconds": rec.get("saved_cpu_seconds"),
                "key": key, "source": "database"}
    except Exception:
        return None


def ensure_step_checkpoint(driver: Any, stage_tag: str, rundir: str, step_name: str,
                           log_name: str = "genus.log"):
    """Validate a user-chosen start step: its pre_<step> checkpoint must exist
    locally or be fetchable from the database. Returns (ok, detail); on
    failure the detail lists what IS available."""
    try:
        if _checkpoint_present(rundir, step_name):
            return True, "local"
        key = _stage_key(driver, stage_tag)
        if _db_enabled(driver) and key is not None:
            from hammer.vlsi import pd_store
            rec = pd_store.fetch_checkpoint(key, step=step_name)
            if rec is not None:
                from pathlib import Path
                pd_store.materialize_checkpoint(rec, Path(rundir))
                return True, "database"
        local = sorted(os.path.basename(p)[len("pre_"):]
                       for p in glob.glob(os.path.join(rundir, "pre_*"))
                       if _checkpoint_present(rundir, os.path.basename(p)[len("pre_"):]))
        indb = []
        if _db_enabled(driver) and key is not None:
            try:
                from hammer.vlsi import pd_store
                indb = [r["step"] for r in pd_store.find_checkpoints(stage_key=key)]
            except Exception:
                indb = []
        return False, (f"no checkpoint exists for step '{step_name}'. "
                       f"Available locally: {', '.join(local) or 'none'}. "
                       f"Available in the database for these inputs: "
                       f"{', '.join(indb) or 'none'}.")
    except Exception as exc:
        return False, f"checkpoint lookup failed: {exc}"


def plan_resume(driver: Any, stage_tag: str, rundir: str, output_filename: str,
                log_name: str = "genus.log") -> Optional[Dict[str, Any]]:
    """Decide whether the coming run can resume from a checkpoint.

    Returns ``{"step": <name>, "saved_seconds": <float|None>, "key": <key>}``
    when a trusted checkpoint exists for unchanged inputs, else None (run from
    scratch). Never raises; any doubt means None.
    """
    try:
        if not is_enabled(driver):
            return None
        confirmed = confirmed_checkpoints(rundir, log_name)
        if not confirmed:
            return _db_fallback_plan(driver, stage_tag, rundir)
        # a key or marker we cannot read proves nothing either way, and the
        # failure may be transient: run from scratch but keep the checkpoints
        key = _stage_key(driver, stage_tag)
        if key is None:
            return None
        state, marker = _load_marker(rundir)
        if state == "unreadable":
            return None
        if marker is None or marker.get("stage_key") != key:
            # checkpoints came from different inputs (or we can't prove
            # otherwise): they are worthless and could mislead a later run.
            # The database may still hold one pushed for the CURRENT inputs
            # (e.g. by another machine), so check it before going scratch.
            clean_checkpoints(rundir)
            return _db_fallback_plan(driver, stage_tag, rundir)
        # A present output json only means "completed" if it is newer than the
        # newest confirmed checkpoint. An older one is a leftover from an
        # earlier successful run that a later (killed) attempt superseded --
        # common when re-running a stage in a long-lived build dir.
        out_path = os.path.join(rundir, output_filename)
        if os.path.exists(out_path):
            try:
                newest_ck = max(os.path.getmtime(os.path.join(rundir, "pre_" + c))
                                for c in confirmed)
                if os.path.getmtime(out_path) > newest_ck:
                    return None  # genuinely completed; dep-check/cache own this
            except OSError:
                return None
        burned = list(marker.get("burned", []))
        # a resume that produced no new confirmed checkpoint made no progress:
        # burn that rung so we step down the ladder instead of looping
        last = marker.get("resumed_from")
        if last is not None and last not in burned and not _made_progress(rundir, log_name):
            burned.append(last)
        candidates = [c for c in confirmed if c not in burned]
        # The ceiling is positional in ANNOUNCED (step) order, which survives
        # both a burned ceiling step and mixed-generation rundirs, where an
        # old run's late-step checkpoint has an earlier mtime than a fresh
        # attempt's early-step one.
        ceiling = _RESUME_CEILING.get(log_name)
        announced = announced_order(rundir, log_name)
        if ceiling and ceiling in announced:
            allowed = set(announced[: announced.index(ceiling) + 1])
            candidates = [c for c in candidates if c in allowed]
        if not candidates:
            # the database still holds the burned rungs (pushed after each failure); drop them so they are not fetched again
            if burned and _db_enabled(driver):
                try:
                    from hammer.vlsi import pd_store
                    for s in burned:
                        pd_store.delete_checkpoints(stage_key=key, step=s)
                except Exception:
                    pass
            clean_checkpoints(rundir)
            return _db_fallback_plan(driver, stage_tag, rundir, skip=burned)
        step = candidates[-1]
        # measured time of the completed steps, from checkpoint mtimes: the
        # span from the first confirmed boundary to the resume point. This is
        # a floor (the first step's own time isn't bracketed by checkpoints).
        saved: Optional[float] = None
        try:
            t0 = os.path.getmtime(os.path.join(rundir, "pre_" + confirmed[0]))
            t1 = os.path.getmtime(os.path.join(rundir, "pre_" + step))
            saved = max(0.0, t1 - t0)
        except OSError:
            pass
        if burned != marker.get("burned", []):
            marker["burned"] = burned
            write_marker(rundir, marker)
        return {"step": step, "saved_seconds": saved,
                "saved_cpu_seconds": _cpu_saved(rundir, confirmed[0], step),
                "key": key}
    except Exception:
        return None


def record_attempt(driver: Any, stage_tag: str, rundir: str,
                   resumed_from: Optional[str], start_step: Optional[str] = None,
                   start_inclusive: bool = True, log_name: str = "genus.log",
                   step_names: Sequence[str] = ()) -> None:
    """Stamp the marker for the attempt that is about to run.

    Keeps the burned ladder when the inputs are unchanged; a new stage key
    starts fresh (the old ladder belonged to different inputs) and drops the
    old inputs' checkpoints past where this attempt starts.
    """
    try:
        key = _stage_key(driver, stage_tag)
        if key is None:
            return
        state, old = _load_marker(rundir)
        if state == "unreadable":
            return
        if old is None or old.get("stage_key") != key:
            _drop_foreign_checkpoints(rundir, resumed_from, start_step,
                                      start_inclusive, log_name, step_names)
        burned = list(old.get("burned", [])) if old and old.get("stage_key") == key else []
        # lineage of config keys this rundir has attempted: lets a later
        # success clear the database rows its own earlier (differently
        # configured) attempts pushed, without touching anyone else's
        history = list(old.get("key_history", [])) if old else []
        if old and old.get("stage_key") and old["stage_key"] != key \
                and old["stage_key"] not in history:
            history.append(old["stage_key"])
        write_marker(rundir, {
            "stage_key": key,
            "ts": time.time(),
            "resumed_from": resumed_from,
            "burned": burned,
            "key_history": history[-10:],
        })
    except Exception:
        pass
