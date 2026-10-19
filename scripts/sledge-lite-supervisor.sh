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
watch_every=$(whole "${SLEDGE_LITE_WATCH_EVERY:-}" 30)
stuck_after=$(whole "${SLEDGE_LITE_STUCK_AFTER:-}" 120)
health_url="http://127.0.0.1:$(whole "${AIRFLOW__API__PORT:-}" 0)/api/v2/monitor/health"
command -v curl >/dev/null 2>&1 ||
    echo "[lite] curl is not installed, so a scheduler that hangs without exiting is not restarted"
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
# When the scheduler was first seen stuck, 0 while it is not.
stuck_since=0
next_watch=0
told_waiting=0
told_api=0

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

# Prints stuck when the api-server says the database answers but the scheduler stopped heartbeating, unknown if it cannot tell.
scheduler_health() {
    local body
    body=$(curl -q -s -f -m 5 --noproxy '*' "$health_url" 2>/dev/null) || { echo unknown; return; }
    body=${body//[[:space:]]/}
    case $body in
        *'"metadatabase":{"status":"healthy"'*) ;;
        *) echo unknown; return ;;
    esac
    case $body in
        *'"scheduler":{"status":"healthy"'*) echo healthy ;;
        *'"scheduler":{"status":"unhealthy"'*) echo stuck ;;
        *) echo unknown ;;
    esac
}

# Prints yes while a task process (a live child of a scheduler worker that is not idle) runs, unknown if ps cannot tell.
scheduler_tasks() {
    ps -A -o pid=,ppid=,stat=,args= 2>/dev/null | awk -v s="${pids[0]}" '
        { up[$1] = $2; st[$1] = $3; if (/LocalExecutor: <idle>/) idle[$1] = 1 }
        END {
            if (!(s in up)) { print "unknown"; exit }
            for (p in up)
                if (st[p] !~ /^Z/ && (up[p] in up) && up[up[p]] == s && !(up[p] in idle)) { print "yes"; exit }
            print "no"
        }'
}

# Restarts a scheduler that has stopped heartbeating although its database answers, once no task runs under it.
watch_scheduler() {
    local health tasks=unknown
    if [ "${state[0]}" != running ] || ! leader_alive 0; then
        stuck_since=0
        told_waiting=0
        return 0
    fi
    # A new scheduler is first checked two rounds after it starts, so it has time to report a heartbeat.
    [ "$SECONDS" -ge "$next_watch" ] && [ $((SECONDS - started[0])) -ge $((2 * watch_every)) ] || return 0
    next_watch=$((SECONDS + watch_every))
    health=$(scheduler_health)
    [ "$health" = stuck ] && tasks=$(scheduler_tasks)
    [ "$health" = healthy ] && told_waiting=0
    if [ "$tasks" = yes ] && [ "$told_waiting" = 0 ]; then
        echo "[lite] the scheduler has stopped scheduling but still runs a task; it is restarted after that task ends"
        told_waiting=1
    fi
    if [ "$tasks" != no ]; then
        stuck_since=0
    elif [ "$stuck_since" = 0 ]; then
        stuck_since=$SECONDS
    elif [ $((SECONDS - stuck_since)) -ge "$stuck_after" ]; then
        echo "[lite] the scheduler has stopped scheduling for over ${stuck_after}s without exiting, and no task runs" \
             "under it; stopping it so it can be restarted. Its processes:"
        { ps -A -o pgid,pid,ppid,stat,wchan:32,args 2>/dev/null || ps -A -o pgid,pid,ppid,stat,wchan,args 2>/dev/null; } |
            awk -v g="${pids[0]}" 'NR == 1 || $1 == g { print "[lite]   " $0 }'
        quick_exits[0]=0
        signal_group TERM 0
        state[0]=draining
        due[0]=$((SECONDS + drain_for[0]))
    fi
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
        2) if [ "${state[0]}" = running ] && leader_alive 0 && [ "$(scheduler_tasks)" != no ]; then
               [ "$told_api" = 1 ] || echo "[lite] the api-server has kept exiting within a minute of starting for" \
                   "${restart_for_text} (last status ${last_rc[2]}), but a task still runs under the scheduler; it" \
                   "keeps being restarted, and the stack stops once no task runs. Fix the error above."
               told_api=1
               return 1
           fi
           echo "[lite] the api-server has kept exiting within a minute of starting for ${restart_for_text} (last" \
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
                    [ "$i" != 2 ] || told_api=0
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
                if group_alive "$i" && leader_alive "$i"; then
                    echo "[lite] the ${names[$i]} did not stop within ${drain_for[$i]}s; killing it"
                    signal_group KILL "$i"
                    wait "${pids[$i]}" 2>/dev/null
                fi
                ! group_alive "$i" || signal_group KILL "$i"
                # Its pid may now go to another process, so nothing signals it until the next start.
                pids[$i]=0
                if [ "${quick_exits[$i]}" -gt 0 ] && [ $((SECONDS - failing_since[i])) -ge "$restart_for" ]; then
                    give_up "$i" && continue
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
    watch_scheduler
done
