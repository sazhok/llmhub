"""Background sweeps and graceful shutdown.

Three timers, each replacing something the PHP got for free by re-scanning the filesystem on
every single request, and one sentinel file, which is the tree's own convention for asking a
long-running service to stop (fw/ochat's `.exit` files, asrhub's `.exit_gateway`).
"""
import asyncio
import os

from logly import logger

from . import bodies, config, db, dispatch, mediahub_client

EXIT_SENTINEL = ".exit_llmhub"

_draining = False


def is_draining() -> bool:
    return _draining


async def watch_exit_sentinel(interval_s: float = 2.0) -> None:
    global _draining
    while True:
        if os.path.exists(EXIT_SENTINEL):
            _draining = True
            logger.info(f"{EXIT_SENTINEL} present - draining: no further hand-outs")
        await asyncio.sleep(interval_s)


async def reaper() -> None:
    """Expired leases return to the queue. This is the lease equivalent of
    RUNNING_INDICATOR_MAX_AGE_S, except that progress renews it."""
    interval = config.settings().reaper_interval_s
    while True:
        try:
            await asyncio.to_thread(dispatch.reap_expired)
        except Exception as e:  # noqa: BLE001 - a sweep must never kill its own loop
            logger.error(f"reaper: {e}")
        await asyncio.sleep(interval)


async def settler() -> None:
    """Calls with too little speech to analyse are settled with empty answers, once their
    transcript has stopped changing. A timer, not a side effect of whichever poll happened to
    scan the directory (which is what mark_tasks_empty() was)."""
    settings = config.settings()
    while True:
        try:
            await asyncio.to_thread(
                dispatch.settle_short,
                settings.min_speech_chars, settings.short_transcript_settle_s,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"settler: {e}")
        await asyncio.sleep(settings.settle_interval_s)


async def body_pusher(interval_s: float = 60.0, batch: int = 50) -> None:
    """Bodies that could not reach mediahub when their order arrived.

    The queue never waits for the store, so this is the other half of that promise: an order
    accepted while mediahub was down still ends up with its transcript in mediahub.
    """
    settings = config.settings()
    while True:
        try:
            if mediahub_client.configured():
                await _push_pending(settings, batch)
        except Exception as e:  # noqa: BLE001
            logger.error(f"body pusher: {e}")
        await asyncio.sleep(interval_s)


async def _push_pending(settings, batch: int) -> None:
    conn = await asyncio.to_thread(db.connect)
    try:
        rows = conn.execute(
            "SELECT order_id, call_uuid, company_id, language, context FROM orders "
            " WHERE body_state = 'local' AND state IN ('parked','ready','leased') "
            " ORDER BY ordered_at LIMIT %s",
            (batch,),
        ).fetchall()
    finally:
        await asyncio.to_thread(db.release, conn)

    for row in rows:
        text = bodies.read_spool(settings.spool_dir, row["call_uuid"])
        if text is None:
            continue
        try:
            state, ref = await bodies.store_transcript(
                call_uuid=row["call_uuid"], company_id=row["company_id"], vtt=text,
                language=row["language"], context=row["context"] or "",
                spool_dir=settings.spool_dir, source=settings.mediahub_source,
                timeout_s=settings.mediahub_timeout_s,
            )
        except bodies.CompanyMismatch as e:
            logger.error(f"body pusher: {e}")
            continue
        if state != "mediahub":
            continue
        conn = await asyncio.to_thread(db.connect)
        try:
            conn.execute(
                "UPDATE orders SET body_state = %s, body_ref = %s WHERE order_id = %s",
                (state, ref, row["order_id"]),
            )
            conn.commit()
        finally:
            await asyncio.to_thread(db.release, conn)
