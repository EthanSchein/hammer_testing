"""
Postgres-backed cache wrapper for Hammer's per-stage actions.

`cache_or_run` wraps a stage's run callable (driver.run_synthesis,
driver.run_par, ...) so that an identical run anywhere else on the team
restores from a stored tarball instead of running the tool again.

Cache key is the stage's slice of the live driver config, plus a content
hash over the RTL files. Hit means we untar the stored rundir and read
<stage>-output.json back out. Miss means we run the tool normally and
push the resulting rundir for the next person.

Off by default. Set HAMMER_PD_CACHE=1 (or vlsi.pd_cache.enabled in a
config) to turn it on. If anything goes wrong (DB down, missing file,
broken tarball) we log a warning and just run the stage like usual.

Access control is whatever Postgres GRANTs the connecting role. If the
user can SELECT from pd_blobs they can read the cache; if they can't,
load_stage_blob returns None and the stage runs from scratch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

try:
    # POSIX-only; harmless to skip on Windows since the cache is Linux-only.
    import resource as _resource
except ImportError:  # pragma: no cover
    _resource = None  # type: ignore

from hammer.vlsi import fingerprints, legacy_hammer, pd_store

# The time-saved tracker (event recording, per-run summary, cross-run
# reporting, ledger/project switches) lives in time_tracking.py. The cache
# imports what it needs inline and re-exports the tracker's public names so
# existing callers -- including already-generated DAGs that do
# `from hammer.vlsi.pd_cache import read_run_cache_summary` -- keep working.
from hammer.vlsi.time_tracking import (
    LEDGER_ENV_VAR, LEDGER_SETTING_KEY,
    PROJECT_ENV_VAR, PROJECT_SETTING_KEY,
    is_ledger_enabled,
    record_event as _record_cache_event,
    stamp_project_from_config as _stamp_project_from_config,
    make_would_rerun as _make_would_rerun,
    stage_module as _stage_module,
    _format_duration, _format_savings,
    read_run_cache_summary, clear_run_cache_events,
    iter_jsonl_cache_events, aggregate_savings, format_savings_report,
    collect_savings_events, exclude_depcheck_skips,
)


def _live_tool_cpu_seconds() -> Optional[float]:
    """CPU seconds burned so far by this process's still-running descendants.

    _child_cpu_seconds uses RUSAGE_CHILDREN, which only counts children that
    have already been waited on -- it reads zero while the tool is still
    running. Checkpoint CPU stamps have to be taken mid-run, so read
    utime+stime straight out of /proc for the whole descendant tree instead.
    Returns None off Linux or if /proc is unreadable.
    """
    try:
        ticks = os.sysconf("SC_CLK_TCK")
        me = os.getpid()
        kids: Dict[int, list] = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat") as f:
                    fields = f.read().rsplit(")", 1)[1].split()
                ppid = int(fields[1])
                kids.setdefault(ppid, []).append(int(entry))
            except (OSError, ValueError, IndexError):
                continue
        total = 0.0
        stack = [me]
        seen = set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            for child in kids.get(pid, []):
                stack.append(child)
                try:
                    with open(f"/proc/{child}/stat") as f:
                        fields = f.read().rsplit(")", 1)[1].split()
                    total += (int(fields[11]) + int(fields[12])) / ticks
                except (OSError, ValueError, IndexError):
                    continue
        return total
    except Exception:
        return None


def _child_cpu_seconds() -> Optional[float]:
    """
    Cumulative CPU (user + sys) consumed by all child processes waited on so
    far in this process. Take a snapshot before and after run_fn() and
    subtract to get the CPU time the tool itself burned.

    Returns None if the resource module isn't available.
    """
    if _resource is None:
        return None
    ru = _resource.getrusage(_resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime


CACHE_ENV_VAR = "HAMMER_PD_CACHE"
CACHE_SETTING_KEY = "vlsi.pd_cache.enabled"


def is_cache_enabled(driver: Optional[Any] = None) -> bool:
    """True if the env var or the driver's setting opts the cache on; never under build_system hammer."""
    if legacy_hammer.is_legacy_hammer(driver):
        return False
    env = os.environ.get(CACHE_ENV_VAR, "")
    if env.strip().lower() not in ("", "0", "false", "no", "off"):
        return True
    from hammer.vlsi import sledge_settings
    return driver is not None and sledge_settings.flag(driver, CACHE_SETTING_KEY, False)


class CacheRestoreContested(RuntimeError):
    """The rundir's obj_dir is locked by a different run; restore would demolish it.

    A restore replaces the whole rundir (staging + os.replace + rmtree of the
    old contents). Doing that under a live tool takes away its working
    directory mid-run. This error must fail the calling task, never downgrade
    to "run the tool locally" -- running in a contested directory is the same
    collision by other means.
    """


def _check_restore_allowed(rundir_path: Path) -> None:
    """Refuse to extract when another run holds the obj_dir lock.

    The lock file name matches hammer.shell.hammer_vlsi.RUN_LOCK_NAME (not
    imported to keep this module free of shell deps). No lock, an unreadable
    lock, or our own run's lock all allow the restore.
    """
    lock = rundir_path.parent / ".sledgehammer-run.lock"
    try:
        held = json.loads(lock.read_text())
    except (OSError, ValueError):
        return
    holder = str(held.get("run_id"))
    ours = os.environ.get("HAMMER_AIRFLOW_RUN_ID")
    if ours is not None and holder == str(ours):
        return
    raise CacheRestoreContested(
        f"refusing cache restore into {rundir_path}: its obj_dir is locked by "
        f"run {holder!r} (this process is {ours!r}). To stop that run and free "
        f"this directory:  sledgehammer cancel {holder}"
    )


# Stages whose sub-step checkpoints stream to the database while the tool
# runs, and the tool log the confirmation parser reads for each.
_STREAM_LOGS = {"synthesis": "genus.log", "par": "innovus.log"}


def _run_with_checkpoint_stream(driver, stage_tag, rundir, run_fn):
    """Run the stage while pushing each tool-confirmed checkpoint to the DB.

    The failure-path push (cli_driver -> substep_resume.push_checkpoint_db)
    only fires when Python survives the failure, and only if the rundir still
    exists. Streaming during the run means a SIGKILL, OOM, node crash, or a
    demolished rundir loses at most one push interval of progress instead of
    all of it. Every push is the same trusted path the failure push uses
    (log-confirmed, key-matched, ceiling-clamped). Each step is uploaded at
    most once, a push that fails for a reason that may pass is retried the
    next interval, and a quiet tool costs one file scan per interval. Any
    trouble in the streamer is swallowed: it must never affect the run itself.
    """
    log_name = _STREAM_LOGS.get(stage_tag)
    if log_name is None:
        return run_fn()
    if os.environ.get("HAMMER_CHECKPOINT_STREAM", "1") in ("0", "false", "no"):
        return run_fn()
    try:
        from hammer.vlsi import substep_resume
        if not (substep_resume.is_enabled(driver) and substep_resume._db_enabled(driver)):
            return run_fn()
    except Exception:
        return run_fn()

    import threading
    stop = threading.Event()
    try:
        interval = float(os.environ.get("HAMMER_CHECKPOINT_STREAM_SECS", "300"))
    except ValueError:
        interval = 300.0
    module = _stage_module(driver, stage_tag)

    def _loop():
        last = done = None
        while not stop.wait(interval):
            try:
                confirmed = substep_resume.confirmed_checkpoints(rundir, log_name)
                # stamp cumulative tool CPU beside every checkpoint we see, so
                # a later resume can price the steps it skips in CPU as well as
                # wall time (checkpoint mtimes only carry the latter)
                cpu_now = _live_tool_cpu_seconds()
                if cpu_now is not None:
                    for step in confirmed:
                        stamp = Path(rundir) / f"pre_{step}.cpustamp"
                        if not stamp.exists():
                            try:
                                stamp.write_text(f"{cpu_now:.3f}\n")
                            except OSError:
                                pass
                if confirmed and confirmed[-1] != last:
                    handled = substep_resume.push_checkpoint_db(
                        driver, stage_tag, rundir, log_name, module=module, skip=done)
                    if handled is not None:
                        last, done = confirmed[-1], handled
            except Exception:
                pass

    t = threading.Thread(target=_loop, name=f"ckpt-stream-{stage_tag}", daemon=True)
    t.start()
    try:
        return run_fn()
    finally:
        stop.set()
        t.join(timeout=5)


_STAGE_TOOL_ATTRS = {
    "synthesis": "syn_tool",
    "par": "par_tool",
    "drc": "drc_tool",
    "lvs": "lvs_tool",
    "sim": "sim_tool",
}


_PATH_SPLIT_RE = re.compile(f"({fingerprints.PATH_SEPARATORS})")


def _content_line(path: str, roots: Any, memo: Dict[str, str]) -> Optional[str]:
    """The fingerprint line, token and content, of a regular file under a content root, as this action's memo
    records it."""
    if path not in memo and fingerprints.tokenize(path, roots, resolve=False) is None:
        return None
    line = fingerprints.file_line(path, roots, memo)
    return line if line.startswith("<") else None


def _rtl_token(path: str, roots: Any) -> Optional[str]:
    """The bare root token of an RTL file under a content root, whose content vlsi.rtl_fingerprint_sha256 covers."""
    tok = fingerprints.tokenize(path, roots, resolve=False)
    return tok if tok is not None and os.path.isfile(path) else None


def _normalize_roots(value: Any, roots: Any, memo: Dict[str, str], seen: Dict[str, Optional[str]]) -> Any:
    """A copy of value for hashing in which a regular file under a content root becomes its content line, or
    the entry seen already holds for it, while directories and missing paths stay absolute."""
    if isinstance(value, str):
        if not roots or "/" not in value and os.sep not in value:
            return value
        parts = _PATH_SPLIT_RE.split(value)
        for i in range(0, len(parts), 2):
            part = parts[i]
            if not os.path.isabs(part):
                continue
            if part not in seen:
                seen[part] = _content_line(part, roots, memo)
            if seen[part] is not None:
                parts[i] = seen[part]
        return "".join(parts)
    if isinstance(value, list):
        return [_normalize_roots(v, roots, memo, seen) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_roots(v, roots, memo, seen) for k, v in value.items()}
    return value


def _stage_input_fingerprint(driver: Any, db: Dict[str, Any], stage_tag: str) -> str:
    """sha256 over what a stage actually consumes from the build dir.
    Hashing their contents (not their paths) makes the key follow the data
    across directories and notice an upstream rerun that left a different
    file at the same path. Layout-sized files contribute size and whole-second
    mtime instead of their bytes. The lines are the action's
    <stage>.upstream_fingerprint_sha256 lines, and an unreadable one fails the key."""
    tool = getattr(driver, _STAGE_TOOL_ATTRS.get(stage_tag, ""), None)
    lines = fingerprints.upstream_lines(db, fingerprints.path_roots(driver), stage_tag,
                                        memo=fingerprints.action_memo(driver),
                                        own_rundir=getattr(tool, "run_dir", None))
    for line in lines:
        if line.startswith("UNREADABLE:"):
            raise PermissionError(f"cannot read stage input {line[len('UNREADABLE:'):]}")
    return fingerprints.digest_lines(lines)


def _build_cache_key(driver: Any, stage_tag: str) -> str:
    """Compute the stage cache key from the live driver config."""
    db_json = driver.database.get_database_json()
    db: Dict[str, Any] = json.loads(db_json)

    rtl_files = db.get("synthesis.inputs.input_files") or []
    rtl_files = [f for f in rtl_files if isinstance(f, str)]
    if rtl_files and not db.get("vlsi.rtl_fingerprint_sha256"):
        defines = db.get("synthesis.inputs.defines") or []
        defines = [d for d in defines if isinstance(d, str)]
        top_module = db.get("synthesis.inputs.top_module")
        include_dirs = [d for d in db.get("synthesis.inputs.include_dirs") or [] if isinstance(d, str)]
        db["vlsi.rtl_fingerprint_sha256"] = pd_store.compute_rtl_fingerprint(
            rtl_files, defines, top_module=top_module if isinstance(top_module, str) else None,
            include_dirs=include_dirs)

    # Collateral edits (LEF/lib files named by path) are covered by
    # vlsi.collateral_fingerprint_sha256, stored as a setting in cli_driver so
    # both stage_change_check and this key see them.

    # A tool's scripting mode (Innovus Tcl vs Python interpreter) changes the
    # scripts, the rundir layout, and the checkpoints, but is derived at run
    # time rather than being a config key of its own -- so bake the effective
    # mode into the hashed slice explicitly. Artifacts produced by the two
    # modes can then never answer for each other, even at the same tool
    # version. (Keyed under vlsi.* so _stage_relevant_keys keeps it.)
    try:
        tool = getattr(driver, _STAGE_TOOL_ATTRS.get(stage_tag, ""), None)
        mode = getattr(tool, "use_python", None)
        if mode is not None:
            db[f"vlsi.pd_cache.script_mode.{stage_tag}"] = (
                "python" if mode else "tcl"
            )
    except Exception:
        pass

    # Files are keyed by content and root token, so the same design in another build dir gets the same key.
    if getattr(driver, "obj_dir", None):
        db[f"vlsi.pd_cache.input_fingerprint.{stage_tag}"] = \
            _stage_input_fingerprint(driver, db, stage_tag)
    roots = fingerprints.path_roots(driver)
    memo = fingerprints.action_memo(driver)
    rtl_tokens = {path: _rtl_token(path, roots) for path in rtl_files}
    db = _normalize_roots(pd_store._stage_relevant_keys(db, stage_tag), roots, {} if memo is None else memo,
                          rtl_tokens)

    return pd_store.compute_stage_key(db, stage_tag)


def _rebase_restored_paths(rundir_path: Path, output_filename: str,
                           info=None, stored_name: Optional[str] = None) -> int:
    """Point a restored rundir's json outputs at the dir it was restored into.

    A blob stored from one build dir carries that dir's absolute paths in its
    output json. Restored somewhere else, the next stage would follow those
    paths back into the original dir (whatever state it is in now), and the
    collateral fingerprint would hash those out-of-dir inputs, so that stage
    could never hit the cache either. Infer the original obj_dir from the
    paths themselves: the prefix before the rundir's name in the blob
    (``stored_name``, by default this rundir's own name). Rewrite it to the
    current obj_dir, and the stored rundir to this one, in every top-level
    json of the rundir. Returns the number of files changed. When the paths
    cannot be rewritten, or a rundir restored under a new name is a symlink,
    holds a symlinked top-level json or has no path under its stored name, it
    removes the output json (or the symlinked rundir itself) and raises, so
    the restore counts as a miss.
    """
    import os
    import re
    info = info or (lambda msg: None)
    try:
        new_dir = str(rundir_path.parent)
        name = rundir_path.name
        stored = stored_name or name
        if stored != name and (rundir_path.is_symlink()
                               or any(j.is_symlink() for j in rundir_path.glob("*.json"))):
            raise ValueError(f"the stored rundir {stored} is a symlink or holds a symlinked json")
        out = rundir_path / output_filename
        if not out.exists():
            return 0
        text = out.read_text()
        prefixes: Dict[str, int] = {}
        for m in re.finditer(r'"((?:/[^"/]+)+)/' + re.escape(stored) + r'/', text):
            prefixes[m.group(1)] = prefixes.get(m.group(1), 0) + 1
        if not prefixes:
            if stored != name:
                raise ValueError(f"{output_filename} has no path under the stored rundir {stored}")
            return 0
        orig = max(prefixes, key=prefixes.get)
        same_dir = orig == new_dir or os.path.realpath(orig) == os.path.realpath(new_dir)
        if same_dir and stored == name:
            return 0
        # only whole-path matches: /a/build/ChipTop must not touch /a/build/ChipTop-iso
        pat = re.compile(re.escape(orig) + r'(/' + re.escape(stored) + r')?(?=/|["\s,\]])')
        changed = 0
        for jf in rundir_path.glob("*.json"):
            s = jf.read_text()
            t = pat.sub(lambda m: f"{new_dir}/{name}" if m.group(1) else new_dir, s)
            if t != s:
                jf.write_text(t)
                changed += 1
        if changed:
            info(f"PD cache: restored {name} was stored from {orig}/{stored}; "
                 f"rewrote {changed} json file(s) to {rundir_path}")
        return changed
    except Exception as e:
        try:
            # unlink a symlinked rundir itself, never a file through it
            (rundir_path if rundir_path.is_symlink() else rundir_path / output_filename).unlink()
        except OSError:
            pass
        raise RuntimeError(f"cannot point the restored paths at {rundir_path}: {e}") from e


def cache_or_run(
    driver: Any,
    stage_tag: str,
    rundir: str,
    output_filename: str,
    run_fn: Callable[[], Tuple[bool, Dict[str, Any]]],
    force_local: bool = False,
    store: bool = True,
    accept: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Cache wrapper around a stage's run function.

    Args:
        driver: HammerDriver instance, used for config and logging.
        stage_tag: One of pd_store.KNOWN_STAGE_TAGS.
        rundir: Absolute path to the stage's run directory (e.g. driver.syn_tool.run_dir).
        output_filename: Name of the per-stage output JSON inside rundir
            (e.g. "syn-output.json"). Used to reconstruct the output dict on
            cache hit.
        run_fn: Callable that actually runs the stage and returns (success, output).
        force_local: If True (the --local or --force flag, or step flags), skip
            restoring from the Postgres cache and run the stage locally. The fresh
            result is still STORED, overwriting the entry for this key, so a forced
            or local re-run refreshes the shared cache rather than ignoring it.
        store: If False (a run with step flags, whose result may be partial),
            run without storing under the full-stage key.
        accept: Called after a successful run, before storing; False (for
            example a fatal error scan) keeps the run out of the cache.

    Returns:
        (success, output) tuple, identical in shape to run_fn's return value.
    """
    log = getattr(driver, "log", None)

    def _info(msg: str) -> None:
        if log is not None:
            log.info(msg)

    def _warn(msg: str) -> None:
        if log is not None:
            log.warning(msg)

    # The stage runs or restores into rundir now, so an older failed store no longer describes what it holds.
    _forget(_pending_path(rundir))

    if not is_cache_enabled(driver):
        _info(f"PD cache disabled for {stage_tag} (set HAMMER_PD_CACHE=1 or "
              f"vlsi.pd_cache.enabled to turn it on); running normally.")
        return _run_with_checkpoint_stream(driver, stage_tag, rundir, run_fn)

    # Resolve the ledger switch once (honors env var + config key) and pass it
    # to each event record so the time-saved tracker can be turned off.
    ledger_on = is_ledger_enabled(driver)
    _stamp_project_from_config(driver)
    module = _stage_module(driver, stage_tag)

    try:
        key = _build_cache_key(driver, stage_tag)
    except Exception as e:
        _warn(f"PD cache: key computation failed ({e}); running {stage_tag} normally.")
        return _run_with_checkpoint_stream(driver, stage_tag, rundir, run_fn)

    short = key[:16]

    if force_local:
        # --local: skip the DB restore entirely and run the tool locally. We
        # still computed the key above and STILL store the fresh result below,
        # so a local re-run refreshes the shared cache instead of bypassing it.
        _info(f"PD cache: --local, --force or step flags set; skipping DB lookup for {stage_tag}, running locally.")
        blob = None
    else:
        try:
            # time the restore from BEFORE the fetch: shipping the blob from
            # Postgres over the network is the dominant overhead legacy never
            # pays, so it must be inside restore_seconds (and thus netted out
            # of the saved time below).
            _restore_t0 = time.monotonic()
            blob = pd_store.load_stage_blob(key)
        except Exception as e:
            # Run it as a miss, so the result is stored, or kept for a later skip to store, once the cache is back.
            _warn(f"PD cache: lookup failed ({_brief(e)}); running {stage_tag} normally.")
            blob = None

    if blob is not None:
        _, data, original_duration, original_cpu = blob
        rundir_path = Path(rundir)
        # Outside the try below on purpose: the generic except downgrades a
        # failed restore to a local run, and a contested directory must fail
        # the task instead.
        _check_restore_allowed(rundir_path)
        try:
            rundir_path.parent.mkdir(parents=True, exist_ok=True)
            stored_name = pd_store.untar_to_directory(data, rundir_path.parent, as_name=rundir_path.name)
            _rebase_restored_paths(rundir_path, output_filename, _info, stored_name)
            output_path = rundir_path / output_filename
            with output_path.open("r") as f:
                output = json.load(f)
            restore_seconds = time.monotonic() - _restore_t0
            saved = None
            saved_cpu = None
            if original_duration is not None:
                saved = max(0.0, original_duration - restore_seconds)
            if original_cpu is not None:
                # Restore is a single-threaded tar untar, so its CPU cost ~=
                # its wall-clock cost. Use that to net it out of the saved-CPU.
                saved_cpu = max(0.0, original_cpu - restore_seconds)
            _record_cache_event(
                stage_tag, "HIT",
                saved_seconds=saved,
                restore_seconds=restore_seconds,
                saved_cpu_seconds=saved_cpu,
                module=module,
                enabled=ledger_on,
            )
            _info(
                f"PD cache HIT for {stage_tag} (sha256={short}...). "
                f"Restored {rundir_path}, skipping run."
                f"{_format_savings(original_duration, restore_seconds, original_cpu)}"
            )
            return True, output
        except Exception as e:
            _warn(
                f"PD cache: hit but restore failed ({e}); running {stage_tag} normally."
            )

    _info(f"PD cache MISS for {stage_tag} (sha256={short}...). Running stage.")
    t0 = time.monotonic()
    cpu0 = _child_cpu_seconds()
    success, output = _run_with_checkpoint_stream(driver, stage_tag, rundir, run_fn)
    duration_seconds = time.monotonic() - t0
    cpu1 = _child_cpu_seconds()
    cpu_seconds: Optional[float] = None
    if cpu0 is not None and cpu1 is not None:
        cpu_seconds = max(0.0, cpu1 - cpu0)
    skip_store = None
    if success and not store:
        skip_store = f"step flags set; not storing this {stage_tag} run, which may be partial."
    elif success and accept is not None:
        try:
            accepted = accept()
        except Exception as e:
            _warn(f"PD cache: post-run check for {stage_tag} raised ({e}).")
            accepted = False
        if not accepted:
            skip_store = f"{stage_tag} failed its post-run checks; not storing this run."
    if skip_store:
        _info(f"PD cache: {skip_store}")
        _record_cache_event(
            stage_tag, "MISS_STORE",
            tool_seconds=duration_seconds,
            tool_cpu_seconds=cpu_seconds,
            store_seconds=None,
            module=module,
            enabled=ledger_on,
        )
    elif success:
        try:
            # Write the stage's output dict into the rundir BEFORE we tar it.
            # cli_driver writes <stage>-output.json after we return, but we
            # need it in the tarball so HIT-path restores can read it back.
            output_path = Path(rundir) / output_filename
            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                from hammer.config import HammerJSONEncoder
                with output_path.open("w") as f:
                    json.dump(output, f, cls=HammerJSONEncoder, indent=4)
            except Exception as e:
                _warn(f"PD cache: could not pre-write {output_filename} ({e}); tarball may lack it.")

            # store cost = tarring/compressing the rundir + uploading the blob
            # to Postgres. This is the one-time overhead SledgeHammer pays on a
            # miss that legacy does not; recorded for visibility, NOT netted
            # into any savings total here.
            _store_t0 = time.monotonic()
            tarball = pd_store.tar_directory(Path(rundir))
            pd_store.store_stage_blob(
                stage_tag, key, tarball,
                duration_seconds=duration_seconds,
                cpu_seconds=cpu_seconds,
                module=module,
                **_provenance(),
            )
            store_seconds = time.monotonic() - _store_t0
            cpu_msg = (
                f", CPU={_format_duration(cpu_seconds)}"
                if cpu_seconds is not None else ""
            )
            _info(
                f"PD cache STORE {stage_tag} (sha256={short}..., "
                f"bytes={len(tarball)}, "
                f"tool runtime={_format_duration(duration_seconds)}{cpu_msg})."
            )
        except Exception as e:
            store_seconds = None
            _warn(f"PD cache: store failed ({_brief(e)}); continuing. A later run that skips {stage_tag} stores it.")
            _mark_pending(rundir, {"key": key, "duration_seconds": duration_seconds, "cpu_seconds": cpu_seconds,
                                   "module": module, "provenance": _provenance(), "tries": 0})
        # The event is the record that the stage RAN; it must not vanish just
        # because the blob upload did. A store failure leaves store_seconds
        # empty, and the JSONL side of record_event works with no database at
        # all -- that was the point of writing it first.
        _record_cache_event(
            stage_tag, "MISS_STORE",
            tool_seconds=duration_seconds,
            tool_cpu_seconds=cpu_seconds,
            store_seconds=store_seconds,
            module=module,
            enabled=ledger_on,
        )
    return success, output


def _brief(e: BaseException) -> str:
    """An error's first line: libpq's own run over several, the rest being hints such as "Is the server running"."""
    return next((line.strip() for line in str(e).splitlines() if line.strip()), type(e).__name__)


def _provenance() -> Dict[str, Optional[str]]:
    return dict(
        triggering_user=os.environ.get("HAMMER_AIRFLOW_TRIGGERING_USER") or None,
        dag_id=os.environ.get("HAMMER_AIRFLOW_DAG_ID") or None,
        dag_run_id=os.environ.get("HAMMER_AIRFLOW_RUN_ID") or None,
        workspace=os.environ.get("HAMMER_AIRFLOW_WORKSPACE") or None,
        design=os.environ.get("HAMMER_AIRFLOW_DESIGN") or os.environ.get("design") or None,
    )


# Skips that retry a failed store before the result is left out of the cache.
PENDING_STORE_TRIES = 3


def _pending_path(rundir: str) -> Path:
    """Beside the rundir, so the tarball never carries it: the key and runtime of a result whose store failed."""
    path = Path(rundir)
    return path.parent / f".{path.name}.pd-store-pending.json"


def _rundir_state(rundir: str) -> str:
    """A digest of each file's path, size and mtime in the rundir, but the output and config files every run rewrites
    after the store."""
    root = Path(rundir)
    digest = hashlib.sha256()
    for top, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = Path(top) / name
            if top == str(root) and re.fullmatch(
                    r"[\w.-]+-output(\.json|-full\.json|-history\.yml)|full_config\.json|module_config\.json", name):
                continue
            try:
                st = path.lstat()
            except OSError:
                continue
            line = f"{path.relative_to(root).as_posix()}\0{st.st_size}\0{st.st_mtime_ns}\n"
            digest.update(line.encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def _mark_pending(rundir: str, pending: Dict[str, Any]) -> None:
    """Best effort: a stage that already succeeded must not fail over its retry marker."""
    try:
        _pending_path(rundir).write_text(json.dumps({**pending, "state": _rundir_state(rundir)}))
    except Exception:
        pass


def _forget(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _store_pending(stage_tag: str, key: str, rundir: str, missing: bool, info: Callable[[str], None],
                   warn: Callable[[str], None]) -> None:
    """Store the local result of a run whose own store failed, while the cache still has no entry for it."""
    path = _pending_path(rundir)
    try:
        pending = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(pending, dict) or pending.get("key") != key or not missing:
        _forget(path)
        return
    if pending.get("state") != _rundir_state(rundir):
        _forget(path)
        info(f"PD cache (skip-path): {stage_tag}'s rundir changed after its store failed; not storing it.")
        return
    provenance = _provenance()
    recorded = pending.get("provenance")
    if isinstance(recorded, dict):
        provenance.update((k, v) for k, v in recorded.items() if k in provenance)
    try:
        pd_store.store_stage_blob(stage_tag, key, pd_store.tar_directory(Path(rundir)),
                                  duration_seconds=pending.get("duration_seconds"),
                                  cpu_seconds=pending.get("cpu_seconds"), module=pending.get("module"),
                                  **provenance)
    except Exception as e:
        tries = int(pending.get("tries") or 0) + 1
        if tries < PENDING_STORE_TRIES:
            _mark_pending(rundir, {**pending, "tries": tries})
            warn(f"PD cache (skip-path): could not store the {stage_tag} result an earlier run failed to store "
                 f"({_brief(e)}); a later skip tries again.")
        else:
            _forget(path)
            warn(f"PD cache (skip-path): could not store the {stage_tag} result an earlier run failed to store "
                 f"({_brief(e)}), {tries} times; leaving it out of the cache.")
        return
    _forget(path)
    info(f"PD cache (skip-path): stored the {stage_tag} result an earlier run failed to store (sha256={key[:16]}...).")


def try_restore_from_cache(
    driver: Any,
    stage_tag: str,
    rundir: str,
    output_filename: str,
) -> bool:
    """
    Lighter sibling of cache_or_run, used in the "stage_change_check says skip"
    branch of cli_driver.py.

    Situation: Hammer's dependency tracker has decided the stage doesn't need
    to rerun (config + inputs unchanged since the last commit_master_database).
    But our local rundir might have been wiped since then (e.g. disk cleanup,
    fresh checkout, or any "I cleared the build dir but kept master_database"
    flow). If the rundir's output_filename is missing on disk, we'd skip the
    stage AND have nothing for downstream stages to read. Bad.

    This function attempts to restore from cache instead. Returns True if it
    restored a tarball, False if it didn't (cache disabled, no matching blob,
    or restore failed). On True the caller can proceed as if the stage ran.
    """
    log = getattr(driver, "log", None)

    def _info(msg: str) -> None:
        if log is not None:
            log.info(msg)

    def _warn(msg: str) -> None:
        if log is not None:
            log.warning(msg)

    if not is_cache_enabled(driver):
        return False

    ledger_on = is_ledger_enabled(driver)
    _stamp_project_from_config(driver)
    module = _stage_module(driver, stage_tag)

    output_path = Path(rundir) / output_filename
    if output_path.exists():
        # Local files still present, nothing to do. The skip is safe.
        # Still helpful to surface the savings vs a fresh tool run.
        original_duration: Optional[float] = None
        original_cpu: Optional[float] = None
        try:
            key = _build_cache_key(driver, stage_tag)
            runtime = pd_store.stage_blob_runtime(key)
            _store_pending(stage_tag, key, rundir, runtime is None, _info, _warn)
            if runtime is not None:
                original_duration, original_cpu = runtime
                if original_duration:
                    cpu_msg = (
                        f"; {_format_duration(original_cpu)} CPU"
                        if original_cpu else ""
                    )
                    _info(
                        f"PD cache (skip-path) for {stage_tag}: stage_change_check "
                        f"says skip, local rundir present. "
                        f"Saved ~{_format_duration(original_duration)} wall"
                        f"{cpu_msg} (original tool runtime) by not re-running."
                    )
        except Exception:
            pass
        _record_cache_event(
            stage_tag, "SKIP_LOCAL",
            saved_seconds=original_duration,
            saved_cpu_seconds=original_cpu,
            # Would legacy make's mtime rule have rerun this stage? True means
            # this skip is a SledgeHammer-only saving (content-aware dep-check
            # beat make); False means legacy would have skipped it too.
            make_would_rerun=_make_would_rerun(driver, output_path),
            module=module,
            enabled=ledger_on,
        )
        return True

    try:
        key = _build_cache_key(driver, stage_tag)
    except Exception as e:
        _warn(f"PD cache (skip-path): key computation failed ({e}); not restoring.")
        return False

    short = key[:16]

    try:
        # time from before the fetch so the Postgres/network transfer counts
        _restore_t0 = time.monotonic()
        blob = pd_store.load_stage_blob(key)
    except Exception as e:
        _warn(f"PD cache (skip-path): lookup failed ({_brief(e)}); not restoring.")
        return False

    if blob is None:
        _record_cache_event(stage_tag, "SKIP_NO_BLOB", module=module, enabled=ledger_on)
        _warn(
            f"PD cache (skip-path): stage_change_check would skip {stage_tag}, "
            f"but no local {output_filename} and no matching cache blob "
            f"(sha256={short}...); {stage_tag} runs instead."
        )
        return False

    _, data, original_duration, original_cpu = blob
    rundir_path = Path(rundir)
    # Same contested-directory refusal as the HIT path, before any extraction.
    # This is the exact path that demolished a live par: the dep-skip branch
    # found no local output json (a fresh in-flight run had cleared it) and
    # restored the old blob over the running tool's rundir.
    _check_restore_allowed(rundir_path)
    try:
        rundir_path.parent.mkdir(parents=True, exist_ok=True)
        stored_name = pd_store.untar_to_directory(data, rundir_path.parent, as_name=rundir_path.name)
        _rebase_restored_paths(rundir_path, output_filename, _info, stored_name)
        restore_seconds = time.monotonic() - _restore_t0
        saved = None
        saved_cpu = None
        if original_duration is not None:
            saved = max(0.0, original_duration - restore_seconds)
        if original_cpu is not None:
            saved_cpu = max(0.0, original_cpu - restore_seconds)
        _record_cache_event(
            stage_tag, "SKIP_RESTORED",
            saved_seconds=saved,
            restore_seconds=restore_seconds,
            saved_cpu_seconds=saved_cpu,
            # The output file was missing, so legacy make would rebuild
            # unconditionally: this restore is a SledgeHammer-only saving.
            make_would_rerun=True,
            module=module,
            enabled=ledger_on,
        )
        _info(
            f"PD cache HIT (skip-path) for {stage_tag} (sha256={short}...). "
            f"stage_change_check said skip, local rundir was missing; "
            f"restored from cache."
            f"{_format_savings(original_duration, restore_seconds, original_cpu)}"
        )
        return True
    except Exception as e:
        _warn(f"PD cache (skip-path): hit but restore failed ({e}).")
        return False

