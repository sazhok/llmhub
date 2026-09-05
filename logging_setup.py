"""App-level logging via logly (console + rotating file sink).

Mirrors mediahub/logging_setup.py and asrhub/logging_setup.py. Separate from uvicorn's own
access/lifecycle logs, which uvicorn_log_config.json routes into a rotating logs/llmhub.log;
this sink gives db.py / dispatch.py / legacy/ a rotated, timestamped app log at
logs/<name>.app.log.
"""
import os

from logly import logger

_configured = False


def setup_logging(name: str = "llmhub") -> None:
    global _configured
    if _configured:
        return

    logger.remove_all()

    logger.add(
        "console",
        date_enabled=True,
        date_style="local",
        format="{time} [{level}] {message}",
    )

    os.makedirs("logs", exist_ok=True)
    logger.add(
        f"logs/{name}.app.log",
        size_limit="50MB",
        retention=5,
        date_enabled=True,
        date_style="local",
        format="{time} [{level}] {message}",
    )

    _configured = True
