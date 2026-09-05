"""HTTP client for mediahub, where the bodies live.

llmhub owns the queue; mediahub owns the transcript and the answers. Three rules shape every
call below, and each one is a trap somebody would otherwise hit:

  1. **llmhub only ever CREATES a call row; it never updates one.** `upsert_call`'s freshness
     guard (mediahub/db.py:289-291) silently drops a whole push - channels included - when the
     stored row already has a `source_mtime` and the incoming one is absent or older: it
     answers 200 with `accepted=false` and writes nothing. Re-asserting `source` would also
     relabel a call that fw or asrhub owns.
  2. **A company mismatch is refused, not merged.** mediahub logs it (db.py:282-288) but still
     writes; a `call_uuid` colliding across two companies must not quietly join two customers'
     data, so we check before pushing.
  3. **An unreachable mediahub is not an error the queue propagates.** Every function here
     returns None/False rather than raising into a request path. The order is accepted anyway
     and the body stays in the spool - see bodies.py.
"""
from typing import Any, Optional

import httpx
from logly import logger

from env_secrets import get_env_secret

_CLIENT: Optional[httpx.AsyncClient] = None


def _base_url() -> str:
    return (get_env_secret("MEDIAHUB_URL") or "").rstrip("/")


def configured() -> bool:
    return bool(_base_url() and get_env_secret("MEDIAHUB_API_KEY"))


def _client(timeout_s: float) -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(
            base_url=_base_url(),
            headers={"x-api-key-token": get_env_secret("MEDIAHUB_API_KEY")},
            timeout=timeout_s,
        )
    return _CLIENT


async def close() -> None:
    global _CLIENT
    if _CLIENT is not None:
        await _CLIENT.aclose()
        _CLIENT = None


async def get_call(call_uuid: str, timeout_s: float = 20.0) -> Optional[dict]:
    if not configured():
        return None
    try:
        response = await _client(timeout_s).get(f"/v1/calls/{call_uuid}")
    except httpx.HTTPError as e:
        logger.warning(f"mediahub get_call({call_uuid}) failed: {e}")
        return None
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        logger.warning(f"mediahub get_call({call_uuid}): HTTP {response.status_code}")
        return None
    return response.json()


async def create_call(
    *, call_uuid: str, company_id: str, source: str, language: Optional[str] = None,
    context: str = "", source_mtime: Optional[float] = None, timeout_s: float = 20.0,
) -> Optional[dict]:
    """Declare an audio-less call.

    Legal by construction: `CallIn.channels` defaults to `[]` (mediahub/models.py:29) and
    `upsert_call` only iterates what the payload carries (mediahub/db.py:330). We arrive with
    a transcript and no audio, which is exactly the shape mediahub's two-step ingest allows.
    """
    if not configured():
        return None
    payload: dict[str, Any] = {
        "call_uuid": call_uuid, "company_id": company_id, "source": source,
        "ingest_status": "partial", "channels": [],
    }
    if language:
        payload["language"] = language
    if context:
        payload["context"] = context
    if source_mtime is not None:
        payload["source_mtime"] = source_mtime
    try:
        response = await _client(timeout_s).post("/v1/calls", json=payload)
    except httpx.HTTPError as e:
        logger.warning(f"mediahub create_call({call_uuid}) failed: {e}")
        return None
    if response.status_code >= 400:
        logger.warning(
            f"mediahub create_call({call_uuid}): HTTP {response.status_code} "
            f"{response.text[:200]}"
        )
        return None
    return response.json()


async def put_transcript(call_uuid: str, vtt: str, timeout_s: float = 20.0) -> bool:
    if not configured():
        return False
    try:
        response = await _client(timeout_s).put(
            f"/v1/calls/{call_uuid}/transcript", params={"format": "vtt"},
            content=vtt.encode("utf-8"),
            headers={"content-type": "text/vtt; charset=utf-8"},
        )
    except httpx.HTTPError as e:
        logger.warning(f"mediahub put_transcript({call_uuid}) failed: {e}")
        return False
    if response.status_code >= 400:
        logger.warning(
            f"mediahub put_transcript({call_uuid}): HTTP {response.status_code} "
            f"{response.text[:200]}"
        )
        return False
    return True


async def get_transcript(call_uuid: str, timeout_s: float = 20.0) -> Optional[str]:
    if not configured():
        return None
    try:
        response = await _client(timeout_s).get(
            f"/v1/calls/{call_uuid}/transcript", params={"format": "vtt"}
        )
    except httpx.HTTPError as e:
        logger.warning(f"mediahub get_transcript({call_uuid}) failed: {e}")
        return None
    if response.status_code >= 400:
        return None
    return response.text


async def put_annotation(
    *, call_uuid: str, kind: str, ref_id: str, payload: dict, timeout_s: float = 20.0,
) -> Optional[str]:
    """Store one task answer. Returns an opaque reference, or None when it did not land.

    There is deliberately no `source` argument: mediahub takes the writer from the API key
    (mediahub/app.py, `put_annotation`), so no caller can write a row under another writer's
    name - which is what makes the (call, source, kind, ref_id) key an ownership claim rather
    than a suggestion.
    """
    if not configured():
        return None
    body = {"kind": kind, "ref_id": ref_id, "payload": payload}
    try:
        response = await _client(timeout_s).put(
            f"/v1/calls/{call_uuid}/annotations", json=body
        )
    except httpx.HTTPError as e:
        logger.warning(f"mediahub put_annotation({call_uuid}/{ref_id}) failed: {e}")
        return None
    if response.status_code >= 400:
        logger.warning(
            f"mediahub put_annotation({call_uuid}/{ref_id}): HTTP {response.status_code} "
            f"{response.text[:200]}"
        )
        return None
    try:
        return str(response.json().get("id", ""))
    except ValueError:
        return ""
