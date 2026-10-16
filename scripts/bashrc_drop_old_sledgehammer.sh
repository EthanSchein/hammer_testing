#!/usr/bin/env bash
# Remove the sledgehammer() shell function older uv_setup.sh versions wrote into a bashrc, and
# leave a marker line so an older checkout's setup does not add it back.
# Usage: bashrc_drop_old_sledgehammer.sh <rcfile>
set -euo pipefail
rc="${1:?usage: bashrc_drop_old_sledgehammer.sh <rcfile>}"
[ -f "$rc" ] || { echo "  none found"; exit 0; }

if grep -q '^sledgehammer() {$' "$rc"; then
    tmp="$(mktemp)"
    if awk '
        /^sledgehammer\(\) \{$/ { blk = $0; inblk = 1; next }
        inblk { blk = blk "\n" $0
                if ($0 == "}") { if (blk ~ /airflow-standalone-ldap\.py/) dropped = 1; else print blk; inblk = 0 }
                next }
        { print }
        END { if (inblk) print blk; exit !dropped }
    ' "$rc" > "$tmp"; then
        cp "$rc" "$rc.sledgehammer.bak"
        cat "$tmp" > "$rc"
        echo "  removed (backup: $rc.sledgehammer.bak); open a new shell or run: unset -f sledgehammer"
    else
        echo "  a different sledgehammer() is defined there; left alone, but it shadows the real command"
    fi
    rm -f "$tmp"
else
    echo "  none found"
fi

# Older setups skip adding their function when this text is already in the file.
if ! grep -qF 'sledgehammer()' "$rc"; then
    [ -z "$(tail -c1 "$rc")" ] || printf '\n' >> "$rc"
    echo "# sledgehammer() shell function retired by scripts/uv_setup.sh; the command is ~/.local/bin/sledgehammer" >> "$rc"
fi
