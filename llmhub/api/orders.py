"""Native ordering API. The producer's side of the queue."""
import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, dispatch, orders

router = APIRouter(prefix="/v1")


class OrderIn(BaseModel):
    call_uuid: str
    company_id: str
    transcript: str
    taskset: str = ""
    url: str = ""
    language: Optional[str] = None
    sequrity_key: str = ""
    context: str = ""
    order_status: str = ""


@router.post("/orders")
async def create_order(payload: OrderIn, client: auth.Client = Depends(auth.require_worker)):
    """Unlike the legacy shim, this says out loud when it will not take an order.

    The empty-200-on-refusal rule exists only because fw cannot hear anything else; a caller
    that speaks this API can be told.
    """
    try:
        order = await orders.accept_order(**payload.model_dump())
    except orders.OrderRejected as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {
        "order_id": order["order_id"], "call_uuid": order["call_uuid"],
        "company_id": order["company_id"], "taskset": order["taskset"],
        "taskset_sha256": order["taskset_sha256"], "state": order["state"],
        "speech_chars": order["speech_chars"], "body_state": order["body_state"],
    }


def _public(order: dict) -> dict:
    """Everything about an order except the customer's credential."""
    return {key: value for key, value in order.items() if key != "sequrity_key"}


@router.get("/orders/{call_uuid}")
async def get_order(call_uuid: str, client: auth.Client = Depends(auth.require_worker)):
    snapshot = await asyncio.to_thread(dispatch.get_order, call_uuid)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="no order for this call")
    return {
        "order": _public(snapshot["order"]),
        "tasks": [
            {key: value for key, value in task.items() if key != "config_json"}
            for task in snapshot["tasks"]
        ],
    }


@router.get("/orders/{call_uuid}/results")
async def get_results(call_uuid: str, client: auth.Client = Depends(auth.require_worker)):
    """Where each answer went, not the answers themselves - those live in mediahub, which is
    the store. Returning them from here would make llmhub a second place to look."""
    snapshot = await asyncio.to_thread(dispatch.get_order, call_uuid)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="no order for this call")
    return {
        "call_uuid": call_uuid,
        "taskset": snapshot["order"]["taskset"],
        "taskset_sha256": snapshot["order"]["taskset_sha256"],
        "results": [
            {
                "task_name": task["task_name"], "state": task["state"],
                "status": task["result_status"], "bytes": task["result_bytes"],
                # 0 bytes is "not applicable", a real answer - never an unfinished task.
                "empty": task["result_bytes"] == 0,
                "ref": task["result_ref"], "at": task["result_at"],
            }
            for task in snapshot["tasks"]
        ],
    }
