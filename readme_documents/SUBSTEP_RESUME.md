# Sub-step checkpoints: automatic resume, manual selection, and the database

Genus and innovus write a checkpoint of the design at every step boundary
(`write_db pre_<step>`) while the generated script runs. Upstream hammer can
restart from one, but only if you diagnose which step failed and pass
`--from_step` by hand, and nothing checks that the checkpoint is real or that
your config still matches it. SledgeHammer turns those checkpoints into a
first-class mechanism with three layers:

1. **Automatic resume.** When a syn or par run dies partway, the next plain
   invocation picks up from the last checkpoint the tool confirmed writing.
2. **Validated manual selection.** `--from_step` / `--only_step` (CLI or the
   DAG trigger form) refuse to start from a step whose checkpoint does not
   exist, and tell you what is available instead.
3. **Database checkpoints.** A running, failed or paused stage pushes its
   newest trusted checkpoint to Postgres, so a fresh checkout, a wiped
   rundir, or a teammate's machine can resume a run it never executed.

## How a checkpoint earns trust

A checkpoint is only used when all of these hold:

1. The tool's own log confirms the write finished. Genus prints "Finished
   exporting design database to file 'pre_X'". Innovus never prints a
   completion line, so trust comes from the `latest` symlink (the script
   repoints it immediately after each `write_db` returns) plus the ordering
   of "Writing Binary DB to pre_X/" lines: a later write starting proves the
   earlier one finished. A write the tool died inside of is never loaded.
   Confirmations are collected across all log rotations, so an attempt that
   died before confirming anything does not erase the proof from earlier
   attempts. The resume point is therefore the deepest trusted progress for
   the current inputs, which can be deeper than where the last attempt died
   or paused.
2. The inputs haven't changed. A marker file in the rundir records the stage
   key (the same config+RTL fingerprint the PD cache uses) for the attempt
   that produced the checkpoints. Any change to the RTL or to the config that
   key covers means the checkpoints describe a different design: they are
   deleted and the run starts from scratch (after checking the database for
   a checkpoint pushed under the new key, e.g. from another machine). The
   stage key leaves out SledgeHammer's run-control keys (the `vlsi.pd_cache`,
   `vlsi.substep_resume` and `vlsi.error_scan` keys, and
   `vlsi.core.airflow_*`), so changing one of those never invalidates the
   checkpoints.
3. It hasn't been burned. If a resume makes no progress (no new confirmed
   checkpoint), the rung it resumed from is burned and the next attempt uses
   the next older one, ending at a scratch run. When the ladder runs out with
   database checkpoints on, the burned steps' rows for the current inputs are
   deleted too, so the next run starts from scratch instead of fetching the
   same bad checkpoint again. The `read_db` in a resume script is guarded
   (`catch` + exit) because both tools otherwise drop to an interactive
   prompt on a bad database and hang the run forever: a corrupt checkpoint
   costs one failed attempt of about half a minute, never a hang and never a
   loop.

One clamp per tool: a resume never starts later than `write_regs`, because
the output bookkeeping needs the tail steps to run in the current invocation.
Those tail steps cost seconds.

## Database checkpoints

Every failure exit of a stage (tool crash, deliberate pause, error-scan
policy failure) pushes the newest trusted checkpoint to Postgres: the
`pre_<step>` file (genus) or directory (innovus), compressed, keyed by the
stage key and step, with design / module / owner / DAG / project provenance.
While the tool runs, the same push also fires from a background streamer,
which checks every 300 seconds (`HAMMER_CHECKPOINT_STREAM_SECS`) and pushes
any newer confirmed checkpoint, so a SIGKILL or a node crash loses at most
one interval. `HAMMER_CHECKPOINT_STREAM=0` turns the streamer off.
When the stage later commits successfully its rows are deleted, so the table
only ever holds stages that are currently running, broken or paused. That
makes `studio checkpoints` a live answer to "what is resumable right now".

Restores prefer local files and fall back to the database. The fallback
covers three cases: an empty rundir (fresh checkout, wiped workspace, another
machine), a key mismatch where someone else pushed a checkpoint for the
current config, and a burned-out local ladder. A checkpoint that already
failed to make progress is not fetched twice.

Commands and switches:

- `studio checkpoints` lists stored checkpoints (`--design`, `--stage`,
  `--key`, `--keys` to print stage keys).
- `studio checkpoints-push --rundir <dir> --stage syn|par` uploads a local
  checkpoint by hand (bank partial progress before wiping a machine, or hand
  a paused run to a teammate). Trust rules still apply: only tool-confirmed
  checkpoints push, the stage key comes from the rundir's marker.
- `studio checkpoints-fetch --id N --dest <rundir>` downloads any row as
  `pre_<step>`, with no key check: an explicit choice, like naming a step
  with `--from_step`. This is the manual bridge for continuing a run whose
  config has since changed; it prints the exact `--from_step` to use next.
- Clearing is automatic and follows the flow: the rundir's marker remembers
  up to ten earlier config keys, and a successful run deletes the rows for
  its current key plus that whole lineage. Completing a stage after a config
  fix therefore sweeps the rows its own broken predecessors pushed, while a
  teammate debugging a different config of the same design keeps their row
  (their keys are not in your rundir's lineage). `studio checkpoints-clear`
  (`--id`, `--key`, `--design`, `--older-than-days`) remains for rundirs
  that were abandoned outright.
- Off switch: `HAMMER_DB_CHECKPOINTS=0` or
  `vlsi.substep_resume.db_checkpoints: false`. Local resume keeps working.

## Choosing a step by hand

Explicit step flags always win over automatic resume:

- `--from_step X` / `--only_step X` first verify that `pre_X` exists, locally
  or in the database (a database hit is downloaded into the rundir
  automatically). If it exists nowhere, the run refuses in seconds with the
  list of steps that are available locally and in the database, instead of
  wedging the tool on a checkpoint that was never written.
- Explicit step flags bypass the unchanged-inputs skip: asking for a step
  means the stage runs, even when it is already committed.
- Step flags also skip the PD cache lookup, and a step-flag run is never
  stored in the cache, since it may be partial.
- `--force` means a scratch run: it skips the PD cache lookup, runs
  the tool and overwrites the cache entry. Without `--force` or step flags, a
  whole-stage cache HIT still beats resume: if the finished result is in the
  cache there is nothing to resume.
- `--force` together with a start step (`--from_step`, `--after_step` or
  `--only_step`) keeps the checkpoints, so the stage starts from that step;
  it also bypasses the unchanged-inputs skip. Plain `--force`, also with
  only `--to_step`, clears them and starts from scratch.

Note the checkpoint a step flag needs is the boundary written before that
step, and a pause writes no `pre_` boundary after its stop step. After
`--to_step add_tieoffs` the newest is `pre_add_tieoffs`, so you continue with
`--from_step add_tieoffs` (which reruns that step), not `write_regs`.
`studio checkpoints` or the refusal message will tell you the exact names.

## Pausing a stage

`--to_step X` stops the stage after step X:

- The tool still starts: Genus launches even when flow control stops before
  the end of its step list.
- A paused run is deliberately incomplete. The selected steps execute and checkpoint (and push
  to the database), but the stage does not commit: no output json, the
  dependency state stays needs-rerun, and the exit code is nonzero so make
  and DAG tasks cannot treat a half-run stage as done. The log says exactly
  what to do: "Checkpoints up to the stop step are saved. Rerun without step
  flags to auto-resume from there."

The pause/inspect/continue loop is therefore: `--to_step X`, look at
whatever you wanted to look at, plain rerun to continue.

## Time accounting

A successful resume records a `RESUME` event in the time-saved ledger. Its
`saved_seconds` is measured, not estimated: the span of the previous
attempt's checkpoint timestamps covering the steps that were skipped. A
checkpoint from the database carries the span measured where it was pushed;
one pushed with `studio checkpoints-push` has none, so it stays null. The
report shows it as "Saved by substep resume" and counts it in SLEDGEHAMMER
TIME SAVED. Legacy hammer could achieve the same skip with a hand-written
`--from_step`, so quote this bucket separately if a reviewer pushes back;
the automation, validation, and cross-machine restore are what the fork adds.

## On the DAG

Automatic resume needs nothing from the UI: DAG tasks run the same CLI path,
so a task that dies mid-tool resumes on the next triggered run by itself
(the `Redo` checkbox forces a scratch run of the selected stages instead, and
`Redo all` of every stage the run executes). Manual step selection is
in the trigger form, injected straight into the task's argv:

- `From step` / `To step` / `Only step`: free-text sub-step names, validated
  the same way as the CLI flags.
- `Step flags apply to`: which stage the fields target (`syn` or `par`), so
  a from_step meant for syn can never leak into par or a bridge action. On
  the `sledgehammer` CLI it is `--steps-stage` (default `syn`), so par step
  flags need `--steps-stage par`.

Example: rerun a hierarchical module from its tieoff insertion on: select
`syn`, Modules `RocketTile`, From step `add_tieoffs`.

## Turning it off

Automatic resume is on by default. You can turn it off in two places:

- `HAMMER_SUBSTEP_RESUME=0` in the environment that runs the tasks. DAG tasks
  inherit the stack's environment, so launch the stack with
  `HAMMER_SUBSTEP_RESUME=0 sledgehammer` (or start your edge worker with it).
  It holds for every run until you relaunch without it. Prefixing
  `sledgehammer run` or `sledgehammer syn` with it has no effect: those
  commands only trigger the run.
- `vlsi.substep_resume.enabled: false` in a project YAML, for a switch that
  stays with the design. Its default, `true`, is in
  `hammer/config/defaults.yml`.

When the variable is set it wins over the YAML in either direction, so
`HAMMER_SUBSTEP_RESUME=1` turns resume back on over a `false` in the YAML. The
variable reads `0`, `false`, `no`, `off` (any case) and an empty value as off.
In the YAML, `false`, `0`, `no` and `off` turn it off and `true`, `1`, `yes`
and `on` turn it on (any case). A value Hammer does not recognize, such as a
misspelling, logs a warning and leaves resume on.

With resume off, a plain rerun after a crash or a pause starts from scratch,
and no stage pushes checkpoints to the database. Step flags still work:
`--from_step` and `--only_step` still check for the checkpoint and can still
fetch it from the database.

Database checkpoints have their own pair, read the same way and with the same
precedence: `HAMMER_DB_CHECKPOINTS=0` or
`vlsi.substep_resume.db_checkpoints: false`. Nothing is then pushed, fetched
or cleared, step flags see only local checkpoints, and local resume keeps
working. The `studio checkpoints` commands ignore both switches.

Neither YAML key is part of the stage key or the dependency check, so adding,
changing or removing one never marks a stage to rerun, never changes its PD
cache key and never invalidates its checkpoints.

## Verified behavior

All of this was tested live against the real Postgres instance, not just
unit-checked (12 genus + 6 innovus + 10 database unit checks also pass):

- riscv151 syn (demo_riscv, sky130): killed mid-run, resumed, completed;
  the resumed netlist was byte-identical to a scratch run. Double-kill
  chains resume from the later checkpoint. A corrupt checkpoint failed in
  24 seconds, burned its rung, and the next run stepped down and completed.
- riscv151 par (innovus 25.11, python mode): killed 60 seconds into routing,
  resumed from `route_opt_design`, produced the final GDS. In python mode
  the evidence is in `par.py` (`read_db('pre_X')`); the log does not echo
  script commands.
- RocketConfig (techname, hierarchical `syn-RocketTile`): killed 33 minutes
  in, resumed from `syn_map`, netlist byte-identical to the known-good run,
  ledger recorded RESUME saved=1822s. A stale output json from an old
  successful run in the same build dir did not block the resume.
- Database round trip: a paused stage pushed a compressed checkpoint; the
  rundir was deleted entirely; the next plain run fetched the checkpoint
  from Postgres, resumed, completed, and cleared the row. A manual
  `--from_step` with the checkpoint only in the database downloaded and used
  it. A bogus step name was refused in 4 seconds with the availability list.

## Scope and known gaps

- Syn (genus) and par (innovus) are covered. Other tools with checkpointing
  (tempus, joules, conformal, openroad) would follow the same per-tool
  confirm-pattern + ceiling recipe.
- A paused or crashed task shows as a red FAILED task in the Airflow UI even
  though a pause is intentional: the task wrapper fails the task on any
  nonzero exit code, and a pause exits nonzero.
- Config changes, other than to SledgeHammer's run-control keys, invalidate
  checkpoints by design. After you fix a config or hook, automatic resume
  goes scratch; the supported debug loop is manual `--from_step` on the same
  machine (local checkpoints are accepted by presence, since naming a step is
  taking responsibility). No setting lets automatic resume, local or from
  the database, carry across a config change; for a database checkpoint,
  `studio checkpoints-fetch` is the manual bridge.
- If a run SUCCEEDED and you then delete its outputs with the cache off, the
  dependency check skips without re-running and nothing regenerates the
  outputs (the SKIP_NO_BLOB hole). With the cache on,
  SKIP_RESTORED already covers this. Resume does not run on that skip path.
