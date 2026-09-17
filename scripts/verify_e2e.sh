#! /bin/bash
#
# The contract gate: a real llmhub, a real Postgres, a real mediahub, and the legacy wire.
#
# `pytest` proves the modules; this proves the thing a client actually talks to. It runs
# against a reserved fixture company (never a real one), takes an order through the shim,
# drains it with scripts/fake_worker.py, checks the answers landed in mediahub, and then does
# the part unit tests cannot: kills a worker mid-batch and shows the reaper re-handing the
# remainder with a new token while the dead holder's answer is refused.
#
#   bash scripts/verify_e2e.sh            # full run
#   bash scripts/verify_e2e.sh --quick    # skip the lease-expiry section (~30s faster)
#
# Exits non-zero on the first failed check. Cleans up its own rows either way.

set -uo pipefail
cd "$(dirname "$0")/.."

QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1

COMPANY="test-9901"
UUID="e2e-$(date +%s)-$$"
BASE="${LLMHUB_URL:-http://100.97.153.111:8008}"
URL="$BASE/hub/v1/worker_acceptor_light.php"
PY=.venv/bin/python
FAILURES=0
STARTED_SERVER=0
FIXTURE_ROOT=""

# shellcheck disable=SC1091
source .venv/bin/activate

# Credentials are read into variables and never echoed.
BASIC=$($PY - <<'PYEOF'
from env_secrets import get_env_secret
raw = (get_env_secret("LLMHUB_BASIC_USERS") or "").split(",")[0].strip()
print(raw)
PYEOF
)
if [ -z "$BASIC" ]; then
    echo "LLMHUB_BASIC_USERS is not set - the shim cannot be exercised" >&2
    exit 2
fi

ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

cleanup() {
    step "cleanup"
    $PY - "$COMPANY" "$UUID" <<'PYEOF' 2>/dev/null
import sys
from llmhub import db
company, call_uuid = sys.argv[1], sys.argv[2]
db.init_pool()
conn = db.connect()
conn.execute("DELETE FROM orders WHERE company_id = %s", (company,))
conn.execute("DELETE FROM sources WHERE company_id = %s", (company,))
conn.commit()
db.release(conn); db.close_pool()
PYEOF
    $PY - "$UUID" <<'PYEOF' 2>/dev/null
import asyncio, sys
from llmhub import mediahub_client as mh
async def main():
    if mh.configured():
        await mh._client(20.0).delete(f"/v1/calls/{sys.argv[1]}",
                                      params={"expect_source": "llmhub"})
        await mh.close()
asyncio.run(main())
PYEOF
    rm -rf "work/spool/$UUID" "work/spool/results/$UUID"
    [ -n "${FIXTURE_ROOT:-}" ] && rm -rf "$FIXTURE_ROOT"
    if [ "$STARTED_SERVER" = "1" ]; then
        bash serve.sh stop >/dev/null 2>&1
    fi
    echo "  removed the fixture company, its orders, and the mediahub call"
}
trap cleanup EXIT

# --------------------------------------------------------------------------------------
step "0. a running llmhub"
# --------------------------------------------------------------------------------------
if curl -fsS -m 3 "$BASE/health" >/dev/null 2>&1; then
    ok "already serving on $BASE"
else
    echo "  starting one..."
    bash serve.sh >/dev/null 2>&1 || { echo "could not start llmhub" >&2; exit 2; }
    STARTED_SERVER=1
    ok "started"
fi
HEALTH=$(curl -fsS -m 5 "$BASE/health")
echo "$HEALTH" | grep -q '"db":true' && ok "database reachable" || bad "health says $HEALTH"

# --------------------------------------------------------------------------------------
step "1. a fixture company with a fixture taskset"
# --------------------------------------------------------------------------------------
FIXTURE_ROOT=$(mktemp -d)
mkdir -p "$FIXTURE_ROOT/$COMPANY/tasksets/1"
for n in script 1 2 3; do
    mkdir -p "$FIXTURE_ROOT/$COMPANY/tasksets/1/$n"
    cat > "$FIXTURE_ROOT/$COMPANY/tasksets/1/$n/config.json" <<JSON
{"name": "$n", "prompt": "e2e fixture question $n", "yes_no": "так/ні"}
JSON
done
$PY - "$COMPANY" "$FIXTURE_ROOT" <<'PYEOF' || bad "could not create the fixture source"
import sys
from llmhub import db, sources
company, root = sys.argv[1], sys.argv[2]
db.init_pool(); db.ensure_schema()
sources.upsert(company, params_dir=root, note="verify_e2e.sh fixture")
sources.set_enabled(company, True, params_root=root)
db.close_pool()
PYEOF
ok "source $COMPANY enabled against $FIXTURE_ROOT"

# --------------------------------------------------------------------------------------
step "2. ordering (op=ordering)"
# --------------------------------------------------------------------------------------
VTT=$'WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nдобрий день, це служба підтримки\n\n00:00:05.000 --> 00:00:09.000\nмене цікавить вартість доставки до Львова\n'
CODE=$(curl -s -o /tmp/e2e_order.$$ -w '%{http_code}' -u "$BASIC" -X POST "$URL" \
    --data-urlencode "task=llm" --data-urlencode "op=ordering" \
    --data-urlencode "company=$COMPANY" --data-urlencode "task_set=1" \
    --data-urlencode "uuid=$UUID" \
    --data-urlencode "vtt=$VTT" --data-urlencode "language=uk" \
    --data-urlencode "url=https://example.invalid/webhooks" \
    --data-urlencode "sequrity_key=E2E-CUSTOMER-TOKEN")
[ "$CODE" = "200" ] && ok "HTTP 200" || bad "ordering answered $CODE, not 200"
[ ! -s /tmp/e2e_order.$$ ] && ok "empty body, as the PHP always answers" \
    || bad "ordering returned a body: $(head -c 100 /tmp/e2e_order.$$)"
rm -f /tmp/e2e_order.$$

$PY - "$UUID" <<'PYEOF' && ok "the order is ready" || bad "the order is not ready"
import sys
from llmhub import db, dispatch
db.init_pool()
found = dispatch.get_order(sys.argv[1])
db.close_pool()
order = found["order"] if found else None
if order:
    print(f"     state={order['state']} speech={order['speech_chars']}c "
          f"body={order['body_state']}")
sys.exit(0 if order and order["state"] == "ready" else 1)
PYEOF

# --------------------------------------------------------------------------------------
step "3. peek: the batch's shape"
# --------------------------------------------------------------------------------------
curl -s -u "$BASIC" -X POST "$URL" -D /tmp/e2e_head.$$ \
    --data-urlencode "task=llm" --data-urlencode "worker_id=e2e-shape" \
    -o /tmp/e2e_batch.$$
grep -qi 'content-type: text/html' /tmp/e2e_head.$$ \
    && ok "Content-Type is text/html, as PHP's default is" \
    || bad "Content-Type is $(grep -i content-type /tmp/e2e_head.$$)"

$PY - /tmp/e2e_batch.$$ "$UUID" "$COMPANY" <<'PYEOF' && ok "batch shape" || bad "batch shape"
import sys, yaml
payload = yaml.safe_load(open(sys.argv[1], "rb").read())
call_uuid, company = sys.argv[2], sys.argv[3]
batch = (payload or {}).get("batch", [])
problems = []
if len(batch) != 4:
    problems.append(f"{len(batch)} entries, expected 4")
always = {"company_id", "task", "task_name", "task_prompt", "body", "total_tasks", "lp",
          "remote_local_path"}
for entry in batch:
    missing = always - set(entry)
    if missing:
        problems.append(f"{entry.get('task_name')}: missing {sorted(missing)}")
    if entry.get("total_tasks") != 4:
        problems.append(f"{entry.get('task_name')}: total_tasks={entry.get('total_tasks')}")
    if entry.get("company_id") != company:
        problems.append(f"company_id={entry.get('company_id')}")
    if entry.get("lp") != f"/home/ubuntu/data/uploads/{call_uuid}/{call_uuid}.vtt":
        problems.append(f"lp={entry.get('lp')}")
    if entry.get("lp") != entry.get("remote_local_path"):
        problems.append("lp and remote_local_path disagree")
    if "-->" not in (entry.get("body") or ""):
        problems.append("body lost its cue timings")
    if entry.get("sequrity_key") != "E2E-CUSTOMER-TOKEN":
        problems.append("sequrity_key did not pass through")
names = sorted(e["task_name"] for e in batch)
if names != ["1", "2", "3", "script"]:
    problems.append(f"task names {names}")
for p in problems:
    print(f"     {p}", file=sys.stderr)
sys.exit(1 if problems else 0)
PYEOF
rm -f /tmp/e2e_head.$$ /tmp/e2e_batch.$$

# The peek above leased the order; give it back so the worker below gets a clean batch.
$PY - "$UUID" <<'PYEOF'
import sys
from llmhub import db, dispatch
db.init_pool()
found = dispatch.get_order(sys.argv[1])
order = found["order"] if found else {}
if order.get("lease_token"):
    dispatch.release_lease(str(order["lease_token"]))
db.close_pool()
PYEOF

# --------------------------------------------------------------------------------------
step "4. a worker drains it"
# --------------------------------------------------------------------------------------
$PY scripts/fake_worker.py --worker-id e2e-worker --max-batches 3 2>&1 | sed 's/^/  /'

$PY - "$UUID" <<'PYEOF' && ok "the order is done, every task answered" || bad "not done"
import sys
from llmhub import db, dispatch
db.init_pool()
found = dispatch.get_order(sys.argv[1])
db.close_pool()
if not found:
    sys.exit(1)
order, states = found["order"], {t["state"] for t in found["tasks"]}
print(f"     state={order['state']} tasks={sorted(states)}")
sys.exit(0 if order["state"] == "done" and states <= {"done", "problem", "settled_empty"} else 1)
PYEOF

# --------------------------------------------------------------------------------------
step "5. the bodies are in mediahub"
# --------------------------------------------------------------------------------------
$PY - "$UUID" <<'PYEOF'
import asyncio, sys
from llmhub import mediahub_client as mh

async def main():
    if not mh.configured():
        print("     mediahub not configured - the spool fallback is what ran")
        return 0
    call = await mh.get_call(sys.argv[1])
    if call is None:
        print("     no call row in mediahub", file=sys.stderr)
        await mh.close()
        return 1
    transcript = await mh.get_transcript(sys.argv[1])
    rows = (await mh._client(20.0).get(f"/v1/calls/{sys.argv[1]}/annotations")).json()
    await mh.close()
    empty = [r for r in rows if r["payload"].get("empty")]
    print(f"     call source={call['source']} transcript={len(transcript or '')}B "
          f"annotations={len(rows)} (of which empty answers: {len(empty)})")
    return 0 if call["source"] == "llmhub" and transcript and len(rows) == 4 else 1

sys.exit(asyncio.run(main()))
PYEOF
[ $? -eq 0 ] && ok "transcript and four answers stored" || bad "bodies did not land"

# --------------------------------------------------------------------------------------
step "6. an answered call is not offered again"
# --------------------------------------------------------------------------------------
BODY=$(curl -s -u "$BASIC" -X POST "$URL" --data-urlencode "task=llm" \
       --data-urlencode "worker_id=e2e-again")
[ -z "$BODY" ] && ok "empty 200 - nothing left to do" || bad "still offering work: $BODY"

if [ "$QUICK" = "1" ]; then
    step "7. lease expiry - skipped (--quick)"
    [ "$FAILURES" = "0" ] && { echo; echo "verify_e2e: all checks passed (quick)"; exit 0; }
    echo; echo "verify_e2e: $FAILURES check(s) FAILED"; exit 1
fi

# --------------------------------------------------------------------------------------
step "7. a worker dies mid-batch"
# --------------------------------------------------------------------------------------
UUID2="${UUID}-b"
curl -s -o /dev/null -u "$BASIC" -X POST "$URL" \
    --data-urlencode "task=llm" --data-urlencode "op=ordering" \
    --data-urlencode "company=$COMPANY" --data-urlencode "task_set=1" \
    --data-urlencode "uuid=$UUID2" \
    --data-urlencode "vtt=$VTT" --data-urlencode "language=uk"

$PY scripts/fake_worker.py --worker-id e2e-doomed --die-after 2 2>&1 | sed 's/^/  /'

STALE=$($PY - "$UUID2" <<'PYEOF'
import sys
from llmhub import db, dispatch
db.init_pool()
found = dispatch.get_order(sys.argv[1])
order = found["order"] if found else {}
print(str(order["lease_token"]) if order.get("lease_token") else "")
db.close_pool()
PYEOF
)
[ -n "$STALE" ] && ok "the dead worker still holds a lease" || bad "no lease to expire"

# Age the lease out, then reap - the same two steps the background reaper does on a timer.
$PY - "$UUID2" <<'PYEOF'
import sys
from llmhub import db, dispatch
db.init_pool()
conn = db.connect()
conn.execute("UPDATE orders SET lease_expires_at = now() - interval '1 second' "
             " WHERE call_uuid = %s", (sys.argv[1],))
conn.commit(); db.release(conn)
print(f"     reaper returned {dispatch.reap_expired()} lease(s)")
db.close_pool()
PYEOF

FRESH=$($PY - "$UUID2" <<'PYEOF'
import sys
from llmhub import db, dispatch
db.init_pool()
batch = dispatch.claim(worker_id="e2e-rescuer", want=5, lease_ttl_s=1800,
                       min_speech_chars=48, grace_s=0)
mine = [b for b in batch if b["order"]["call_uuid"] == sys.argv[1]]
print(str(mine[0]["order"]["lease_token"]) if mine else "")
if mine:
    print(f"     re-handed with {len(mine[0]['tasks'])} unanswered task(s)",
          file=sys.stderr)
db.close_pool()
PYEOF
)
[ -n "$FRESH" ] && [ "$FRESH" != "$STALE" ] \
    && ok "re-dispatch minted a new lease token" \
    || bad "the token did not change (stale='$STALE' fresh='$FRESH')"

$PY - "$UUID2" "$STALE" <<'PYEOF' && ok "the dead holder's answer is refused (409)" \
    || bad "a stale holder could still report"
import sys
from llmhub import db, dispatch
db.init_pool()
try:
    dispatch.report_result(call_uuid=sys.argv[1], task_name="3", status="done",
                           result_bytes=5, lease_token=sys.argv[2])
    ok = False
except dispatch.StaleReport:
    ok = True
finally:
    db.close_pool()
sys.exit(0 if ok else 1)
PYEOF

$PY - "$COMPANY" "$UUID2" <<'PYEOF' 2>/dev/null
import sys
from llmhub import db
db.init_pool(); conn = db.connect()
conn.execute("DELETE FROM orders WHERE call_uuid = %s", (sys.argv[2],))
conn.commit(); db.release(conn); db.close_pool()
PYEOF
$PY - "$UUID2" <<'PYEOF' 2>/dev/null
import asyncio, sys
from llmhub import mediahub_client as mh
async def main():
    if mh.configured():
        await mh._client(20.0).delete(f"/v1/calls/{sys.argv[1]}",
                                      params={"expect_source": "llmhub"})
        await mh.close()
asyncio.run(main())
PYEOF

echo
if [ "$FAILURES" = "0" ]; then
    echo "verify_e2e: all checks passed"
    exit 0
fi
echo "verify_e2e: $FAILURES check(s) FAILED"
exit 1
