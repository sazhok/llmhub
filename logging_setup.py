"""App-level logging via logly (console + rotating file sink).

Mirrors mediahub/logging_setup.py and asrhub/logging_setup.py. Separate from uvicorn's own
access/lifecycle logs, which uvicorn_log_config.json routes into a rotating logs/llmhub.log;
this sink gives db.py / dispatch.py / legacy/ a rotated, timestamped app log at
logs/<name>.app.log.

logly 0.2.x API. The 0.1.6 spelling this replaced - remove_all(), the "console" sink name,
size_limit=, date_enabled/date_style - declared a 50 MB bound that logly never honoured.
Measured 2026-09-24 on the pinned 0.1.6 build: a sink declared at 1 MB rotated once and then
grew to 42 MB, 42x its limit, while retention did bound the file count. It is the same defect
fw/CLAUDE.md records from rejecting logly for fw/log_rotation.py. 0.2.5 holds it - the same
80 MB into a 1 MB sink leaves 2.5 MB across three files - which is why the pin moved. Keep it
pinned: this API is a breaking rewrite away from 0.1.6 in both directions.
"""
import dataclasses
import os

from logly import logger

_configured = False

# 0.2.x runs str.format() over EVERY message, arguments or not, and 0.1.6 did not. Every log
# line here is an f-string, so a message that merely contains braces - a dict, a set, a JSON
# body - raises KeyError/IndexError from inside logger.info() instead of logging. In asrhub it
# cost llm_local_deploy 1297 calls 2026-10-01..08 (the stage raised on its last log line after
# writing its files, and the retry recorded `skipped`). raw=True restores 0.1.6's "the message
# is the text". Set on the shared singleton because every module binds `from logly import
# logger` at import time; logger.exception() carries it into its own view. `_options` is
# private, which the exact pin makes tolerable - re-check it on any version move.
logger._options = dataclasses.replace(logger._options, raw=True)

# {time} alone renders whole seconds in 0.2.x, where 0.1.6 rendered RFC3339 nanoseconds. The
# explicit spec keeps sub-second resolution, which is what these logs get read for.
_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} [{level}] {message}"


def setup_logging(name: str = "llmhub") -> None:
    global _configured
    if _configured:
        return

    logger.remove()                 # 0.2.x: remove(), not remove_all()

    logger.add("stderr", format=_FORMAT)    # 0.2.x names the console sink by its stream

    os.makedirs("logs", exist_ok=True)
    logger.add(
        f"logs/{name}.app.log",
        rotation="50 MB",           # 0.2.x: rotation=, not size_limit=
        retention=5,
        format=_FORMAT,
    )

    _configured = True
