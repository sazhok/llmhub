"""Which companies llmhub serves. The migration valve, as data.

A company is served only once its row says `enabled`, which is what makes the cutover
per-company rather than all-at-once. Enabling is refused when the company's taskset tree is
not on this box: 17 of har's 26 companies have one here, and a company whose tables were never
generated locally (Azov `1`, Япіко `102`, the `develop-*` set) would otherwise be switched on
to a queue nobody can answer.

`company_id` is the frozen `source_key` wire format (`1call-102`, `552`) that fw, mediahub and
asrhub already agree on. Nothing here re-derives it.
"""
from typing import Optional

from logly import logger

from . import db, dispatch, tasksets


def list_sources() -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT s.*, "
            "       (SELECT count(*) FROM orders o "
            "         WHERE o.company_id = s.company_id AND o.state = 'ready') AS ready, "
            "       (SELECT count(*) FROM orders o "
            "         WHERE o.company_id = s.company_id AND o.state = 'parked') AS parked "
            "  FROM sources s ORDER BY s.company_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.release(conn)


def upsert(company_id: str, *, params_dir: str = "", max_attempts: int = 5,
           priority: int = 0, note: str = "") -> dict:
    conn = db.connect()
    try:
        row = conn.execute(
            "INSERT INTO sources (company_id, params_dir, max_attempts, priority, note) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (company_id) DO UPDATE SET params_dir = EXCLUDED.params_dir, "
            "    max_attempts = EXCLUDED.max_attempts, priority = EXCLUDED.priority, "
            "    note = EXCLUDED.note, updated_at = now() "
            "RETURNING *",
            (company_id, params_dir, max_attempts, priority, note),
        ).fetchone()
        conn.commit()
        return dict(row)
    finally:
        db.release(conn)


def params_root_for(company_id: str, default_root: str) -> str:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT params_dir FROM sources WHERE company_id = %s", (company_id,)
        ).fetchone()
    finally:
        db.release(conn)
    return (row["params_dir"] if row and row["params_dir"] else default_root)


class NoTaskset(Exception):
    """Enabling a company whose taskset tree never reached this box."""


def set_enabled(company_id: str, enabled: bool, *, params_root: str,
                force: bool = False) -> dict:
    """Enable or disable a company. Enabling releases everything parked for it.

    The taskset check happens HERE rather than at first poll, so a company that cannot be
    served fails at the moment somebody asks for it, loudly, instead of producing a queue that
    silently never drains.
    """
    if enabled and not force:
        root = params_root_for(company_id, params_root)
        if not tasksets.has_taskset(root, company_id):
            raise NoTaskset(
                f"no taskset under {root}/{company_id}/tasksets - this company's tables have "
                f"not been deployed to this box (prompts2tasks.py writes them here; "
                f"deploy_params_remote.sh only pushes them to har)"
            )

    conn = db.connect()
    try:
        row = conn.execute(
            "INSERT INTO sources (company_id, enabled) VALUES (%s, %s) "
            "ON CONFLICT (company_id) DO UPDATE SET enabled = EXCLUDED.enabled, "
            "    updated_at = now() RETURNING *",
            (company_id, enabled),
        ).fetchone()
        conn.commit()
    finally:
        db.release(conn)

    logger.info(f"source {company_id}: enabled={enabled}")
    if enabled:
        dispatch.release_parked(company_id)
    return dict(row)


def get(company_id: str) -> Optional[dict]:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM sources WHERE company_id = %s", (company_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.release(conn)
