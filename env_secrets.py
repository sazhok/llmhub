"""Secrets come from a git-ignored .env, never from source, config files, or the database.

Same shape as mediahub/env_secrets.py, asrhub/env_secrets.py and ochat/env_secrets.py.

Note what is NOT a secret of ours: a call's `sequrity_key` arrives on the wire with every
order and is handed straight back to the worker, which uses it as `x-api-key-token` against
the *customer's* backend (ochat/llm_gateway.cur.py:1508). It is a pass-through we store and
echo, so it must never reach a log line, an error body, or an /v1/admin response.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def get_env_secret(env_var_name: str) -> str:
    return os.environ.get(env_var_name, "") if env_var_name else ""


def masked(value: str, keep: int = 4) -> str:
    """Render a secret for logging. fw learned this the hard way - fw/run_cur/*.sh still
    contain live API keys in plaintext because a generated shell script interpolated one."""
    if not value:
        return "<empty>"
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)
