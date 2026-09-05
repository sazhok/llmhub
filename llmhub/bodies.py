"""Where a transcript and an answer actually live.

mediahub is the store; the local spool is a write-through cache in front of it, and it exists
for two independent reasons:

  - **Availability.** An order must be accepted even when mediahub is down. The legacy
    contract has no way to say "try again later" - `op=ordering` always answers an empty 200
    (worker_acceptor_light.php:112-152) and fw would simply move on - so a body that cannot be
    pushed yet is still safe on disk and pushed by the sweep.
  - **Latency.** A hand-out needs the whole vtt. Serving it from the spool costs one local
    read instead of a round trip per claimed call.

The spool is never the system of record. Once an order reaches a terminal state and its body
is known to be in mediahub, the spool copy is disposable.

Writes are tmp-file-then-rename. The PHP writes results in place and had to grow a byte-count
check to notice a short write (`write_task_result()`, worker_acceptor_light.php:1059, added
after a full filesystem turned truncated results into an unbounded re-answer loop on
2026-09-04); an atomic rename makes a partial file unobservable instead of detectable.
"""
import hashlib
import json
import os
from typing import Optional

from logly import logger

from . import mediahub_client


def _spool_path(spool_dir: str, call_uuid: str) -> str:
    # One flat directory, one file per call. At ~5000 live calls this is nothing on a box with
    # 228 million free inodes; on har, with 1.07 million, the same shape was a hazard.
    return os.path.join(spool_dir, f"{call_uuid}.vtt")


def write_spool(spool_dir: str, call_uuid: str, vtt: str) -> str:
    os.makedirs(spool_dir, exist_ok=True)
    path = _spool_path(spool_dir, call_uuid)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(vtt)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def read_spool(spool_dir: str, call_uuid: str) -> Optional[str]:
    try:
        with open(_spool_path(spool_dir, call_uuid), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def drop_spool(spool_dir: str, call_uuid: str) -> None:
    try:
        os.unlink(_spool_path(spool_dir, call_uuid))
    except OSError:
        pass


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CompanyMismatch(Exception):
    """mediahub already holds this call under a different company."""


async def store_transcript(
    *, call_uuid: str, company_id: str, vtt: str, language: Optional[str], context: str,
    spool_dir: str, source: str, timeout_s: float,
) -> tuple[str, str]:
    """Spool the transcript, then try to put it in mediahub. Returns (body_state, body_ref).

    `body_state` is 'mediahub' once the store has it and 'local' otherwise; the sweep in
    lifecycle.py retries the local ones. Either way the order is accepted.
    """
    path = write_spool(spool_dir, call_uuid, vtt)
    if not mediahub_client.configured():
        return "local", path

    existing = await mediahub_client.get_call(call_uuid, timeout_s=timeout_s)
    if existing is None:
        created = await mediahub_client.create_call(
            call_uuid=call_uuid, company_id=company_id, source=source,
            language=language, context=context, timeout_s=timeout_s,
        )
        if created is None:
            return "local", path
    elif existing.get("company_id") != company_id:
        # Refuse rather than write. mediahub would log the mismatch and carry on
        # (mediahub/db.py:282-288); joining two customers' calls under one uuid is worse than
        # keeping this body local and saying so.
        raise CompanyMismatch(
            f"{call_uuid} is stored under company {existing.get('company_id')!r}, "
            f"this order claims {company_id!r}"
        )

    if await mediahub_client.put_transcript(call_uuid, vtt, timeout_s=timeout_s):
        return "mediahub", f"mediahub:{call_uuid}"
    return "local", path


async def load_body(*, call_uuid: str, spool_dir: str, timeout_s: float) -> Optional[str]:
    """The whole vtt, timings included, as the worker must receive it."""
    body = read_spool(spool_dir, call_uuid)
    if body is not None:
        return body
    return await mediahub_client.get_transcript(call_uuid, timeout_s=timeout_s)


async def store_result(
    *, call_uuid: str, taskset: str, task_name: str, content: str, status: str,
    checklist_item_uuid: str = "", taskset_sha256: str = "", spool_dir: str,
    timeout_s: float,
) -> tuple[bool, str]:
    """Store one task's answer. Returns (stored, reference).

    An empty answer is a real answer - the pipeline has always used a zero-byte result file to
    mean "not applicable" - so `content: ""` with `empty: true` is written, never an absent
    row. Nothing downstream may infer "unfinished" from a length.
    """
    payload = {
        "content": content,
        "status": status,
        "bytes": len(content.encode("utf-8")),
        "empty": content == "",
        "checklist_item_uuid": checklist_item_uuid,
        "taskset_sha256": taskset_sha256,
    }
    ref_id = f"{taskset}/{task_name}"
    if mediahub_client.configured():
        annotation_id = await mediahub_client.put_annotation(
            call_uuid=call_uuid, kind="llm_task_result",
            ref_id=ref_id, payload=payload, timeout_s=timeout_s,
        )
        if annotation_id is not None:
            return True, f"mediahub:{annotation_id}"

    # Fall back to the spool so an answer is never lost because the store was unreachable.
    directory = os.path.join(spool_dir, "results", call_uuid)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{taskset}.task-{task_name}.json")
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as e:
        logger.error(f"could not spool result {call_uuid}/{ref_id}: {e}")
        return False, ""
    return True, f"spool:{path}"
