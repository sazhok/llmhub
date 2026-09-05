#! /bin/bash
# Health plus a queue snapshot. Needs LLMHUB_ADMIN_KEY from .env for the queue half.
wd=.
[ ! -f $wd/serve.sh ] && wd=..
cd $wd
set -a; [ -f .env ] && source .env; set +a
BASE=${LLMHUB_URL:-http://127.0.0.1:8008}
echo "--- health:"
curl -sS "$BASE/health" && echo
echo "--- queue:"
curl -sS -H "x-api-key-token: ${LLMHUB_ADMIN_KEY}" "$BASE/v1/admin/queue" && echo
echo "--- sources:"
curl -sS -H "x-api-key-token: ${LLMHUB_ADMIN_KEY}" "$BASE/v1/admin/sources" && echo
