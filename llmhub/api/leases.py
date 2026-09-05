"""Native worker API: leases as capabilities.

The difference from the legacy shim is not the shape of the JSON, it is that a lease token is
minted per hand-out and re-minted on every re-dispatch. A holder whose lease expired and was
given to somebody else presents a token that matches no row and is told 409, so it discards
its answer instead of overwriting a fresher one. The PHP has no way to notice that at all -
it writes the result file whoever asks.
"""
import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from .. import auth, config, dispatch, orders

router = APIRouter(prefix="/v1")


class LeaseRequest(BaseModel):
    worker_id: str
    include_companies: list[str] = []
    exclude_companies: list[str] = []


@router.post("/leases")
async def take_lease(payload: LeaseRequest, client: auth.Client = Depends(auth.require_worker)):
    auth.assert_owns(client, payload.worker_id)
    claimed = await orders.claim_batch(
        worker_id=payload.worker_id,
        include=payload.include_companies, exclude=payload.exclude_companies,
    )
    if claimed is None:
        # 204, not an empty 200: a native client should not have to guess. The shim keeps the
        # empty 200 because ochat's `yaml.safe_load(b"") is None` depends on it.
        return Response(status_code=204)
    order = claimed["order"]
    return {
        "token": str(order["lease_token"]),
        "expires_at": order["lease_expires_at"],
        "order": {
            "order_id": order["order_id"], "call_uuid": order["call_uuid"],
            "company_id": order["company_id"], "taskset": order["taskset"],
            "taskset_sha256": order["taskset_sha256"],
            "lease_generation": order["lease_generation"],
        },
        "batch": claimed["batch"],
    }


class ResultIn(BaseModel):
    task_name: str
    status: str          # done | problem | failed
    content: str = ""


class ResultsIn(BaseModel):
    results: list[ResultIn]


def _order_for_token(token: str) -> dict:
    from .. import db
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM orders WHERE lease_token = %s", (token,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        db.release(conn)


@router.post("/leases/{token}/renew")
async def renew(token: str, client: auth.Client = Depends(auth.require_worker)):
    order = await asyncio.to_thread(dispatch.renew, token, config.settings().lease_ttl_s)
    if order is None:
        raise HTTPException(status_code=409, detail="lease no longer held")
    return {"token": token, "expires_at": order["lease_expires_at"]}


@router.post("/leases/{token}/results")
async def post_results(token: str, payload: ResultsIn,
                       client: auth.Client = Depends(auth.require_worker)):
    order = await asyncio.to_thread(_order_for_token, token)
    if not order:
        raise HTTPException(status_code=409, detail="lease no longer held")
    accepted, rejected = [], []
    for result in payload.results:
        if result.status == "failed":
            await asyncio.to_thread(
                dispatch.fail_task, call_uuid=order["call_uuid"],
                task_name=result.task_name, error=result.content,
            )
            rejected.append({"task_name": result.task_name, "reason": "reported failed"})
            continue
        try:
            outcome = await orders.record_result(
                call_uuid=order["call_uuid"], task_name=result.task_name,
                status=result.status, content=result.content, lease_token=token,
            )
        except orders.StorageFailed as e:
            # The task stays unanswered and the lease is kept, so the call is held rather than
            # offered to everybody at once.
            raise HTTPException(status_code=507, detail=str(e))
        except dispatch.StaleReport as e:
            rejected.append({"task_name": result.task_name, "reason": str(e)})
            continue
        accepted.append({"task_name": result.task_name, "order_state": outcome["order_state"]})
    if rejected and not accepted:
        raise HTTPException(status_code=409, detail={"rejected": rejected})
    return {"accepted": accepted, "rejected": rejected}


class UnavailableIn(BaseModel):
    reason: str = ""


@router.post("/leases/{token}/unavailable")
async def unavailable(token: str, payload: UnavailableIn,
                      client: auth.Client = Depends(auth.require_worker)):
    """The engine is down, not the job: the attempt is refunded and the order parked briefly.

    Without this an outage spends every attempt a call has. asrhub buried 28 calls in about
    fifteen seconds on 2026-08-27 for exactly this reason, and a local vLLM restart is the
    same event.
    """
    settings = config.settings()
    order = await asyncio.to_thread(
        dispatch.mark_unavailable, token, settings.unavailable_retry_s, payload.reason
    )
    if order is None:
        raise HTTPException(status_code=409, detail="lease no longer held")
    return {"order_id": order["order_id"], "retry_after": order["retry_after"]}


@router.delete("/leases/{token}")
async def release(token: str, client: auth.Client = Depends(auth.require_worker)):
    order = await asyncio.to_thread(dispatch.release_lease, token)
    if order is None:
        raise HTTPException(status_code=409, detail="lease no longer held")
    return {"order_id": order["order_id"], "state": order["state"]}
