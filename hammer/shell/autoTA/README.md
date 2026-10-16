# AutoTA+: AI-Powered VLSI Log Analyzer

Automated debug agent for the Sledgehammer ASIC pipeline. Analyzes synthesis (Genus), RTL simulation, and place-and-route (Innovus) logs using Gemini AI. Read [Known Issues](#known-issues) before you turn on patching in the DAG.

## Setup

### 1. Get a Gemini API Key

Go to [Google Cloud API Credentials](https://console.cloud.google.com/apis/credentials) and create an API key with Gemini API access.

### 2. Save the Key

```bash
cd hammer/shell/autoTA   # in your SledgeHammer checkout (vlsi/hammer in Chipyard)
python3 gemini.py --set-key YOUR_API_KEY
```

This writes the key to `autoTA/.api_key` (mode `0600`, gitignored), so no `source` is needed. `gemini.py` reads `$AUTOTA_API_KEY` first, then `.api_key`, then an `export AUTOTA_API_KEY=` line in `~/.bashrc`. For DAG runs, the user that runs the Airflow worker must be able to read the key.

### 3. Verify

```bash
python3 gemini.py --show-key
```

`--show-key` checks only `$AUTOTA_API_KEY` and `.api_key`, not `~/.bashrc`.

### 4. Install the Dependencies

```bash
bash first-time.sh
```

This installs Miniforge into `autoTA/conda/` and pip-installs `google-genai`, `rich` and `pyyaml` there. When `gemini.py` cannot import `google.genai` or `yaml`, it re-runs itself in the base environment of the conda named by `$AUTOTA_CONDA`, else that one, else `conda` on `PATH`. Installing `google-genai` and `pyyaml` into the Python that runs `gemini.py` works too.

## Usage

```bash
# Analyze synthesis logs
python3 gemini.py --phase syn

# Analyze RTL simulation logs
python3 gemini.py --phase sim_rtl

# Analyze place-and-route logs
python3 gemini.py --phase par

# Analyze a specific log file in syn-rundir/
python3 gemini.py --phase syn genus.log2

# Help
python3 gemini.py --help
```

Run from your **design build directory** (e.g., `build-sky130-cm/gcd/`), or the DAG will set the working directory automatically. `gemini.py` looks for the design configs in that directory, its parent and grandparent, then in `configs-design/<design>/` two to five levels above; if it finds none, it sends no configs and no HDL.

Each phase reads the newest log in its run directory: `genus.log*` in `syn-rundir/`, `*.log` in `sim-rtl-rundir/`, `innovus.log*` in `par-rundir/`. A log file you name is looked up in that directory.

Running `gemini.py` by hand only analyzes: it prints the report and writes the logs below, and never edits your files. At a terminal it renders the report with `rich` when that is installed; when its output is piped or captured, it prints the raw reply.

## Patching in the DAG

Only the `Sledgehammer_demo_gcd` DAG applies patches, and only when you trigger it with `debug` on:

```bash
sledgehammer dags trigger Sledgehammer_demo_gcd --conf '{"syn": true, "debug": true}'
```

Airflow does not load this DAG or `Sledgehammer_Meta_gcd` by default: both live in `hammer/shell/airflow_example_dags.py`, outside the dags folder. The per-design `sledgehammer_<design>_<user>` DAGs have no debug tasks.

After each selected stage, the `syn_debug`, `sim_rtl_debug` or `par_debug` task runs `gemini.py` in the run's `OBJ_DIR` (`par_debug` only when `par` did not succeed) and captures its output. That output is not a terminal, so `gemini.py` prints the raw reply even with `rich` installed, and when `gemini.py` exits 0, `patcher.py` parses it. On `PATCH_AND_RETRY` it applies each `PATCH` block to the first file with that basename it finds walking up from `OBJ_DIR` (in `configs-design/*/`, `src/` or the directory itself). If any block applied, it writes `autota_patches/patch_status.json`. See [Known Issues](#known-issues) for the limits of this step.

The `Sledgehammer_Meta_gcd` DAG chains trials of `Sledgehammer_demo_gcd`. `trial_0` always runs, and each later trial runs only if the one before it left a patch, up to `num_trials` (default 3, at most 10). Every trial reruns the Meta-DAG's own conf with `debug` forced on. The model's `Trigger` is saved in `patch_status.json` but does not pick the stages.

## What Gets Sent to the AI

Every stage receives **all design config files** so the AI can predict downstream issues (e.g., syn_debug sees par.yml to forecast PAR problems).

| Data | syn | sim_rtl | par |
|------|-----|---------|-----|
| Primary config (else `common.yml`) | `syn.yml` | `sim-rtl.yml` | `par.yml` |
| All other configs | ✅ every `.yml` beside the primary config | ✅ | ✅ |
| HDL source files | ✅ (raw, no comment stripping) | only if listed (see below) | only if listed (see below) |
| Testbench files | n/a | ✅ (`*_tb.v`) | n/a |
| Timing report | newest `*.setup_view.rpt` in `syn-rundir/reports/` (else `*timing*.rpt`) | n/a | latest postRoute setup and hold reports (else postCTS) |
| Log issues | Genus log | Sim log | Innovus log |

HDL comes only from `synthesis.inputs.input_files` in the primary config, cut to 30,000 characters per file (head and tail). The gcd `sim-rtl.yml` and `par.yml` do not set it, so those phases send no HDL. Each timing report is cut down before it is sent: up to 20 lines of its WNS, TNS and slack summary and its first two critical paths, truncated at 3,000 characters.

`extract.py` sorts the log lines into categories by keyword (for `sim_rtl`: compile errors, test failures, simulation timeouts, errors and warnings). It masks PDK names in the log issues (built-in patterns plus the comma-separated regexes in `AUTOTA_PDK_PATTERNS`). Configs, HDL and timing reports go to Gemini unmasked.

## AI Output Format

The AI outputs a structured analysis with:
1. **SUMMARY**: What happened, what's broken
2. **CURRENT ERRORS**: Each issue with file, line, evidence (the par prompt asks for a category instead of file and line)
3. **DOWNSTREAM PREDICTIONS**: What will fail in future stages (the par prompt asks for **TIMING** instead: setup and hold WNS, TNS and failing counts)
4. **PATCH**: `FILE:`, `FIND:`, `REPLACE_WITH:` and `END_PATCH` blocks, not a `git diff` (`FIND: NEW_FILE` creates a file)
5. **CHANGE LOG**: One bullet per file explaining the change
6. **ACTION**: Result (PROCEED, PATCH_AND_RETRY, or ABORT), Confidence (HIGH, MEDIUM, or LOW), Trigger and, for par, Fix Level

## Files

| File | Purpose |
|------|---------|
| `gemini.py` | Main analyzer: calls Gemini AI on extracted log issues |
| `extract.py` | Log parser: extracts warnings/errors by category and severity |
| `config.yml` | AI model settings and phase-specific prompts |
| `patcher.py` | Parses the AI reply and applies its `PATCH` blocks (DAG debug tasks only) |
| `pruner.py` | Retry limiter and branch report over `branches.json` (no DAG calls it today) |
| `branches.json` | Branch log for `patcher.track_branch`, which no DAG calls today (holds only March 2026 entries) |
| `first-time.sh` | Installs Miniforge into `conda/` with `google-genai`, `rich` and `pyyaml` |
| `history_summary.md` | Notes on why the patch format moved from `git diff` to find-and-replace |

## Output & Logs

| Location | What | Persists |
|----------|------|----------|
| `autoTA/logs/*.json` | Write-once JSON audit log (sealed to mode `0440`) | Shared |
| `<build-dir>/synthesis_issues.log`, `sim_issues.log`, `par_issues.log` | Extracted log issues, rewritten each run | Per-design |
| `<build-dir>/autota_logs/` | Legacy text logs | Per-design |
| `<build-dir>/autota_patches/<timestamp>_<phase>/` | Patch archives (see below) | Per-design |
| `<build-dir>/autota_patches/<run-id>_<stage>/` | DAG only: `patch_raw.txt` and `originals/` of the files `patcher.py` tried to patch (first 30 characters of the run id, then `_syn`, `_sim` or `_par`) | Per-design |
| `<build-dir>/autota_patches/patch_status.json` | DAG only: tells the Meta-DAG a patch was applied | Per-design |

### Patch Archives

Created by every `gemini.py` run that reaches the AI, before any patching. Contains backups of the HDL and config files the AI was given:

```
autota_patches/2026-03-04_132800_syn/
├── manifest.json      # timestamp, phase, git commit, backed-up file paths
├── ai_analysis.md     # full AI response (with the PATCH blocks)
└── originals/         # backup copies of all source + config files
```

**Finding what you need:**
- **Latest?** → Sort the timestamped folders by name (newest last)
- **What files?** → `manifest.json`
- **What did the AI say?** → `ai_analysis.md`
- **Rollback?** → Copy files from `originals/` back (paths in `manifest.json`)

## Known Issues

- When the exact `FIND` text is missing, the fuzzy match can replace the wrong lines and still report success. Check `git diff` after a patch.
- Patches edit the shared `e2e/src` and `configs-design/` files in place, so review them before you apply one in a checkout others use.
