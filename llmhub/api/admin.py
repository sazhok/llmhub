"""Operator surface: which companies are served, what is queued, reload after a deploy."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth, config, dispatch, sources, tasksets

router = APIRouter(prefix="/v1/admin")


@router.post("/reload-keys")
async def reload_keys(client: auth.Client = Depends(auth.require_admin)):
    return {"clients": auth.reload_keys()}


@router.post("/reload-tasksets")
async def reload_tasksets(client: auth.Client = Depends(auth.require_admin)):
    """After prompts2tasks.py writes a new generation. The cache also revalidates itself by
    mtime, so this is for when you do not want to wait for taskset_ttl_s."""
    return {"dropped": tasksets.invalidate()}


@router.get("/sources")
async def list_sources(client: auth.Client = Depends(auth.require_admin)):
    settings = config.settings()
    rows = await asyncio.to_thread(sources.list_sources)
    for row in rows:
        root = row["params_dir"] or settings.params_root
        row["generations"] = tasksets.generations(root, row["company_id"])
    return {"sources": rows, "params_root": settings.params_root}


class EnableIn(BaseModel):
    enabled: bool
    force: bool = False


@router.post("/sources/{company_id}/enabled")
async def set_enabled(company_id: str, payload: EnableIn,
                      client: auth.Client = Depends(auth.require_admin)):
    try:
        row = await asyncio.to_thread(
            sources.set_enabled, company_id, payload.enabled,
            params_root=config.settings().params_root, force=payload.force,
        )
    except sources.NoTaskset as e:
        # 409, not 404: the request was permitted, the world is not what it assumed.
        raise HTTPException(status_code=409, detail=str(e))
    return row


@router.get("/queue")
async def queue(client: auth.Client = Depends(auth.require_admin)):
    return await asyncio.to_thread(dispatch.queue_stats)
