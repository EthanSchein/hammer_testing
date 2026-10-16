#!/usr/bin/env bash
# From-scratch environment setup for Hammer + Airflow + Postgres under uv.
# Run from a fresh clone:  ./scripts/uv_setup.sh
#
# Profiles
#   (default)       The full SledgeHammer stack: Hammer, Airflow, the Postgres
#                   driver, sibling plugins. What the studio needs.
#   SLEDGE_LAB=1    Hammer only. No Airflow, no Postgres driver, no
#                   compiled extensions, no dev tooling, no prompts, and it
#                   prefers the system python over a downloaded one. For
#                   coursework where people run hammer-vlsi and nothing else.
#                   On a fresh VM this finishes in well under a minute.
#
# Knobs (either profile)
#   SLEDGE_NO_PLUGINS=1   skip the sibling hammer-*-plugin install loop
#   PYVER=3.11            python minor version the venv is built with
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
# The local Postgres step may install Postgres with conda, which the cleanup below takes off PATH.
_path_before_cleanup="${PATH:-}"

_lab="${SLEDGE_LAB:-}"
# The caller's PATH and conda, kept before the cleanup below so the link step can spot other sledgehammer copies.
_caller_path="${PATH:-}"
_caller_conda_exe="${CONDA_EXE:-}"

# Sanitize the build environment. psycopg2 compiles from source
# here; whatever OpenSSL / libpq is on the linker path gets baked into the
# binaries as an RPATH, and a FOREIGN one (conda's, an active venv's, an env
# module's) then breaks the binary in every future shell -- e.g. conda's
# libcrypto lacks EVP_md2, so psycopg2 fails to import forever after. No later
# cleanup fixes an RPATH. So we neutralize the known offenders before building
# (uv builds the venv with its own standalone Python, so the result is clean),
# and -- as a catch-all for anything we did not anticipate -- verify at the end
# that psycopg2 actually imports.
#
# The lab profile compiles nothing, but the same stray environments can still
# shadow the python or the venv we are about to create, so it runs this too.
_strip_pathlike() {   # $1 = the ':'-list; $2.. = glob patterns of entries to drop
    local list="$1"; shift
    local out="" p pat drop; local IFS=:
    for p in $list; do
        drop=0
        for pat in "$@"; do case "$p" in $pat) drop=1; break ;; esac; done
        [ "$drop" = 0 ] && out="${out:+$out:}$p"
    done
    printf '%s' "$out"
}
_neutralized=""
# 1. conda / mamba / micromamba / miniforge / pixi -- the usual culprit
if [ -n "${CONDA_PREFIX:-}${CONDA_DEFAULT_ENV:-}${MAMBA_ROOT_PREFIX:-}${PIXI_PROJECT_ROOT:-}" ] \
   || printf '%s' "${PATH:-}" | grep -qiE 'conda|miniforge|mamba|pixi'; then
    _neutralized="$_neutralized conda/mamba/pixi"
    PATH="$(_strip_pathlike "$PATH" '*conda*' '*miniforge*' '*mamba*' '*pixi*')"; export PATH
    [ -n "${LD_LIBRARY_PATH:-}" ] && { LD_LIBRARY_PATH="$(_strip_pathlike "$LD_LIBRARY_PATH" '*conda*' '*miniforge*' '*mamba*' '*pixi*')"; export LD_LIBRARY_PATH; }
    unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_SHLVL CONDA_PROMPT_MODIFIER \
          CONDA_EXE CONDA_PYTHON_EXE MAMBA_ROOT_PREFIX PIXI_PROJECT_ROOT 2>/dev/null || true
fi
# 2. an already-active pip/uv virtualenv would shadow the one we are about to build
if [ -n "${VIRTUAL_ENV:-}" ]; then
    _neutralized="$_neutralized venv($(basename "$VIRTUAL_ENV"))"
    PATH="$(_strip_pathlike "$PATH" "$VIRTUAL_ENV/bin")"; export PATH
    unset VIRTUAL_ENV 2>/dev/null || true
fi
# 3. an LD_PRELOAD injects a library into every build subprocess
[ -n "${LD_PRELOAD:-}" ] && { _neutralized="$_neutralized LD_PRELOAD"; unset LD_PRELOAD; }
if [ -n "$_neutralized" ]; then
    echo "note: neutralized for a clean build:$_neutralized"
fi
# 4. anything left on LD_LIBRARY_PATH that is not a system path (env modules,
#    spack, a hand-set lib dir) can still carry a foreign OpenSSL. We do not
#    strip it blindly (it may be intentional), but flag it so a later failure
#    has an obvious first thing to try.
if [ -z "$_lab" ] && [ -n "${LD_LIBRARY_PATH:-}" ] \
   && printf '%s' "$LD_LIBRARY_PATH" | tr ':' '\n' | grep -vqE '^(/usr/|/lib|/opt/dell|$)'; then
    echo "note: LD_LIBRARY_PATH has non-system entries below; if the build's psycopg2"
    echo "      check fails, 'unset LD_LIBRARY_PATH' and rerun:"
    printf '        %s\n' "$LD_LIBRARY_PATH"
fi

PG_LOCAL="$HOME/pg_local"
LIBNSL_LOCAL="$HOME/libnsl_local"
SECRETS_DIR="$REPO/.sledgehammer"
SECRETS_FILE="${SLEDGE_SECRETS_FILE:-$SECRETS_DIR/airflow-secrets.env.gpg}"
AIRFLOW_VERSION="${AIRFLOW_VERSION:-3.1.0}"
EDGE3_VERSION="${EDGE3_VERSION:-1.3.0}"
PYVER="${PYVER:-3.11}"
# Must match SLANG_VERSION in hammer/vlsi/rtl_check.py: the RTL fingerprint is a
# function of slang's AST JSON, and rtl_check refuses a version it was not pinned
# to rather than silently moving every stored hash.
SLANG_VERSION="${SLANG_VERSION:-11.0}"

step() { printf '\n=== %s ===\n' "$1"; }

if [ -n "$_lab" ]; then
    echo "profile: lab (SLEDGE_LAB=1): Hammer only, no Airflow / Postgres"
fi

# A checkout's first setup makes the lite stack its default; checkouts set up before keep the full one.
if [ -z "$_lab" ] && [ ! -e .venv ] && [ ! -e .sledgehammer ]; then
    mkdir -p .sledgehammer
    chmod 700 .sledgehammer
    printf '%s\n' "# sledgehammer starts the lite stack; set SLEDGE_LITE=0 here, or run 'sledgehammer full', for the full one." \
        "SLEDGE_LITE=1" > .sledgehammer/launch.env
    echo "new checkout: 'sledgehammer' will start the lite stack (scheduler, dag-processor and api-server, sized"
    echo "for a small VM); 'sledgehammer full' starts the full one. The default is in .sledgehammer/launch.env."
fi

step "uv"
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version

step "slang $SLANG_VERSION (RTL fingerprint)"
# hammer/vlsi/rtl_check.py shells out to this to fingerprint RTL: it hashes
# slang's own --ast-json output rather than a hand-rolled AST walk, so the tool
# itself is the dependency. Not a pip package -- pyslang exists but does not bind
# slang's ASTSerializer, so the CLI is the only way to reach it.
if [ -x "$REPO/tools/slang/slang" ] \
   && "$REPO/tools/slang/slang" --version 2>/dev/null | grep -q "$SLANG_VERSION"; then
    echo "already present: $("$REPO/tools/slang/slang" --version)"
else
    case "$(uname -s)/$(uname -m)" in
        Linux/x86_64)  _slang_asset="slang-linux-x86_64.tar.gz" ;;
        Darwin/arm64)  _slang_asset="slang-macos-arm64.tar.gz" ;;
        *)             _slang_asset="" ;;
    esac
    if [ -z "$_slang_asset" ]; then
        # No prebuilt asset for this platform (linux-arm64, intel macOS).
        echo "WARNING: no prebuilt slang for $(uname -s)/$(uname -m)."
        echo "         Install it yourself and point \$SLANG_BIN at it, e.g."
        echo "           conda install conda-forge::slang-verilog"
        echo "         or build from https://github.com/MikePopoloski/slang."
        echo "         Until then the RTL fingerprint falls back to a byte hash of the RTL"
        echo "         files and warns: comment and whitespace edits count as RTL changes."
    else
        mkdir -p "$REPO/tools/slang"
        curl -fsSL -o "$REPO/tools/slang/$_slang_asset" \
            "https://github.com/MikePopoloski/slang/releases/download/v${SLANG_VERSION}/${_slang_asset}"
        tar xzf "$REPO/tools/slang/$_slang_asset" -C "$REPO/tools/slang"
        rm -f "$REPO/tools/slang/$_slang_asset"
        chmod +x "$REPO/tools/slang/slang"
        "$REPO/tools/slang/slang" --version
    fi
fi

if [ -z "$_lab" ]; then
step "pg_config (psycopg2 builds from source)"
if [ ! -x "$PG_LOCAL/usr/bin/pg_config" ]; then
    tmp="$(mktemp -d)"
    ( cd "$tmp"
      dnf download libpq-devel
      dnf download libpq
      mkdir -p "$PG_LOCAL"
      rpm2cpio libpq-devel-*x86_64.rpm | ( cd "$PG_LOCAL" && cpio -idmv )
      rpm2cpio libpq-[0-9]*x86_64.rpm  | ( cd "$PG_LOCAL" && cpio -idmv ) )
    ln -sf libpq.so.5 "$PG_LOCAL/usr/lib64/libpq.so"
    rm -rf "$tmp"
fi
export PATH="$PG_LOCAL/usr/bin:$PATH"
pg_config --version

# The lab image ships libnsl system-wide (it is a Cadence runtime dependency
# the VM template installs), so this private copy is only for machines where
# you cannot dnf install.
step "libnsl (Cadence tools on RHEL 9)"
if [ ! -f "$LIBNSL_LOCAL/usr/lib64/libnsl.so.1" ]; then
    tmp="$(mktemp -d)"
    ( cd "$tmp"
      dnf download libnsl
      mkdir -p "$LIBNSL_LOCAL"
      rpm2cpio libnsl-*x86_64.rpm | ( cd "$LIBNSL_LOCAL" && cpio -idmv ) )
    rm -rf "$tmp"
fi
ls "$LIBNSL_LOCAL/usr/lib64/libnsl.so.1"

step "persist PATH in ~/.bashrc"
grep -q 'pg_local/usr/bin' "$HOME/.bashrc" 2>/dev/null || \
    printf '\nexport PATH="$HOME/.local/bin:$HOME/pg_local/usr/bin:$PATH"\n' >> "$HOME/.bashrc"
fi   # end of the default-profile-only prerequisites

step "virtual environment + dependencies"
if [ -n "$_lab" ]; then
    # home is NFS and the venv lives on the local /scratch disk. uv's cache
    # defaults to ~/.cache/uv (the NFS home), so hardlinking cache -> venv
    # crosses filesystems, fails, and warns "Failed to hardlink ... degraded
    # performance" on every file. Put the cache on the SAME disk as the venv so
    # the hardlinks succeed: fast and quiet, and it needs no per-file copy.
    export UV_CACHE_DIR="$REPO/.uv-cache"
    # Prefer the distro interpreter when the requested minor version is already
    # installed: it is patched by the distro rather than being one more private
    # copy nobody updates, and it saves a ~30 MB download per person. Fall back
    # to uv's standalone build when the system does not have it OR cannot run
    # (e.g. a pyenv shim that exists but exits non-zero) -- make the venv build
    # itself the test, not just "the file is executable".
    if [ ! -d .venv ]; then
        _syspy="$(command -v "python$PYVER" 2>/dev/null || true)"
        if [ -n "$_syspy" ] && uv venv --python "$_syspy"; then
            echo "  venv built on system interpreter $_syspy"
        else
            echo "  system python$PYVER absent or unusable; using uv's own build"
            uv python install "$PYVER"
            uv venv --python "$PYVER"
        fi
    fi
    uv lock
    # --no-dev: pytest, pyright, tox, Sphinx and pylint are not needed to run a flow.
    uv sync --no-dev
else
    uv python install "$PYVER"
    [ -d .venv ] || uv venv --python "$PYVER"
    uv lock
    uv sync --group dev
fi

if [ -z "$_lab" ]; then
step "airflow $AIRFLOW_VERSION"
source .venv/bin/activate
PYTHON_VERSION="$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
CONSTRAINT="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"
uv pip uninstall myst-parser mdit-py-plugins markdown-it-py >/dev/null 2>&1 || true
# Airflow 3 also opens an asyncpg engine on a Postgres metadata DB.
uv pip install "apache-airflow==${AIRFLOW_VERSION}" "asyncpg>=0.30.0,<1" --constraint "$CONSTRAINT"
# Edge worker support (constrained so it can't drag airflow to a newer release).
uv pip install "apache-airflow-providers-edge3==${EDGE3_VERSION}" --constraint "$CONSTRAINT"
# psycopg2 is not a base dependency (see pyproject.toml: it lives in the
# "cache" extra so a plain hammer install needs no compiler). The studio
# does need it, and needs it built here rather than as a wheel, so that the
# libraries sanitized above are the ones baked into it.
uv pip install "psycopg2==2.9.11" --no-binary psycopg2 --reinstall
AIRFLOW_HOME="$(mktemp -d)" airflow version

step "verify the compiled build is clean (no foreign library baked in)"
# The real safety net: whatever environment we failed to strip above, a
# tainted psycopg2 shows up here as an import error. Fail loudly with the fix
# instead of leaving a broken venv that only breaks later, at first DB use.
if _pg_msg="$(python3 -c "import psycopg2" 2>&1)"; then
    echo "  psycopg2 imports clean"
else
    echo "ERROR: psycopg2 was built against a foreign library and cannot load:" >&2
    printf '%s\n' "$_pg_msg" | sed 's/^/  /' >&2
    echo "  This means an environment was active that put a foreign OpenSSL/libpq" >&2
    echo "  on the linker path. Check for a conda/venv/module/spack environment or" >&2
    echo "  a non-system LD_LIBRARY_PATH, clear it, and rerun this script." >&2
    exit 1
fi
python3 -c "import asyncpg" || { echo "ERROR: asyncpg is missing; Airflow 3 needs it for a Postgres metadata DB." >&2; exit 1; }

step "put 'sledgehammer' on PATH (~/.local/bin)"
# A link to this one command, so the venv's python and pip do not shadow the system's.
mkdir -p "$HOME/.local/bin"
_link="$HOME/.local/bin/sledgehammer"
_old_link="$(readlink "$_link" 2>/dev/null || true)"
if [ -n "$_old_link" ] && [ "$(readlink -f "$_link")" != "$(readlink -f "$REPO/.venv/bin/sledgehammer")" ]; then
    if [ -e "$_link" ]; then
        echo "  was -> $_old_link"
    else
        echo "  was -> $_old_link (no longer exists)"
    fi
elif [ -z "$_old_link" ] && [ -e "$_link" ]; then
    echo "  replacing the file at $_link (a pip --user install?) with the link"
fi
ln -sfn "$REPO/.venv/bin/sledgehammer" "$HOME/.local/bin/sledgehammer"
"$HOME/.local/bin/sledgehammer" --help >/dev/null
# Any other sledgehammer on the caller's PATH or in its conda (a Chipyard env's, say) can run instead of this one.
_self="$(readlink -f "$REPO/.venv/bin/sledgehammer")"
_seen=""
IFS=: read -ra _dirs <<< "$_caller_path${_caller_conda_exe:+:$(dirname "$_caller_conda_exe")}"
for _d in "${_dirs[@]}"; do
    [ -n "$_d" ] && [ -f "$_d/sledgehammer" ] && [ -x "$_d/sledgehammer" ] || continue
    _r="$(readlink -f "$_d/sledgehammer")"
    case " $_seen " in *" $_r "*) continue ;; esac
    _seen="$_seen $_r"
    [ "$_r" = "$_self" ] && continue
    echo "  WARNING: $_d/sledgehammer is another copy; it runs instead of this one wherever $_d comes"
    echo "           first on PATH (check with: type -a sledgehammer). An older copy may run without"
    echo "           handing off to its checkout's venv; if it is stale, uninstall it from that environment."
done
echo "  $HOME/.local/bin/sledgehammer -> $REPO/.venv/bin/sledgehammer"

step "remove the old 'sledgehammer' shell function from ~/.bashrc"
# It shadowed the real command; only the block this script used to write is removed, and only now that the link works.
bash "$REPO/scripts/bashrc_drop_old_sledgehammer.sh" "$HOME/.bashrc"
fi   # end of the default-profile-only Airflow / Postgres block

step "hammer plugins (editable, any that sit next to this checkout)"
# Tech/PDK plugins (techname*, mentor, etc.) are separate packages, not deps of
# hammer-vlsi, so `uv sync` never installs them. When this checkout lives
# inside a design tree (e.g. chipyard/vlsi/hammer), the plugins are siblings:
# install any that are present so a chipyard integration is ready without a
# manual pip step. A standalone hammer clone simply finds none. Skip with
# SLEDGE_NO_PLUGINS=1.
if [ -z "${SLEDGE_NO_PLUGINS:-}" ]; then
    shopt -s nullglob
    _found_plugin=0
    for _plug in "$(dirname "$REPO")"/hammer-*-plugin*; do
        [ -f "$_plug/pyproject.toml" ] || [ -f "$_plug/setup.py" ] || continue
        echo "  installing $(basename "$_plug")"
        uv pip install -e "$_plug"
        _found_plugin=1
    done
    shopt -u nullglob
    [ "$_found_plugin" = 0 ] && echo "  none found next to $(dirname "$REPO") (standalone checkout; nothing to do)"
else
    echo "  skipped (SLEDGE_NO_PLUGINS set)"
fi

if [ -n "$_lab" ]; then
step "verify hammer-vlsi runs"
# Judged by its OUTPUT: hammer-vlsi looks up hammer-shell-test by name, and when
# the venv's scripts are not on PATH it prints one line to stderr and exits 1
# before any usage, so the usage block is the proof that it ran.
# Judge the WHOLE output, not the first line. stderr is unbuffered and stdout is
# block-buffered on a pipe, so a stray warning could otherwise become "line 1"
# and fail a working install. grep for the usage line anywhere in the output.
_out="$(PATH="$REPO/.venv/bin:$PATH" hammer-vlsi -h 2>&1 || true)"
if printf '%s\n' "$_out" | grep -q '^usage: hammer-vlsi'; then
    echo "  ok: $(printf '%s\n' "$_out" | grep -m1 '^usage:')"
else
    echo "ERROR: hammer-vlsi did not print its usage block." >&2
    printf '  got: %s\n' "${_out:-<no output at all>}" >&2
    exit 1
fi

step "persist PATH in ~/.bashrc"
# Appended, never prepended: .venv/bin carries its own python, python3 and pip,
# and putting it first would shadow the system ones for everything else.
_venv_bin="$REPO/.venv/bin"
if grep -qF "$_venv_bin" "$HOME/.bashrc" 2>/dev/null; then
    echo "  already present"
else
    printf '\n# hammer-vlsi (SledgeHammer). Appended on purpose; see scripts/uv_setup.sh.\nexport PATH="$PATH:%s"\n' "$_venv_bin" >> "$HOME/.bashrc"
    echo "  added to ~/.bashrc"
fi
fi

if [ -z "$_lab" ]; then
step "local Postgres (Airflow's database and the cache)"
_local_pg=""
if [ -f "$SECRETS_FILE" ]; then
    echo "  skipped: $SECRETS_FILE already names a Postgres"
elif [ ! -t 0 ]; then
    echo "  no tty, skipping. Create one later: ./scripts/sledge-local-postgres.sh"
else
    read -rp "  set up a local Postgres for SledgeHammer now? [Y/n]: " DO_LOCAL_PG
    case "${DO_LOCAL_PG,,}" in
        n|no)
            echo "  skipped. Create one later: sledgehammer postgres use local"
            ;;
        *)
            if PATH="$PATH:$_path_before_cleanup" "$REPO/scripts/sledge-local-postgres.sh" setup; then
                while IFS='=' read -r _k _v; do
                    case "$_k" in
                        SLEDGE_PG_DIR|SLEDGE_PG_HOST|SLEDGE_PG_PORT|SLEDGE_PG_USER|SLEDGE_PG_DB|SLEDGE_PG_PASSWORD_FILE) export "$_k=$_v" ;;
                    esac
                done < <("$REPO/scripts/sledge-local-postgres.sh" env)
                _local_pg=1
            else
                echo "  the local Postgres setup did not finish; the secrets step below asks which Postgres to use."
            fi
            ;;
    esac
fi
fi

if [ -z "$_lab" ]; then
step "secrets (committed airflow.cfg ships blank; create the encrypted env)"
if [ -f "$SECRETS_FILE" ]; then
    echo "already present: $SECRETS_FILE"
elif [ ! -t 0 ]; then
    # No terminal to answer a prompt from (a script, a provisioning hook, a CI
    # job). Do not fail the whole install at the last step; say what to do.
    echo "  no tty, skipping the secrets prompt. Airflow won't start until these exist."
    echo "  create them later: ./scripts/sledge-secrets-create.sh"
else
    read -rp "  set up Postgres secrets now? [Y/n]: " DO_SECRETS
    if [ "${DO_SECRETS,,}" = "n" ]; then
        echo "  skipped. Airflow won't start until these exist."
        echo "  create them later: ./scripts/sledge-secrets-create.sh"
    else
        "$REPO/scripts/sledge-secrets-create.sh"
        if [ -n "$_local_pg" ]; then
            # postgres.env beside the secrets is where studio and hammer-vlsi outside Airflow find this Postgres.
            SLEDGE_SECRETS_FILE="$SECRETS_FILE" "$REPO/.venv/bin/python" -I -c \
                'import sys; from hammer.shell import sledgehammer_postgres as p; sys.exit(p.write_local_target(sys.argv[1]))' \
                "${SLEDGE_PG_DIR:-$HOME/.sledgehammer/postgres}" \
                || echo "  to point studio and hammer-vlsi outside Airflow at it, run: sledgehammer postgres use local"
        fi
    fi
fi
fi

step "done"
if [ -n "$_lab" ]; then
cat <<EOF
Hammer is installed in $REPO/.venv and its scripts are on PATH for new bash
shells, through ~/.bashrc. zsh, csh, or a login shell that skips ~/.bashrc,
needs the same PATH entry in its own startup file.
For this shell:
    export PATH="\$PATH:$REPO/.venv/bin"
Check it works with:
    hammer-vlsi -h
If it prints "hammer-shell does not appear to be on the path" instead of its
usage, PATH is wrong: open a new shell or run the export above.
EOF
else
cat <<EOF
Setup complete. Open a new shell, then start the stack from any directory with:
    sledgehammer
(first launch runs the DB migrations automatically.) Register a design's DAG
once with 'sledgehammer buildfile' from its vlsi directory, then run flows there
with 'sledgehammer syn', 'sledgehammer par', ..., or from anywhere with
'sledgehammer syn -t <design>'.
PATH was added to ~/.bashrc only. If your shell does not read it (zsh, csh, or
a login shell that skips ~/.bashrc), add $HOME/.local/bin to PATH yourself.
EOF
fi
