#! /bin/bash

mode=${1:-bg}  # 'fg' - foreground, 'stop' - just stop, anything else - background via nohup

cd "$(dirname "$0")"
source .venv/bin/activate

mkdir -p logs

# The tailnet IP, because the two clients are NOT both on this box: ochat's gateway workers
# poll from here, but the producer does not - `fw`'s llmmon runs on oa, qd and az, and only
# there (2026-09-05: `ps` on all three, none on bq). A queue bound to loopback is one nothing
# can order into, and llmmon deletes the job file whether or not the POST succeeded
# (fw/llmmon.py:624), so an unreachable hub is a lost call rather than a retried one. Same
# reason asrhub's gateway binds this address for its off-host workers. 0.0.0.0 stays
# forbidden - this box has a public IP and constant scanner traffic.
#
# Set LLMHUB_HOST=127.0.0.1 in .env to go back to loopback (nothing off-box can order then).
set -a; [ -f .env ] && source .env; set +a
HOST=${LLMHUB_HOST:-100.97.153.111}
PORT=8008   # 8000-8007 are taken; see ../CLAUDE.md's port table

# Off the loopback the shim is reachable by anything on the tailnet, and with no
# LLMHUB_BASIC_USERS it authenticates nothing (auth.require_basic returns "anonymous"). That
# combination is refused here rather than logged at boot and forgotten.
if [ "$HOST" != "127.0.0.1" ] && [ -z "${LLMHUB_BASIC_USERS//,/}" ]; then
    echo "refusing to bind $HOST with no LLMHUB_BASIC_USERS - the shim would be open" >&2
    exit 1
fi

# Stop whatever is already on the port, and wait for the new process to actually answer.
#
# Copied from mediahub/serve.sh, which learned it the expensive way: without this, the second
# uvicorn exits with "[Errno 98] address already in use" *into the rotated log*, nohup returns
# 0, the script prints "Started with PID ...", and the OLD process keeps serving. Health
# checks pass and only the new routes are missing. It happened again here on 2026-09-05, so
# it is not a historical curiosity.
# Matched by PORT on any address, not by $HOST:$PORT: on 2026-09-05 the bind moved from
# loopback to the tailnet IP and the address-scoped version saw nothing to stop, so the old
# process kept serving 127.0.0.1:8008 beside the new one - two uvicorns on one database.
listeners() {
    ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u || true
}

stop_existing() {
    local pids
    pids=$(listeners)
    [ -z "$pids" ] && return 0
    echo "stopping $pids on port $PORT"
    kill $pids 2>/dev/null || true
    for _ in $(seq 20); do
        sleep 0.5
        pids=$(listeners)
        [ -z "$pids" ] && return 0
    done
    echo "still listening on port $PORT after 10s: $pids" >&2
    return 1
}

wait_healthy() {
    for _ in $(seq 40); do
        sleep 0.25
        if curl -fsS -m 2 "http://$HOST:$PORT/health" >/dev/null 2>&1; then
            return 0
        fi
    done
    echo "no answer on http://$HOST:$PORT/health - see logs/llmhub.startup.log" >&2
    return 1
}

stop_existing || exit 1

if [ "$mode" == "stop" ]; then
    rm -f llmhub.pid
    echo "stopped"
    exit 0
fi

if [ "$mode" == "fg" ]; then
    exec uvicorn llmhub.app:app --host "$HOST" --port "$PORT"
fi

echo "Running llmhub in background..."
nohup uvicorn llmhub.app:app --host "$HOST" --port "$PORT" \
    --log-config uvicorn_log_config.json &> logs/llmhub.startup.log &
echo $! > llmhub.pid
if wait_healthy; then
    echo "Started with PID $(cat llmhub.pid), logging to logs/llmhub.log (rotated)"
else
    exit 1
fi
