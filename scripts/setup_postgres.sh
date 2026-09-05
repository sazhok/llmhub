#! /bin/bash
# One-time (but idempotent, safe to re-run) setup of llmhub's role and database.
#
# llmhub shares the isolated :5433 cluster with mediahub and asrhub but gets its OWN database
# and role, so none of the three can read or corrupt another's tables. mediahub is the store;
# this database is the queue.
#
# Run manually, not from app code - creating a role/database needs a superuser privilege the
# app's own role should never have.
#
# Usage: bash scripts/setup_postgres.sh

set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "ERROR: .env not found - copy .env.example and fill in LLMHUB_PG_* first" >&2
    exit 1
fi
set -a
source .env
set +a

: "${LLMHUB_PG_USER:?LLMHUB_PG_USER not set in .env}"
: "${LLMHUB_PG_PASSWORD:?LLMHUB_PG_PASSWORD not set in .env}"
: "${LLMHUB_PG_DB:?LLMHUB_PG_DB not set in .env}"
: "${LLMHUB_PG_PORT:?LLMHUB_PG_PORT not set in .env}"

PSQL="sudo -u postgres psql -p ${LLMHUB_PG_PORT}"

role_exists=$($PSQL -tAc "SELECT 1 FROM pg_roles WHERE rolname = '${LLMHUB_PG_USER}'")
if [ "$role_exists" == "1" ]; then
    echo "Role '${LLMHUB_PG_USER}' already exists, skipping creation."
else
    echo "Creating role '${LLMHUB_PG_USER}'..."
    $PSQL -c "CREATE ROLE ${LLMHUB_PG_USER} WITH LOGIN PASSWORD '${LLMHUB_PG_PASSWORD}'"
fi

db_exists=$($PSQL -tAc "SELECT 1 FROM pg_database WHERE datname = '${LLMHUB_PG_DB}'")
if [ "$db_exists" == "1" ]; then
    echo "Database '${LLMHUB_PG_DB}' already exists, skipping creation."
else
    echo "Creating database '${LLMHUB_PG_DB}' owned by '${LLMHUB_PG_USER}'..."
    $PSQL -c "CREATE DATABASE ${LLMHUB_PG_DB} OWNER ${LLMHUB_PG_USER}"
fi

# gen_random_uuid() mints the lease token. Built into Postgres 13+, but be explicit so a
# future move to an older or stripped cluster fails here rather than at the first hand-out.
echo "Verifying gen_random_uuid() is available in '${LLMHUB_PG_DB}'..."
sudo -u postgres psql -p "${LLMHUB_PG_PORT}" -d "${LLMHUB_PG_DB}" \
    -tAc "SELECT gen_random_uuid()" > /dev/null

echo "Done. Tables are created on first start by db.ensure_schema()."
