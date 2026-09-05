"""Accepting an order, and turning a claim into a batch. Shared by both API surfaces.

The shim and the native API differ in how they are spoken to, not in what they do, so both go
through here. Everything that is a *decision* - what a missing taskset means, when a body is
allowed to be only local, what a batch entry contains - lives in this module rather than in a
route handler.
"""
import asyncio
import json
from typing import Any, Optional

from logly import logger

from . import bodies, config, db, dispatch, sources, tasksets, vtt
from .legacy import render


class OrderRejected(Exception):
    """The order cannot be turned into work. The legacy shim swallows this into an empty 200;
    the native API returns it as a 4xx."""


async def accept_order(
    *,
    call_uuid: str,
    company_id: str,
    transcript: str,
    taskset: str = "",
    url: str = "",
    language: Optional[str] = None,
    sequrity_key: str = "",
    context: str = "",
    order_status: str = "",
) -> dict:
    """Validate, snapshot the taskset, store the body, queue the tasks."""
    settings = config.settings()

    if not call_uuid or not company_id:
        raise OrderRejected("uuid and company_id are both required")
    if not transcript:
        # order_llm_task() defaults a missing vtt to "WEBVTT\n\n" and then requires it to be
        # non-empty (worker_acceptor_light.php:1241); an order with no transcript is not an
        # order, it is a mistake upstream.
        raise OrderRejected("vtt is required")

    generation = taskset or settings.default_taskset
    params_root = await asyncio.to_thread(
        sources.params_root_for, company_id, settings.params_root
    )
    taskset_obj = tasksets.get(params_root, company_id, generation,
                               ttl_s=settings.taskset_ttl_s)
    if taskset_obj is None:
        # The PHP refuses the same way, by requiring the taskset directory to exist
        # (worker_acceptor_light.php:1268). Accepting the call anyway would queue work whose
        # questions nobody can produce.
        raise OrderRejected(
            f"no taskset '{generation}' for company '{company_id}' under {params_root}"
        )

    try:
        body_state, body_ref = await bodies.store_transcript(
            call_uuid=call_uuid, company_id=company_id, vtt=transcript, language=language,
            context=context, spool_dir=settings.spool_dir, source=settings.mediahub_source,
            timeout_s=settings.mediahub_timeout_s,
        )
    except bodies.CompanyMismatch as e:
        raise OrderRejected(str(e))

    order = await asyncio.to_thread(
        dispatch.create_order,
        call_uuid=call_uuid, company_id=company_id, taskset=generation,
        taskset_sha256=taskset_obj.sha256, tasks=list(taskset_obj.tasks),
        speech_chars=vtt.speech_chars(transcript),
        body_state=body_state, body_ref=body_ref, body_sha256=bodies.sha256(transcript),
        url=url, language=language, sequrity_key=sequrity_key, context=context,
        order_status=order_status,
    )
    logger.info(
        f"ordered {company_id}/{call_uuid} taskset={generation} "
        f"tasks={taskset_obj.task_count} speech={order['speech_chars']}c "
        f"body={body_state} state={order['state']}"
    )
    return order


async def claim_batch(
    *,
    worker_id: str,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> Optional[dict]:
    """Claim one order and render its batch. None when there is no work.

    One order per claim, deliberately: the PHP hands out exactly one call's tasks per poll
    (worker_acceptor_light.php:1543-1580) and ochat's task dependencies assume the whole call
    is with one worker.
    """
    settings = config.settings()
    claimed = await asyncio.to_thread(
        dispatch.claim,
        worker_id=worker_id, want=1, include=include, exclude=exclude,
        lease_ttl_s=settings.lease_ttl_s, min_speech_chars=settings.min_speech_chars,
        grace_s=settings.claim_grace_s,
    )
    if not claimed:
        return None
    order = claimed[0]["order"]
    tasks = claimed[0]["tasks"]

    body = await bodies.load_body(
        call_uuid=order["call_uuid"], spool_dir=settings.spool_dir,
        timeout_s=settings.mediahub_timeout_s,
    )
    if body is None:
        # The body is gone from both the spool and the store. Handing out a batch with an
        # empty transcript would spend a model run on nothing and write a confident verdict
        # about silence, so return the lease and let the sweep or an operator deal with it.
        await asyncio.to_thread(dispatch.mark_unavailable, str(order["lease_token"]),
                                settings.unavailable_retry_s, "transcript unavailable")
        logger.error(f"no body for {order['call_uuid']} - lease returned")
        return None

    entries = []
    for task in tasks:
        config_json = json.loads(task["config_json"] or "{}")
        entries.append(render.batch_entry(
            order=order, task_name=task["task_name"], config=config_json, body=body,
            total_tasks=_total_tasks(order), lp=render.legacy_lp(
                settings.legacy_lp_root, order["call_uuid"]),
        ))
    return {"order": order, "tasks": tasks, "batch": entries}


def _total_tasks(order: dict) -> int:
    """`total_tasks` is the size of the taskset, not of the batch: a call whose second half is
    already answered still reports the full count, exactly as `count($ext2task_info)` does."""
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT count(*) AS n FROM tasks WHERE order_id = %s", (order["order_id"],)
        ).fetchone()
        return int(row["n"])
    finally:
        db.release(conn)


async def record_result(
    *,
    call_uuid: str,
    task_name: str,
    status: str,
    content: str,
    lease_token: Optional[str] = None,
) -> dict:
    """Store the answer, then mark the task. In that order, on purpose.

    If the store fails, the task stays unanswered and the caller is told 507 - which is what
    makes the failure visible instead of turning into a task that looks finished and has no
    result. That is the same conclusion the PHP reached on 2026-09-04, arrived at from the
    other direction: it had been removing the running marker and answering 200 regardless of
    whether `file_put_contents` wrote anything.
    """
    settings = config.settings()
    snapshot = await asyncio.to_thread(dispatch.get_order, call_uuid)
    if snapshot is None:
        raise dispatch.StaleReport(f"no order for {call_uuid}")
    order = snapshot["order"]
    checklist_item_uuid = ""
    for task in snapshot["tasks"]:
        if task["task_name"] == task_name:
            checklist_item_uuid = str(
                json.loads(task["config_json"] or "{}").get("uuid", "") or ""
            )
            break

    stored, ref = await bodies.store_result(
        call_uuid=call_uuid, taskset=order["taskset"], task_name=task_name,
        content=content, status=status, checklist_item_uuid=checklist_item_uuid,
        taskset_sha256=order["taskset_sha256"], spool_dir=settings.spool_dir,
        timeout_s=settings.mediahub_timeout_s,
    )
    if not stored:
        raise StorageFailed(f"could not store {call_uuid}/{task_name}")

    outcome = await asyncio.to_thread(
        dispatch.report_result,
        call_uuid=call_uuid, task_name=task_name, status=status,
        result_bytes=len(content.encode("utf-8")), result_ref=ref,
        lease_token=lease_token, lease_ttl_s=settings.lease_ttl_s,
    )
    if outcome["order_state"] == "done":
        # The body is in the store and the queue is finished with it.
        bodies.drop_spool(settings.spool_dir, call_uuid)
    return outcome


class StorageFailed(Exception):
    """The answer did not reach the store. The task must stay unanswered."""


def batch_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """What peek returns. Always the `{"batch": [...]}` envelope: the PHP emits a bare object
    only when the batch is empty, and that branch is unreachable there (it returns false
    instead), while ochat unwraps with `job_info.get("batch", [job_info])`."""
    return {"batch": entries}
