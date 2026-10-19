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
names=(scheduler dag-processor api-server)
commands=("scheduler --skip-serve-logs" "dag-processor" "api-server")
pids=(0 0 0)
started=(0 0 0)
quick_exits=(0 0 0)
# When each component's current run of exits within a minute of starting began.
failing_since=(0 0 0)
last_rc=(0 0 0)
# Each component is running, off, or draining (the rest of its group was told to stop) or waiting to restart until due[i].
state=(running running running)
due=(0 0 0)
# The scheduler's group holds the running tasks, so it gets the same time to wind down as on Ctrl-C.
drain_for=("$stop_timeout" 5 5)
# Running tasks report through the api-server, so it is never left down for long.
delay_cap=(60 60 15)

start() {
    local i=$1
    # Each component gets its own process group; its log prefixer ignores the stop signals so it drains the last lines.
    set -m
    # shellcheck disable=SC2086
    command airflow ${commands[$i]} > >(trap '' INT TERM HUP
        while IFS= read -r line || [ -n "$line" ]; do printf '%s | %s\n' "${names[$i]}" "$line"; done) 2>&1 &
    pids[$i]=$!
    set +m
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

give_up() {
    local i=$1
    case $i in
        0) echo "[lite] the scheduler has kept exiting within a minute of starting for ${restart_for_text} (last" \
                "status ${last_rc[0]}); stopping the dag-processor and api-server. Fix the error above and start" \
                "sledgehammer again."
           stop_all
           exit "${last_rc[0]}" ;;
        1) echo "[lite] the dag-processor has kept exiting within a minute of starting for ${restart_for_text} (last" \
                "status ${last_rc[1]}); not restarting it. Registered DAGs still run, but new or changed DAG files" \
                "are not picked up until you restart sledgehammer."
           state[1]=off ;;
        2) echo "[lite] the api-server has kept exiting within a minute of starting for ${restart_for_text} (last" \
                "status ${last_rc[2]}); stopping the stack, because tasks report through it. Fix the error above" \
                "and start sledgehammer again."
           stop_all
           exit 1 ;;
    esac
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
    for i in 0 1 2; do
        case ${state[$i]} in
            running)
                leader_alive "$i" && continue
                wait "${pids[$i]}"
                last_rc[$i]=$?
                if [ $((SECONDS - started[i])) -lt 60 ]; then
                    quick_exits[$i]=$((quick_exits[i] + 1))
                    [ "${quick_exits[$i]}" = 1 ] && failing_since[$i]=$SECONDS
                else
                    quick_exits[$i]=0
                fi
                if [ "$i" = 0 ]; then
                    echo "[lite] the scheduler exited (status ${last_rc[0]}); giving its tasks up to ${stop_timeout}s to stop"
                else
                    echo "[lite] the ${names[$i]} exited (status ${last_rc[$i]})"
                fi
                signal_group TERM "$i"
                state[$i]=draining
                due[$i]=$((SECONDS + drain_for[i])) ;;
            draining)
                group_alive "$i" && [ "$SECONDS" -lt "${due[$i]}" ] && continue
                ! group_alive "$i" || signal_group KILL "$i"
                # Its pid may now go to another process, so nothing signals it until the next start.
                pids[$i]=0
                if [ "${quick_exits[$i]}" -gt 0 ] && [ $((SECONDS - failing_since[i])) -ge "$restart_for" ]; then
                    give_up "$i"
                    continue
                fi
                if [ "${quick_exits[$i]}" = 5 ]; then
                    if [ "${delay_cap[$i]}" = 60 ]; then apart="a minute"; else apart="${delay_cap[$i]}s"; fi
                    echo "[lite] the ${names[$i]} exited within a minute of starting 5 times in a row; it keeps being" \
                         "restarted, $apart apart at most, until it stays up or ${restart_for_text} have passed." \
                         "Ctrl-C stops the stack."
                fi
                # A component that keeps failing (its database is down, say) waits longer before each restart.
                delay=$((restart_delay << (quick_exits[i] > 7 ? 6 : (quick_exits[i] > 0 ? quick_exits[i] - 1 : 0))))
                [ "$delay" -le "${delay_cap[$i]}" ] || delay=${delay_cap[$i]}
                echo "[lite] restarting the ${names[$i]} in ${delay}s"
                state[$i]=waiting
                due[$i]=$((SECONDS + delay)) ;;
            waiting)
                [ "$SECONDS" -ge "${due[$i]}" ] || continue
                state[$i]=running
                start "$i" ;;
        esac
    done
done
