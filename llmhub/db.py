"""The queue: orders, their tasks, the sources they are allowed to come from.

Deliberately NOT in mediahub, for the same reason asrhub has its own database: mediahub is a
store (call metadata, audio, transcripts, annotations) and has no job state at all - no
attempts, no lease, no `FOR UPDATE SKIP LOCKED` anywhere - and giving it some would change
what it is. llmhub owns the queue; mediahub owns the bodies.

What this replaces is a directory tree. In worker_acceptor_light.php a task is "done" iff the
file `<uuid>.<taskset>.task-<name>` exists and "running" iff a `.processing_<name>` marker is
younger than 30 minutes, so every poll had to `scandir()` ~5000 call directories and stat its
way through each one (peek_llm_job, worker_acceptor_light.php:1334). Two properties of that
design are worth naming, because the schema below exists to remove them:

  1. The scan order is `scandir()`'s, i.e. alphabetical by uuid, and the first eligible call
     wins. A call that keeps failing sits at the same early position forever and starves
     everything sorting after it.
  2. "Does the file exist" cannot distinguish a truncated write from a legitimately empty
     answer, which is why a full disk turned into an unbounded re-answer loop on 2026-09-04.

Pattern copied from mediahub/db.py and asrhub/asrhub/db.py: raw DDL in ensure_schema(), plain
sync functions, a psycopg ConnectionPool, no ORM and no migration framework. Schema evolution
is CREATE TABLE IF NOT EXISTS + ALTER TABLE ... ADD COLUMN IF NOT EXISTS on every boot.
"""
from datetime import datetime, timezone
from typing import Optional

import psycopg
from logly import logger
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from env_secrets import get_env_secret

# orders.state - the lifecycle of one call's analysis.
ORDER_STATES = (
    "parked",         # accepted, but its company is not enabled here yet - held, not dropped
    "ready",          # claimable
    "leased",         # a worker holds it (lease_token + lease_expires_at set)
    "done",           # every task answered
    "settled_empty",  # too little speech to ask anything: every task answered with 0 bytes
    "failed",         # attempts cap reached
    "superseded",     # a newer order for the same call replaced it
    "abandoned",      # operator dropped it
)

# tasks.state. Note `settled_empty` is a real answer, not a failure: an empty result file has
# always meant "not applicable" in this pipeline, and conflating it with "unfinished" is the
# bug that write_task_result() (worker_acceptor_light.php:1059) had to be patched around.
TASK_STATES = ("pending", "leased", "done", "settled_empty", "problem", "failed")

_POOL: Optional[ConnectionPool] = None


def now() -> datetime:
    return datetime.now(timezone.utc)


def _conninfo() -> str:
    return psycopg.conninfo.make_conninfo(
        host=get_env_secret("LLMHUB_PG_HOST") or "127.0.0.1",
        port=get_env_secret("LLMHUB_PG_PORT") or "5433",
        dbname=get_env_secret("LLMHUB_PG_DB") or "llmhub",
        user=get_env_secret("LLMHUB_PG_USER"),
        password=get_env_secret("LLMHUB_PG_PASSWORD"),
    )


def init_pool() -> None:
    global _POOL
    if _POOL is not None:
        return
    _POOL = ConnectionPool(
        _conninfo(), min_size=1, max_size=16, kwargs={"row_factory": dict_row}, open=False,
    )
    _POOL.open(wait=True, timeout=10)  # fail fast on bad credentials/unreachable server


def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        _POOL.close()
        _POOL = None


# Waiting forever for a connection turns pool exhaustion into a silent, total stall: the
# blocked caller is usually inside asyncio.to_thread, so the default executor fills up and the
# event loop stops answering /health too. A bounded wait makes it an error somebody can see.
_CONNECT_TIMEOUT_S = 30.0


def connect() -> psycopg.Connection:
    if _POOL is None:
        raise RuntimeError("db.init_pool() has not been called")
    return _POOL.getconn(timeout=_CONNECT_TIMEOUT_S)


def release(conn: psycopg.Connection) -> None:
    # Read-only helpers never commit, which would hand an idle-in-transaction connection back
    # to the pool; roll back here (a no-op if the caller already committed).
    conn.rollback()
    if _POOL is not None:
        _POOL.putconn(conn)


def ping() -> bool:
    try:
        conn = connect()
    except Exception as e:  # noqa: BLE001 - /health must answer even when the DB is down
        logger.warning(f"db ping could not get a connection: {e}")
        return False
    try:
        conn.execute("SELECT 1")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"db ping failed: {e}")
        return False
    finally:
        release(conn)


# --------------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------------

_SCHEMA = """
-- One company, keyed by the frozen source_key wire format ('1call-102', '552') that fw,
-- mediahub and asrhub already agree on. This table is the migration valve: a company is
-- served by llmhub only once its row says so, which is what makes the cutover per-company
-- rather than all-at-once.
CREATE TABLE IF NOT EXISTS sources (
    company_id    TEXT PRIMARY KEY,
    enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    -- Taskset root override. Empty means settings.params_root, which on this box is
    -- /home/ubuntu/hrm/local/data/params - the tree prompts2tasks.py writes and
    -- deploy_params_remote.sh pushes to har. bq holds the upstream copy, har the downstream.
    params_dir    TEXT NOT NULL DEFAULT '',
    max_attempts  INTEGER NOT NULL DEFAULT 5,
    priority      INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One ordering of one call. A re-order supersedes the previous one and starts its tasks
-- over, exactly as order_llm_task() unlinks every *.task-* file for the call
-- (worker_acceptor_light.php:1310-1330).
CREATE TABLE IF NOT EXISTS orders (
    order_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    call_uuid         TEXT NOT NULL,
    company_id        TEXT NOT NULL,
    taskset           TEXT NOT NULL DEFAULT '1',
    -- WHICH generation of the taskset judged this call. Nothing has ever recorded this, which
    -- is the entire reason symphony/backend/history.py had to grow an identify() that guesses
    -- it back out of file names.
    taskset_sha256    TEXT NOT NULL DEFAULT '',

    -- Pass-through values echoed into the peek payload. sequrity_key is the customer's
    -- backend credential, not ours: store it, never log it, never return it from /v1/admin.
    url               TEXT NOT NULL DEFAULT '',
    language          TEXT,
    sequrity_key      TEXT NOT NULL DEFAULT '',
    context           TEXT NOT NULL DEFAULT '',
    order_status      TEXT NOT NULL DEFAULT '',

    -- The body. 'local' means it is only in the spool because mediahub was unreachable;
    -- a background sweep promotes it to 'mediahub'. The queue never stops for the store.
    body_state        TEXT NOT NULL DEFAULT 'local',
    body_ref          TEXT NOT NULL DEFAULT '',
    body_sha256       TEXT NOT NULL DEFAULT '',
    -- Length of the SPEECH, not of the vtt: text_by_vtt() strips the header and the cue
    -- timing lines. The 48-character rule is measured on this, while the body shipped to a
    -- worker is the whole vtt with timings.
    speech_chars      INTEGER NOT NULL DEFAULT 0,
    transcript_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    state             TEXT NOT NULL DEFAULT 'ready',
    priority          INTEGER NOT NULL DEFAULT 0,
    ordered_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts          INTEGER NOT NULL DEFAULT 0,
    retry_after       TIMESTAMPTZ,
    last_error        TEXT,

    leased_by         TEXT,
    lease_token       UUID,
    lease_expires_at  TIMESTAMPTZ,
    -- Bumped on every claim, so a stale holder's report is recognisable as stale even when
    -- the row has since been handed to somebody else and back.
    lease_generation  INTEGER NOT NULL DEFAULT 0,
    prior_holders     TEXT[] NOT NULL DEFAULT '{}',

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one live order per call. Ordering the same call again must supersede, not race.
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_live_call ON orders (call_uuid)
    WHERE state IN ('parked', 'ready', 'leased');

-- The claim index. Partial on the claimable state and ordered the way the claim query orders,
-- so Postgres walks the index and stops at LIMIT instead of sorting the world.
CREATE INDEX IF NOT EXISTS idx_orders_claim
    ON orders (priority DESC, ordered_at) WHERE state = 'ready';
CREATE INDEX IF NOT EXISTS idx_orders_company ON orders (company_id, state);
CREATE INDEX IF NOT EXISTS idx_orders_lease_expiry
    ON orders (lease_expires_at) WHERE state = 'leased';
CREATE INDEX IF NOT EXISTS idx_orders_settle
    ON orders (transcript_at) WHERE state = 'ready';
CREATE INDEX IF NOT EXISTS idx_orders_call ON orders (call_uuid, order_id DESC);

CREATE TABLE IF NOT EXISTS tasks (
    order_id      BIGINT NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    task_name     TEXT NOT NULL,
    -- Natural order (1,2,...,10,...,23), not scandir's lexicographic one. The PHP got the
    -- right order only by accident of how it enumerated the taskset directory.
    ordinal       INTEGER NOT NULL DEFAULT 0,
    -- The task's config.json, verbatim. Snapshotted at ordering time so a taskset redeployed
    -- mid-analysis cannot change the questions under a call already being judged.
    config_json   TEXT NOT NULL DEFAULT '{}',
    state         TEXT NOT NULL DEFAULT 'pending',
    result_status TEXT,
    -- 0 is a legitimate answer ("not applicable"), never a failure. The state column carries
    -- that fact; the byte count never does.
    result_bytes  INTEGER,
    result_ref    TEXT NOT NULL DEFAULT '',
    result_at     TIMESTAMPTZ,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    PRIMARY KEY (order_id, task_name)
);
CREATE INDEX IF NOT EXISTS idx_tasks_pending ON tasks (order_id) WHERE state = 'pending';

-- Cached taskset generations, so a peek payload stays reproducible from the database alone
-- during an incident even if the tree on disk has moved on.
CREATE TABLE IF NOT EXISTS tasksets (
    company_id   TEXT NOT NULL,
    generation   TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    tasks_json   TEXT NOT NULL,
    task_count   INTEGER NOT NULL,
    loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, generation, sha256)
);

CREATE TABLE IF NOT EXISTS order_events (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id    BIGINT NOT NULL,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_order_events ON order_events (order_id, at);
"""

# Additive changes go here rather than into _SCHEMA, so an existing database picks them up on
# the next boot. Each must be idempotent.
_MIGRATIONS = [
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS taskset_sha256 TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS ordinal INTEGER NOT NULL DEFAULT 0",
]


def ensure_schema() -> None:
    conn = connect()
    try:
        conn.execute(_SCHEMA)
        for stmt in _MIGRATIONS:
            conn.execute(stmt)
        conn.commit()
        logger.info("schema ensured")
    finally:
        release(conn)
