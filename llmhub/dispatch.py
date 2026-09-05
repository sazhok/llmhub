"""Ordering, claiming, reporting - the queue itself.

Plain synchronous functions over a psycopg pool, called from the event loop through
asyncio.to_thread, exactly as asrhub/asrhub/dispatch.py does.

The one structural decision worth stating up front: **the lease is on the order, the result is
on the task.** The PHP hands out a whole call (peek_llm_job emits every new task of one call
and touches every marker, worker_acceptor_light.php:1543-1580) but completes one task at a
time. That is not an accident of implementation - ochat's TaskQuery makes tasks of the same
call depend on each other (`"source": "script"` in a task's config means it waits for the
`script` task's answer, ochat/llm_gateway.cur.py:196-205), so splitting one call's tasks
across workers would deadlock them against each other.
"""
import json
from typing import Any, Optional

from logly import logger

from . import db


def _event(conn, order_id: int, kind: str, **detail) -> None:
    conn.execute(
        "INSERT INTO order_events (order_id, kind, detail_json) VALUES (%s, %s, %s)",
        (order_id, kind, json.dumps(detail, ensure_ascii=False)),
    )


# --------------------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------------------

def create_order(
    *,
    call_uuid: str,
    company_id: str,
    taskset: str,
    taskset_sha256: str,
    tasks: list[tuple[str, dict[str, Any]]],
    speech_chars: int,
    body_state: str = "local",
    body_ref: str = "",
    body_sha256: str = "",
    url: str = "",
    language: Optional[str] = None,
    sequrity_key: str = "",
    context: str = "",
    order_status: str = "",
) -> dict:
    """Accept one ordering of one call, superseding any live order for the same call.

    Re-ordering starts every task over, which is what order_llm_task() achieves by unlinking
    every `*.task-*` file for the call (worker_acceptor_light.php:1310-1330). The previous
    order is kept as `superseded` rather than deleted, so its history stays readable.

    An order for a company that is not enabled here is **parked, not refused**. Refusing would
    lose the call: the legacy contract has no way to say "not mine" (op=ordering always
    answers an empty 200), and the producer would move on. Enabling the source releases
    everything parked for it.
    """
    conn = db.connect()
    try:
        source = conn.execute(
            "SELECT enabled FROM sources WHERE company_id = %s", (company_id,)
        ).fetchone()
        if source is None:
            conn.execute(
                "INSERT INTO sources (company_id, enabled, note) VALUES (%s, FALSE, %s) "
                "ON CONFLICT (company_id) DO NOTHING",
                (company_id, "auto-created by a first order"),
            )
            enabled = False
        else:
            enabled = bool(source["enabled"])

        superseded = conn.execute(
            "UPDATE orders SET state = 'superseded', updated_at = now(), "
            "       lease_token = NULL, leased_by = NULL, lease_expires_at = NULL "
            " WHERE call_uuid = %s AND state IN ('parked', 'ready', 'leased') "
            "RETURNING order_id, url, sequrity_key, context, order_status, language",
            (call_uuid,),
        ).fetchall()

        # order_llm_task() merges its new state OVER the call's existing status.yaml
        # (worker_acceptor_light.php:1280-1296), so a re-order that omits `sequrity_key` keeps
        # the one from the first order. Dropping it instead would leave the worker with no
        # credential for the customer's backend and a silently unpostable answer, so the
        # inheritance is reproduced rather than tidied away.
        if superseded:
            previous = superseded[-1]
            url = url or previous["url"]
            sequrity_key = sequrity_key or previous["sequrity_key"]
            context = context or previous["context"]
            order_status = order_status or previous["order_status"]
            language = language if language is not None else previous["language"]

        row = conn.execute(
            """
            INSERT INTO orders (call_uuid, company_id, taskset, taskset_sha256, url, language,
                                sequrity_key, context, order_status, body_state, body_ref,
                                body_sha256, speech_chars, state)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (call_uuid, company_id, taskset, taskset_sha256, url, language, sequrity_key,
             context, order_status, body_state, body_ref, body_sha256, speech_chars,
             "ready" if enabled else "parked"),
        ).fetchone()

        order_id = row["order_id"]
        for ordinal, (task_name, config) in enumerate(tasks):
            conn.execute(
                "INSERT INTO tasks (order_id, task_name, ordinal, config_json) "
                "VALUES (%s, %s, %s, %s)",
                (order_id, task_name, ordinal,
                 json.dumps(config, ensure_ascii=False, sort_keys=True)),
            )
        _event(conn, order_id, "ordered", company_id=company_id, taskset=taskset,
               tasks=len(tasks), speech_chars=speech_chars,
               superseded=[r["order_id"] for r in superseded])
        conn.commit()
        return dict(row)
    finally:
        db.release(conn)


def release_parked(company_id: str) -> int:
    """Enabling a source frees everything held for it. Nothing is lost in the gap between a
    company being pointed here and being switched on."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "UPDATE orders SET state = 'ready', updated_at = now() "
            " WHERE company_id = %s AND state = 'parked' RETURNING order_id",
            (company_id,),
        ).fetchall()
        conn.commit()
        if rows:
            logger.info(f"{company_id}: released {len(rows)} parked order(s)")
        return len(rows)
    finally:
        db.release(conn)


# --------------------------------------------------------------------------------------
# Claiming
# --------------------------------------------------------------------------------------

_CLAIM_SQL = """
WITH picked AS (
    SELECT o.order_id
      FROM orders o
      JOIN sources s ON s.company_id = o.company_id
     WHERE o.state = 'ready'
       AND s.enabled
       AND (cardinality(%(include)s::text[]) = 0 OR o.company_id = ANY(%(include)s::text[]))
       AND NOT (o.company_id = ANY(%(exclude)s::text[]))
       AND o.speech_chars >= %(min_speech_chars)s
       AND o.attempts < s.max_attempts
       AND (o.retry_after IS NULL OR o.retry_after <= now())
       AND (NOT (%(worker_id)s = ANY(o.prior_holders))
            OR o.updated_at < now() - make_interval(secs => %(grace)s))
     ORDER BY (%(worker_id)s = ANY(o.prior_holders)),
              o.priority DESC, s.priority DESC, o.ordered_at
     LIMIT %(want)s
       FOR UPDATE SKIP LOCKED
)
UPDATE orders o
   SET state            = 'leased',
       leased_by        = %(worker_id)s,
       lease_token      = gen_random_uuid(),
       lease_expires_at = now() + make_interval(secs => %(ttl)s),
       lease_generation = o.lease_generation + 1,
       attempts         = o.attempts + 1,
       updated_at       = now()
  FROM picked
 WHERE o.order_id = picked.order_id
RETURNING o.*
"""


def claim(
    *,
    worker_id: str,
    want: int = 1,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
    lease_ttl_s: float,
    min_speech_chars: int,
    grace_s: float,
) -> list[dict]:
    """Hand out whole orders, oldest first.

    Four properties, each replacing a specific behaviour of peek_llm_job():

      - **O(log n).** `idx_orders_claim` is partial on `state='ready'` and ordered the way this
        query orders, so Postgres walks the index and stops at LIMIT. The PHP scandirs ~5000
        directories and stats its way through each one, on every poll, from nine workers.
      - **Fair.** `ORDER BY ordered_at`. The PHP's order is scandir's, i.e. alphabetical by
        uuid: a call that keeps failing holds the same early position forever and starves
        everything sorting after it.
      - **Safe under concurrency.** One statement, `FOR UPDATE SKIP LOCKED`. The PHP touches
        `.processing_<task>` only *after* its eligibility checks (:1580), so two pollers
        genuinely can hand out the same call.
      - **Filtered here, not there.** ochat drops a call whose company is excluded only after
        the batch has been handed out and marked (ochat/llm_gateway.cur.py:1345-1352), wasting
        the hand-out; the PHP's own company filter is dead code (`if(false && ...)`, :1430).
    """
    conn = db.connect()
    try:
        orders = conn.execute(_CLAIM_SQL, {
            "worker_id": worker_id, "want": max(1, want),
            "include": include or [], "exclude": exclude or [],
            "min_speech_chars": min_speech_chars, "grace": grace_s, "ttl": lease_ttl_s,
        }).fetchall()
        out = []
        for order in orders:
            tasks = conn.execute(
                "UPDATE tasks SET state = 'leased' "
                " WHERE order_id = %s AND state = 'pending' "
                "RETURNING *",
                (order["order_id"],),
            ).fetchall()
            if not tasks:
                # Nothing left to ask about: every task was answered while the order still
                # said 'ready'. Close it here rather than handing a worker an empty batch.
                conn.execute(
                    "UPDATE orders SET state = 'done', lease_token = NULL, leased_by = NULL, "
                    "       lease_expires_at = NULL, updated_at = now() WHERE order_id = %s",
                    (order["order_id"],),
                )
                _event(conn, order["order_id"], "closed-empty")
                continue
            _event(conn, order["order_id"], "leased", worker_id=worker_id,
                   generation=order["lease_generation"], tasks=len(tasks))
            out.append({"order": dict(order), "tasks": [dict(t) for t in tasks]})
        conn.commit()
        return out
    finally:
        db.release(conn)


def renew(lease_token: str, lease_ttl_s: float) -> Optional[dict]:
    conn = db.connect()
    try:
        row = conn.execute(
            "UPDATE orders SET lease_expires_at = now() + make_interval(secs => %s), "
            "       updated_at = now() "
            " WHERE lease_token = %s AND state = 'leased' RETURNING *",
            (lease_ttl_s, lease_token),
        ).fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        db.release(conn)


def release_lease(lease_token: str) -> Optional[dict]:
    """A polite return: the order goes back to 'ready', the attempt is refunded, and the
    worker is NOT recorded as a prior holder - it is not being blamed, it just stopped."""
    conn = db.connect()
    try:
        row = conn.execute(
            "UPDATE orders SET state = 'ready', leased_by = NULL, lease_token = NULL, "
            "       lease_expires_at = NULL, attempts = GREATEST(attempts - 1, 0), "
            "       updated_at = now() "
            " WHERE lease_token = %s AND state = 'leased' RETURNING *",
            (lease_token,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE tasks SET state = 'pending' WHERE order_id = %s AND state = 'leased'",
                (row["order_id"],),
            )
            _event(conn, row["order_id"], "released")
        conn.commit()
        return dict(row) if row else None
    finally:
        db.release(conn)


def mark_unavailable(lease_token: str, retry_after_s: float, reason: str = "") -> Optional[dict]:
    """The engine is down, not the job. Refund the attempt and park the order briefly.

    asrhub learned this distinction on 2026-08-27, when 28 calls were buried in `failed` in
    about fifteen seconds while the STT host was down. ochat's local vLLM restarting is the
    identical failure mode, and the current pipeline has no way to express it at all.
    """
    conn = db.connect()
    try:
        row = conn.execute(
            "UPDATE orders SET state = 'ready', leased_by = NULL, lease_token = NULL, "
            "       lease_expires_at = NULL, attempts = GREATEST(attempts - 1, 0), "
            "       retry_after = now() + make_interval(secs => %s), last_error = %s, "
            "       updated_at = now() "
            " WHERE lease_token = %s AND state = 'leased' RETURNING *",
            (retry_after_s, reason[:500] or "engine unavailable", lease_token),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE tasks SET state = 'pending' WHERE order_id = %s AND state = 'leased'",
                (row["order_id"],),
            )
            _event(conn, row["order_id"], "unavailable", reason=reason[:500])
        conn.commit()
        return dict(row) if row else None
    finally:
        db.release(conn)


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

class StaleReport(Exception):
    """The task this report answers has already been answered by somebody else."""


def _close_if_finished(conn, order_id: int) -> Optional[str]:
    remaining = conn.execute(
        "SELECT count(*) AS n FROM tasks WHERE order_id = %s AND state IN ('pending','leased')",
        (order_id,),
    ).fetchone()["n"]
    if remaining:
        return None
    conn.execute(
        "UPDATE orders SET state = 'done', lease_token = NULL, leased_by = NULL, "
        "       lease_expires_at = NULL, updated_at = now() WHERE order_id = %s",
        (order_id,),
    )
    _event(conn, order_id, "completed")
    return "done"


def report_result(
    *,
    call_uuid: str,
    task_name: str,
    status: str,
    result_bytes: int,
    result_ref: str = "",
    lease_token: Optional[str] = None,
    lease_ttl_s: float = 1800.0,
) -> dict:
    """Record one task's answer.

    Resolution is by (call_uuid, task_name), because that is all the legacy wire carries: the
    worker returns the `lp` string it was given and its `task_name`, and `lp`'s stem is the
    call uuid (ochat/llm_gateway.cur.py:181, :1359). The live order for that call is the
    answer, which reproduces taskset_by_call_dir() (worker_acceptor_light.php:851) - "whatever
    taskset this call is currently ordered for" - with no status file to read.

    A result arriving after its lease expired is **accepted** as long as the task is still
    unanswered. A slow-but-correct answer is worth more than a lost call; only a task already
    answered under a different lease generation is rejected as stale.
    """
    conn = db.connect()
    try:
        row = conn.execute(
            """
            SELECT o.order_id, o.state AS order_state, o.lease_token, o.lease_generation,
                   o.taskset, t.state AS task_state
              FROM orders o
              JOIN tasks t ON t.order_id = o.order_id
             WHERE o.call_uuid = %s AND t.task_name = %s
               AND o.state IN ('leased', 'ready', 'parked')
             ORDER BY (o.state = 'leased') DESC, o.order_id DESC
             LIMIT 1
            """,
            (call_uuid, task_name),
        ).fetchone()
        if row is None:
            raise StaleReport(f"no live order for {call_uuid}/{task_name}")

        if lease_token is not None and str(row["lease_token"]) != str(lease_token):
            raise StaleReport(f"lease token no longer holds {call_uuid}")

        if row["task_state"] in ("done", "settled_empty", "problem"):
            raise StaleReport(f"{call_uuid}/{task_name} was already answered")

        task_state = {"done": "done", "problem": "problem"}.get(status, "done")
        conn.execute(
            "UPDATE tasks SET state = %s, result_status = %s, result_bytes = %s, "
            "       result_ref = %s, result_at = now(), attempts = attempts + 1 "
            " WHERE order_id = %s AND task_name = %s",
            (task_state, status, result_bytes, result_ref, row["order_id"], task_name),
        )
        # Progress renews the lease. The PHP's marker ages out from the moment of hand-out
        # regardless of how much of the call has been answered since.
        conn.execute(
            "UPDATE orders SET lease_expires_at = now() + make_interval(secs => %s), "
            "       updated_at = now() WHERE order_id = %s AND state = 'leased'",
            (lease_ttl_s, row["order_id"]),
        )
        closed = _close_if_finished(conn, row["order_id"])
        conn.commit()
        return {"order_id": row["order_id"], "taskset": row["taskset"],
                "task_state": task_state, "order_state": closed or row["order_state"]}
    finally:
        db.release(conn)


def fail_task(*, call_uuid: str, task_name: str, error: str) -> Optional[dict]:
    """A worker reporting `failed`. The PHP records nothing for this status and leaves the
    running marker in place so the task ages back into the queue; here the attempt is counted
    and the task returns to `pending` explicitly."""
    conn = db.connect()
    try:
        row = conn.execute(
            """
            SELECT o.order_id FROM orders o JOIN tasks t ON t.order_id = o.order_id
             WHERE o.call_uuid = %s AND t.task_name = %s AND o.state IN ('leased', 'ready')
             ORDER BY (o.state = 'leased') DESC, o.order_id DESC LIMIT 1
            """,
            (call_uuid, task_name),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE tasks SET state = 'pending', attempts = attempts + 1, last_error = %s "
            " WHERE order_id = %s AND task_name = %s AND state != 'done'",
            (error[:500], row["order_id"], task_name),
        )
        _event(conn, row["order_id"], "task-failed", task=task_name, error=error[:500])
        conn.commit()
        return dict(row)
    finally:
        db.release(conn)


# --------------------------------------------------------------------------------------
# Background sweeps
# --------------------------------------------------------------------------------------

def reap_expired(max_attempts_note: str = "") -> int:
    """Expired leases go back to the queue, and the holder is remembered as a prior one.

    Already-answered tasks stay answered, so a partially reported batch resumes with only its
    remainder - the PHP's behaviour, made explicit rather than emergent from which files
    happen to exist.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "UPDATE orders SET state = 'ready', "
            "       prior_holders = CASE WHEN leased_by IS NULL THEN prior_holders "
            "                            ELSE array_append(prior_holders, leased_by) END, "
            "       leased_by = NULL, lease_token = NULL, lease_expires_at = NULL, "
            "       updated_at = now() "
            " WHERE state = 'leased' AND lease_expires_at < now() "
            "RETURNING order_id, call_uuid, leased_by",
            (),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tasks SET state = 'pending' WHERE order_id = %s AND state = 'leased'",
                (row["order_id"],),
            )
            _event(conn, row["order_id"], "lease-expired")
        # An order that has burned its attempts is terminal, not an infinite retry.
        failed = conn.execute(
            "UPDATE orders o SET state = 'failed', updated_at = now() "
            "  FROM sources s "
            " WHERE s.company_id = o.company_id AND o.state = 'ready' "
            "   AND o.attempts >= s.max_attempts "
            "RETURNING o.order_id",
        ).fetchall()
        for row in failed:
            _event(conn, row["order_id"], "gave-up", note=max_attempts_note)
        conn.commit()
        if rows or failed:
            logger.info(f"reaper: {len(rows)} lease(s) expired, {len(failed)} order(s) gave up")
        return len(rows)
    finally:
        db.release(conn)


def settle_short(min_speech_chars: int, settle_after_s: float) -> int:
    """Settle calls with too little speech to ask anything about.

    A port of mark_tasks_empty() (worker_acceptor_light.php:900-918), with its two conditions
    intact: wait until the transcript has stopped changing, and **do not touch a task that is
    currently leased** - somebody may already be answering it. The difference is that this is
    a timer rather than a side effect of whichever poll happened to scan the directory.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT order_id FROM orders "
            " WHERE state = 'ready' AND speech_chars < %s "
            "   AND transcript_at <= now() - make_interval(secs => %s)",
            (min_speech_chars, settle_after_s),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tasks SET state = 'settled_empty', result_status = 'done', "
                "       result_bytes = 0, result_at = now() "
                " WHERE order_id = %s AND state = 'pending'",
                (row["order_id"],),
            )
            conn.execute(
                "UPDATE orders SET state = 'settled_empty', updated_at = now() "
                " WHERE order_id = %s",
                (row["order_id"],),
            )
            _event(conn, row["order_id"], "settled-empty")
        conn.commit()
        if rows:
            logger.info(f"settled {len(rows)} order(s) as too short to analyse")
        return len(rows)
    finally:
        db.release(conn)


def requeue_orphaned_leases() -> int:
    """At boot nothing can legitimately be leased: one process owns this queue."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "UPDATE orders SET state = 'ready', leased_by = NULL, lease_token = NULL, "
            "       lease_expires_at = NULL, updated_at = now() "
            " WHERE state = 'leased' RETURNING order_id",
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tasks SET state = 'pending' WHERE order_id = %s AND state = 'leased'",
                (row["order_id"],),
            )
            _event(conn, row["order_id"], "requeued-at-boot")
        conn.commit()
        if rows:
            logger.info(f"requeued {len(rows)} order(s) left leased by a previous process")
        return len(rows)
    finally:
        db.release(conn)


# --------------------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------------------

def get_order(call_uuid: str) -> Optional[dict]:
    conn = db.connect()
    try:
        order = conn.execute(
            "SELECT * FROM orders WHERE call_uuid = %s ORDER BY order_id DESC LIMIT 1",
            (call_uuid,),
        ).fetchone()
        if order is None:
            return None
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE order_id = %s ORDER BY ordinal, task_name",
            (order["order_id"],),
        ).fetchall()
        return {"order": dict(order), "tasks": [dict(t) for t in tasks]}
    finally:
        db.release(conn)


def queue_stats() -> dict:
    conn = db.connect()
    try:
        by_state = conn.execute(
            "SELECT state, count(*) AS n FROM orders GROUP BY state ORDER BY state"
        ).fetchall()
        by_company = conn.execute(
            "SELECT company_id, state, count(*) AS n FROM orders "
            " WHERE state IN ('parked','ready','leased') GROUP BY company_id, state "
            " ORDER BY company_id, state"
        ).fetchall()
        return {
            "orders_by_state": {r["state"]: r["n"] for r in by_state},
            "live_by_company": [dict(r) for r in by_company],
        }
    finally:
        db.release(conn)
