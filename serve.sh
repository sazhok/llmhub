#! /bin/bash

mode=${1:-bg}  # 'fg' - foreground, 'stop' - just stop, anything else - background via nohup

cd "$(dirname "$0")"
source .venv/bin/activate

mkdir -p logs

# Loopback, not the tailnet IP. Both clients of this service run on this box: fw's llmmon
# orders, and ochat's nine gateway workers poll. asrhub and mediahub bind the Tailscale IP
# only because they have off-host clients (STT workers, har); llmhub has none, so the smaller
# surface is free. 0.0.0.0 stays forbidden - this box has a public IP and constant scanner
# traffic. If an off-host client ever appears, change this to 100.97.153.111 and say here why.
HOST=127.0.0.1
PORT=8008   # 8000-8007 are taken; see ../CLAUDE.md's port table

# Stop whatever is already on the port, and wait for the new process to actually answer.
#
# Copied from mediahub/serve.sh, which learned it the expensive way: without this, the second
# uvicorn exits with "[Errno 98] address already in use" *into the rotated log*, nohup returns
# 0, the script prints "Started with PID ...", and the OLD process keeps serving. Health
# checks pass and only the new routes are missing. It happened again here on 2026-09-05, so
# it is not a historical curiosity.
stop_existing() {
    local pids
    pids=$(ss -ltnpH "src $HOST:$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u || true)
    [ -z "$pids" ] && return 0
    echo "stopping $pids on $HOST:$PORT"
    kill $pids 2>/dev/null || true
    for _ in $(seq 20); do
        sleep 0.5
        pids=$(ss -ltnpH "src $HOST:$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u || true)
        [ -z "$pids" ] && return 0
    done
    echo "still listening on $HOST:$PORT after 10s: $pids" >&2
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
