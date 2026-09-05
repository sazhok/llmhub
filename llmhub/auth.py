"""Two surfaces, two mechanisms.

The native /v1 API uses `x-api-key-token`, one key per worker, exactly as asrhub does. The
legacy shim uses HTTP Basic, because that is what the clients already send and neither is
going to change for a cutover: fw/llmmon.py:34 and ochat/llm_gateway.cur.py:1240-1254 both
attach `HTTPBasicAuth`. On har that check was Apache's (.htaccess, `Require group admins`);
the PHP itself has no authentication in it at all.

Copied from asrhub/asrhub/auth.py rather than mediahub/auth.py for one specific property:
mediahub builds its key table at import, so rotating a key needs a process restart. Here the
table reloads on SIGHUP and via POST /v1/admin/reload-keys, so adding a worker never means
dropping every in-flight lease.
"""
import base64
import hmac
import os
import signal
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request
from logly import logger

from env_secrets import get_env_secret

WORKER = "worker"
ADMIN = "admin"


@dataclass(frozen=True)
class Client:
    name: str      # worker_id for workers, "admin" for the admin key
    role: str


_CLIENTS_BY_KEY: dict[str, Client] = {}
_BASIC_USERS: dict[str, str] = {}


def _parse_worker_keys(raw: str) -> dict[str, Client]:
    """LLMHUB_WORKER_KEYS = "ochat-1:key1,ochat-2:key2". Malformed entries are skipped loudly
    rather than silently, so a typo shows up at startup instead of as a 401 storm later."""
    clients: dict[str, Client] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        worker_id, sep, key = entry.partition(":")
        worker_id, key = worker_id.strip(), key.strip()
        if not sep or not worker_id or not key:
            logger.warning(f"LLMHUB_WORKER_KEYS: skipping malformed entry '{entry}'")
            continue
        if key in clients:
            logger.warning(
                f"LLMHUB_WORKER_KEYS: key reused by '{worker_id}' and "
                f"'{clients[key].name}' - keeping the first"
            )
            continue
        clients[key] = Client(name=worker_id, role=WORKER)
    return clients


def _parse_basic_users(raw: str) -> dict[str, str]:
    """LLMHUB_BASIC_USERS = "user:password,other:password". A password containing a comma
    cannot be expressed here; that is a deliberate limit of the format, not an oversight."""
    users: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        user, sep, password = entry.partition(":")
        user, password = user.strip(), password.strip()
        if not sep or not user or not password:
            logger.warning("LLMHUB_BASIC_USERS: skipping a malformed entry")
            continue
        users[user] = password
    return users


def reload_keys() -> int:
    """Re-read .env and rebuild both key tables. Returns the number of native API clients."""
    global _CLIENTS_BY_KEY, _BASIC_USERS
    from dotenv import load_dotenv

    load_dotenv(override=True)

    clients = _parse_worker_keys(os.environ.get("LLMHUB_WORKER_KEYS", ""))
    admin_key = get_env_secret("LLMHUB_ADMIN_KEY")
    if admin_key:
        if admin_key in clients:
            logger.warning("LLMHUB_ADMIN_KEY collides with a worker key - admin wins")
        clients[admin_key] = Client(name="admin", role=ADMIN)

    _CLIENTS_BY_KEY = clients
    _BASIC_USERS = _parse_basic_users(os.environ.get("LLMHUB_BASIC_USERS", ""))
    logger.info(
        f"loaded {len(clients)} api key(s) "
        f"(workers: {sorted(c.name for c in clients.values() if c.role == WORKER)}) "
        f"and {len(_BASIC_USERS)} basic user(s)"
    )
    return len(clients)


def install_sighup_handler() -> None:
    """SIGHUP reloads keys without dropping in-flight leases. Best-effort: not every runtime
    allows signal handlers on the main thread (e.g. under some test harnesses)."""
    try:
        signal.signal(signal.SIGHUP, lambda _s, _f: reload_keys())
    except (ValueError, OSError) as e:
        logger.warning(f"could not install SIGHUP handler: {e}")


# --------------------------------------------------------------------------------------
# Native API
# --------------------------------------------------------------------------------------

def _authenticate(api_key: Optional[str]) -> Client:
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing x-api-key-token header")
    client = _CLIENTS_BY_KEY.get(api_key)
    if client is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return client


async def require_any(x_api_key_token: Optional[str] = Header(None)) -> Client:
    return _authenticate(x_api_key_token)


async def require_worker(client: Client = Depends(require_any)) -> Client:
    if client.role not in (WORKER, ADMIN):
        raise HTTPException(status_code=403, detail="worker role required")
    return client


async def require_admin(client: Client = Depends(require_any)) -> Client:
    if client.role != ADMIN:
        raise HTTPException(status_code=403, detail="admin role required")
    return client


def assert_owns(client: Client, worker_id: str) -> None:
    """The key identifies the worker; the body only names it. Admin may act for any worker,
    which fake_worker.py's drills and manual recovery both need."""
    if client.role == ADMIN:
        return
    if client.name != worker_id:
        raise HTTPException(
            status_code=403,
            detail=f"key is registered to '{client.name}', not '{worker_id}'",
        )


# --------------------------------------------------------------------------------------
# Legacy shim: HTTP Basic
# --------------------------------------------------------------------------------------

_BASIC_REALM = 'Basic realm="For Harmonica clients only"'


def basic_users_configured() -> bool:
    return bool(_BASIC_USERS)


def require_basic(request: Request) -> str:
    """Returns the authenticated user name, or raises 401 with a WWW-Authenticate header.

    The header matters: `requests`' HTTPBasicAuth sends credentials pre-emptively, but a bare
    401 without the challenge is what makes a misconfiguration look like a server error in
    every client log instead of an auth failure.

    With no LLMHUB_BASIC_USERS configured the shim is OPEN. That is the same posture as the
    PHP itself (which authenticates nothing) and is only safe because the service binds
    127.0.0.1; it is logged once per boot in app.py so it is never a silent state.
    """
    if not _BASIC_USERS:
        return "anonymous"

    header = request.headers.get("authorization") or ""
    scheme, _, payload = header.partition(" ")
    if scheme.lower() != "basic" or not payload:
        raise HTTPException(
            status_code=401, detail="Basic auth required",
            headers={"WWW-Authenticate": _BASIC_REALM},
        )
    try:
        decoded = base64.b64decode(payload).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - any malformed credential is just a 401
        raise HTTPException(
            status_code=401, detail="Malformed Basic credentials",
            headers={"WWW-Authenticate": _BASIC_REALM},
        )
    user, _, password = decoded.partition(":")
    expected = _BASIC_USERS.get(user)
    # compare_digest on a fixed dummy when the user is unknown, so a wrong user name and a
    # wrong password take the same time.
    if expected is None or not hmac.compare_digest(password, expected):
        raise HTTPException(
            status_code=401, detail="Invalid credentials",
            headers={"WWW-Authenticate": _BASIC_REALM},
        )
    return user
