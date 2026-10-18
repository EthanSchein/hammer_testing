#!/usr/bin/env bash
# Runs the lite stack (scheduler without its log server, dag-processor, api-server), restarting whichever component crashes and stopping all three on Ctrl-C.
set -u

# Whole seconds; anything else means the default, so a typo cannot stop the supervisor halfway.
whole() {
    case $1 in
        ''|*[!0-9]*) echo "$2" ;;
        *) local n=${1#"${1%%[!0]*}"}
           if [ "${#n}" -le 6 ]; then echo "${n:-0}"; else echo 999999; fi ;;
    esac
}

poll=${SLEDGE_LITE_POLL:-2}
stop_timeout=$(whole "${SLEDGE_LITE_STOP_TIMEOUT:-}" 30)
restart_delay=$(whole "${SLEDGE_LITE_RESTART_DELAY:-}" 5)
restart_for=$(whole "${SLEDGE_LITE_RESTART_FOR:-}" 900)
if [ $((restart_for % 60)) = 0 ]; then restart_for_text="$((restart_for / 60)) min"; else restart_for_text="${restart_for}s"; fi
# The scheduler is running, draining (the rest of its group was told to stop) or waiting to restart, until sched_until.
sched=running
sched_until=0
sched_rc=0
# When the scheduler's current run of exits within a minute of starting began.
failing_since=0
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
    if [ "$sched" = running ] && ! leader_alive 0; then
        wait "${pids[0]}"
        sched_rc=$?
        if [ $((SECONDS - started[0])) -lt 60 ]; then
            quick_exits[0]=$((quick_exits[0] + 1))
            [ "${quick_exits[0]}" = 1 ] && failing_since=$SECONDS
        else
            quick_exits[0]=0
        fi
        # Its group holds the running tasks, so they get the same time to wind down as on Ctrl-C.
        echo "[lite] the scheduler exited (status $sched_rc); giving its tasks up to ${stop_timeout}s to stop"
        signal_group TERM 0
        sched=draining
        sched_until=$((SECONDS + stop_timeout))
    elif [ "$sched" = draining ] && { ! group_alive 0 || [ "$SECONDS" -ge "$sched_until" ]; }; then
        ! group_alive 0 || signal_group KILL 0
        # Its pid may now go to another process, so nothing signals it until the next start.
        pids[0]=0
        if [ "${quick_exits[0]}" -gt 0 ] && [ $((SECONDS - failing_since)) -ge "$restart_for" ]; then
            echo "[lite] the scheduler has kept exiting within a minute of starting for ${restart_for_text} (last" \
                 "status $sched_rc); stopping the dag-processor and api-server. Fix the error above and start" \
                 "sledgehammer again."
            stop_all
            exit "$sched_rc"
        fi
        if [ "${quick_exits[0]}" = 5 ]; then
            echo "[lite] the scheduler exited within a minute of starting 5 times in a row; it keeps being restarted," \
                 "a minute apart at most, until it stays up or ${restart_for_text} have passed. Ctrl-C stops the stack."
        fi
        # A scheduler that keeps failing (its database is down, say) waits longer before each restart.
        delay=$((restart_delay << (quick_exits[0] > 7 ? 6 : (quick_exits[0] > 0 ? quick_exits[0] - 1 : 0))))
        [ "$delay" -le 60 ] || delay=60
        echo "[lite] restarting the scheduler in ${delay}s"
        sched=waiting
        sched_until=$((SECONDS + delay))
    elif [ "$sched" = waiting ] && [ "$SECONDS" -ge "$sched_until" ]; then
        sched=running
        start 0
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
