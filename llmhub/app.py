"""llmhub - the LLM checklist queue.

One process, one database, two API surfaces: a native REST/JSON one with leases, and a shim
that speaks worker_acceptor_light.php's wire protocol so fw and ochat move by changing a URL.

What it replaces is a directory tree used as a queue. See llmhub/db.py for why that had to
stop being a directory tree, and llmhub/legacy/shim.py for exactly which of its behaviours are
reproduced on purpose.

Three background tasks run for the process lifetime:

    lifecycle.reaper()       expired leases -> ready
    lifecycle.settler()      calls with too little speech -> settled, once they stop changing
    lifecycle.body_pusher()  transcripts that could not reach mediahub yet

All are cancelled on shutdown before the pool closes, so nothing is mid-statement when the
connections go.
"""
import asyncio
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from logly import logger

# env_secrets/logging_setup live at the repo root beside the package, matching mediahub's and
# asrhub's flat layout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logging_setup import setup_logging  # noqa: E402

from . import auth, config, db, dispatch, lifecycle, mediahub_client  # noqa: E402
from .api import admin as admin_api  # noqa: E402
from .api import leases as leases_api  # noqa: E402
from .api import orders as orders_api  # noqa: E402
from .legacy import shim  # noqa: E402

VERSION = "0.1.0"
_STARTED_AT = time.monotonic()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging("llmhub")
    auth.reload_keys()
    auth.install_sighup_handler()
    db.init_pool()
    db.ensure_schema()
    # One process owns this queue, so nothing can legitimately be leased at boot.
    dispatch.requeue_orphaned_leases()

    settings = config.settings()
    Path(settings.spool_dir).mkdir(parents=True, exist_ok=True)

    if not auth.basic_users_configured():
        logger.warning(
            "LLMHUB_BASIC_USERS is empty - the legacy shim accepts any caller. That matches "
            "the PHP (which authenticates nothing; Apache did it) and is only safe because "
            "this process binds loopback."
        )
    if not mediahub_client.configured():
        logger.warning(
            "MEDIAHUB_URL/MEDIAHUB_API_KEY unset - transcripts and answers stay in "
            f"{settings.spool_dir} and are pushed when the store is configured"
        )

    tasks = [
        asyncio.create_task(lifecycle.watch_exit_sentinel(), name="exit-watcher"),
        asyncio.create_task(lifecycle.reaper(), name="reaper"),
        asyncio.create_task(lifecycle.settler(), name="settler"),
        asyncio.create_task(lifecycle.body_pusher(), name="body-pusher"),
    ]
    logger.info(f"llmhub {VERSION} up: params_root={settings.params_root}")
    yield

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await mediahub_client.close()
    db.close_pool()
    logger.complete()


app = FastAPI(title="llmhub", version=VERSION, lifespan=lifespan)
app.include_router(orders_api.router)
app.include_router(leases_api.router)
app.include_router(admin_api.router)
app.include_router(shim.router)


@app.get("/health")
async def health():
    """Unauthenticated, and it DOES touch the database: a hub that cannot reach Postgres
    cannot hand out a single job, so reporting it healthy would be a lie."""
    return {
        "status": "draining" if lifecycle.is_draining() else "ok",
        "version": VERSION,
        "db": await asyncio.to_thread(db.ping),
        "mediahub": mediahub_client.configured(),
        "uptime_s": round(time.monotonic() - _STARTED_AT, 1),
    }


@app.get("/v1/whoami")
async def whoami(client: auth.Client = Depends(auth.require_any)):
    return {"client": client.name, "role": client.role}
