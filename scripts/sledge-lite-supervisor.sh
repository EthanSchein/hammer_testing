#!/usr/bin/env bash
# Runs the lite stack (scheduler without its log server, dag-processor, api-server), restarting a crashed dag-processor or api-server and stopping all three when the scheduler exits or on Ctrl-C.
set -u

poll=${SLEDGE_LITE_POLL:-2}
stop_timeout=${SLEDGE_LITE_STOP_TIMEOUT:-30}
names=(scheduler dag-processor api-server)
commands=("scheduler --skip-serve-logs" "dag-processor" "api-server")
pids=(0 0 0)
wanted=(0 0 0)
started=(0 0 0)
quick_exits=(0 0 0)

start() {
    local i=$1
    # Each component gets its own process group; its log prefixer ignores the stop signals so it drains the last lines.
    set -m
    # shellcheck disable=SC2086
    command airflow ${commands[$i]} > >(trap '' INT TERM HUP
        while IFS= read -r line || [ -n "$line" ]; do printf '%s | %s\n' "${names[$i]}" "$line"; done) 2>&1 &
    pids[$i]=$!
    set +m
    wanted[$i]=1
    started[$i]=$SECONDS
    echo "[lite] started ${names[$i]} (pid ${pids[$i]})"
}

leader_alive() {
    [ "${pids[$1]}" -gt 0 ] && kill -0 "${pids[$1]}" 2>/dev/null
}

group_alive() {
    [ "${pids[$1]}" -gt 0 ] && kill -0 -- "-${pids[$1]}" 2>/dev/null
}

signal_group() {
    [ "${pids[$2]}" -gt 0 ] || return 0
    kill "-$1" -- "-${pids[$2]}" 2>/dev/null || kill "-$1" "${pids[$2]}" 2>/dev/null
}

wait_groups() {
    local deadline=$1 i busy
    shift
    while [ "$SECONDS" -lt "$deadline" ]; do
        busy=0
        for i in "$@"; do
            group_alive "$i" && busy=1
        done
        [ "$busy" = 0 ] && return 0
        sleep 1
    done
    return 1
}

stop_all() {
    trap '' INT TERM HUP
    local i deadline=$((SECONDS + stop_timeout))
    # Tasks report to the api-server while they wind down, so it stops last.
    signal_group TERM 0
    signal_group TERM 1
    wait_groups "$deadline" 0
    signal_group TERM 2
    [ "$deadline" -gt $((SECONDS + 5)) ] || deadline=$((SECONDS + 5))
    wait_groups "$deadline" 0 1 2
    for i in 0 1 2; do
        if group_alive "$i"; then
            echo "[lite] ${names[$i]} did not stop within ${stop_timeout}s; killing it"
            signal_group KILL "$i"
        fi
    done
    wait 2>/dev/null
}

trap 'stop_all; exit 130' INT
trap 'stop_all; exit 143' TERM
trap 'stop_all; exit 129' HUP

for i in 0 1 2; do
    start "$i"
done
while :; do
    sleep "$poll" &
    wait $!
    if ! leader_alive 0; then
        wait "${pids[0]}"
        rc=$?
        echo "[lite] the scheduler exited (status $rc); stopping the dag-processor and api-server"
        stop_all
        exit "$rc"
    fi
    for i in 1 2; do
        if [ "${wanted[$i]}" = 0 ] || leader_alive "$i"; then
            continue
        fi
        wait "${pids[$i]}"
        rc=$?
        if [ $((SECONDS - started[i])) -lt 60 ]; then
            quick_exits[$i]=$((quick_exits[i] + 1))
        else
            quick_exits[$i]=0
        fi
        signal_group TERM "$i"
        wait_groups $((SECONDS + 5)) "$i" || signal_group KILL "$i"
        if [ "${quick_exits[$i]}" -lt 3 ]; then
            echo "[lite] ${names[$i]} exited (status $rc); restarting it"
            start "$i"
        elif [ "$i" = 2 ]; then
            echo "[lite] the api-server exited (status $rc) within a minute of starting, 3 times in a row;" \
                 "stopping the stack, because tasks report through it"
            stop_all
            exit 1
        else
            echo "[lite] the dag-processor exited (status $rc) within a minute of starting, 3 times in a row;" \
                 "not restarting it. Registered DAGs still run, but new or changed DAG files are not picked up" \
                 "until you restart sledgehammer."
            wanted[$i]=0
        fi
    done
done
