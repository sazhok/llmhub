#! /bin/bash
# Graceful stop, then verify the process is actually gone.
#
# ochat's stop_all_llm_gateway.sh returns the moment it touches a sentinel, which lets a
# restart race a still-draining worker; ochat/control/stop_all.sh had to be written
# specifically to poll for real exit. This does the polling version from the start.

wd=.
[ ! -f $wd/serve.sh ] && wd=..
[ ! -f $wd/serve.sh ] && echo "ERROR: run from the llmhub root or its control/ dir" && exit 1
cd $wd

touch .exit_llmhub
echo "Requested graceful stop (.exit_llmhub)"

pid=""
[ -f llmhub.pid ] && pid=$(cat llmhub.pid)
[ -n "$pid" ] && kill "$pid" 2>/dev/null

for i in $(seq 1 30); do
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        echo "llmhub stopped."
        rm -f .exit_llmhub llmhub.pid
        exit 0
    fi
    sleep 1
done

echo "WARNING: still alive after 30s (PID $pid)." >&2
echo "         Inspect logs/llmhub.app.log, then: kill $pid" >&2
exit 1
