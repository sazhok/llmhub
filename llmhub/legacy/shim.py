"""The PHP-compatible surface: one URL, three verbs, decided by form parameters.

Mounted at the same path the live hub uses, so switching a client is replacing a host name:

    https://api.harmonica.cloud/hub/v1/worker_acceptor_light.php
    http://100.97.153.111:8008/hub/v1/worker_acceptor_light.php

Everything here is bug-compatible on purpose except one thing, marked below. In particular:

  - **An empty 200 means "no work".** `yaml.safe_load(b"")` is None, and that is how ochat
    learns the queue is empty (ochat/llm_gateway.cur.py:1338-1341). A 204 would be cleaner and
    would break it.
  - **`op=ordering` always answers an empty 200**, even when the order is refused. The PHP's
    handler `exit()`s having echoed nothing whether `order_llm_task()` returned true or false
    (worker_acceptor_light.php:112-152), and fw retries five times with backoff on anything
    else (fw/llmmon.py:174-180). A refusal that turned into a 500 would become a retry storm.
  - **Content-Type stays `text/html`.** The PHP never sets one, so JSON has always gone out
    under PHP's default. The client parses with `yaml.safe_load`, which does not care, but
    shadow-comparing responses is easier when the headers match too.

The one deliberate deviation: `task` other than `llm` returns **404** rather than silently
falling through to the ASR branch. ASR and MT stay on the PHP; a client that arrives here for
them is misconfigured and must be visible rather than quietly served nothing.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, Request, Response
from logly import logger

from .. import auth, dispatch, orders
from . import forms, render

router = APIRouter()

_MEDIA_TYPE = "text/html; charset=utf-8"
_PATH = "/hub/v1/worker_acceptor_light.php"


def _empty() -> Response:
    """The "nothing for you" answer, byte for byte: 200, no body."""
    return Response(content=b"", media_type=_MEDIA_TYPE)


@router.get(_PATH)
@router.post(_PATH)
async def worker_acceptor_light(
    request: Request, basic_user: str = Depends(auth.require_basic)
) -> Response:
    params = await forms.read_params(request)
    task = params.get("task", "asr") or "asr"
    operation = params.get("op", "") or ""

    if operation == "test":
        # Reachable for any task, exactly as the PHP's first branch is (:88-91).
        return Response(content=f"This is a test for task: '{task}'.".encode("utf-8"),
                        media_type=_MEDIA_TYPE)

    if task != "llm":
        return Response(
            content=f"llmhub serves task=llm only; '{task}' stays on the PHP hub".encode(),
            status_code=404, media_type=_MEDIA_TYPE,
        )

    if operation == "ordering":
        return await _order(params, basic_user)
    if operation == "reporting":
        return await _report(params)
    return await _peek(params, basic_user)


async def _order(params: forms.Params, basic_user: str) -> Response:
    call_uuid = params.get("uuid") or params.get("call_id") or ""
    company_id = params.get("company_id") or params.get("company") or ""
    # order_llm_task() defaults the transcript to "WEBVTT\n\n" (:146) and then refuses an
    # order whose text is empty; keeping the default here means the refusal happens for the
    # same reason and in the same place.
    transcript = params.get("vtt") or "WEBVTT\n\n"
    try:
        await orders.accept_order(
            call_uuid=call_uuid,
            company_id=company_id,
            transcript=transcript,
            taskset=params.get("task_set") or "",
            url=params.get("url") or "",
            language=params.get("language"),
            sequrity_key=params.get("sequrity_key") or params.get("sequrity_token") or "",
            context=params.get("context") or "",
            order_status=params.get("status") or "",
        )
    except orders.OrderRejected as e:
        # Logged and counted, never signalled. See the module docstring.
        logger.warning(f"order refused for {company_id or '?'}/{call_uuid or '?'}: {e}")
    except Exception as e:  # noqa: BLE001 - an ordering must not 500 into fw's retry loop
        logger.error(f"order failed for {company_id or '?'}/{call_uuid or '?'}: {e}")
    return _empty()


async def _peek(params: forms.Params, basic_user: str) -> Response:
    # The legacy wire carries no worker identity - nine ochat processes share one credential -
    # so the whole fleet is one holder here. That is enough for what prior_holders is for:
    # an order this fleet has already tried is offered again only after the grace window,
    # instead of being handed straight back to it.
    worker_id = params.get("worker_id") or f"legacy:{basic_user}"
    include = _split(params.get("include_company_ids"))
    exclude = _split(params.get("exclude_company_ids"))
    claimed = await orders.claim_batch(
        worker_id=worker_id, include=include, exclude=exclude
    )
    if claimed is None:
        return _empty()
    body = json.dumps(orders.batch_payload(claimed["batch"]), ensure_ascii=False)
    return Response(content=body.encode("utf-8"), media_type=_MEDIA_TYPE)


def _split(raw) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


async def _report(params: forms.Params) -> Response:
    lp = params.get("lp") or ""
    status = params.get("status") or ""
    # get_task_info() defaults the task name to "llm" (worker_acceptor_light.php:109).
    task_name = params.get("task_name") or "llm"
    call_uuid = render.call_uuid_from_lp(lp)
    if not call_uuid:
        logger.warning("report with no usable lp - ignoring")
        return _empty()

    if status == "done":
        content = params.get("content")
        content = "" if content is None else content
    elif status == "problem":
        content = "<problem>\n"   # the literal the PHP writes (:1207)
    else:
        # Any other status writes nothing and answers 200 (the 507 branch is guarded by
        # `else if($status == "done" || $status == "problem")`, :1218). `failed` additionally
        # returns the task to the queue here, which the PHP achieves only by leaving its
        # running marker to age out.
        if status == "failed":
            await _fail(call_uuid, task_name, params.get("content") or "")
        return _empty()

    try:
        await orders.record_result(
            call_uuid=call_uuid, task_name=task_name, status=status, content=content,
        )
    except orders.StorageFailed as e:
        # The answer did not reach the store, so the task stays unanswered AND the lease is
        # kept: the call is held out of the queue until it ages out rather than being handed
        # to every worker at once. The worker retries a bounded number of times
        # (ochat/llm_gateway.cur.py:2411-2436) instead of counting an unstored answer as
        # delivered - which is the loop that emptied har's inodes on 2026-09-04.
        logger.error(f"507 for {call_uuid}/{task_name}: {e}")
        return Response(content=b"", status_code=507, media_type=_MEDIA_TYPE)
    except dispatch.StaleReport as e:
        # Somebody else already answered this task. The PHP cannot notice this at all - it
        # would overwrite the result file - so answering 200 keeps the client's behaviour
        # unchanged while the second answer is dropped.
        logger.warning(f"stale report for {call_uuid}/{task_name}: {e}")
        return _empty()
    return _empty()


async def _fail(call_uuid: str, task_name: str, error: str) -> None:
    await asyncio.to_thread(
        dispatch.fail_task, call_uuid=call_uuid, task_name=task_name, error=error
    )
