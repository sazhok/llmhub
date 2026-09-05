"""Static settings from cfg/settings.yaml.

Same split of responsibility as asrhub: YAML is *authored* (things a human writes and reviews
in git), the database is *operational* (which companies are enabled, what is queued, what is
leased - changed at runtime through /v1/admin and expected to survive a restart).

Every timing default below is inherited from the PHP rather than chosen, and each names the
constant it comes from, because changing one silently changes production behaviour.
"""
from dataclasses import dataclass
from pathlib import Path

import yaml

CFG_ROOT = Path("cfg")


@dataclass(frozen=True)
class Settings:
    # Where deployed tasksets live. On this box the upstream copy that prompts2tasks.py
    # writes; har's /home/ubuntu/data/params is the rsync target, not the source.
    params_root: str = "/home/ubuntu/hrm/local/data/params"

    # Bodies that could not reach mediahub wait here. Never the system of record.
    spool_dir: str = "work/spool"

    # The path the legacy wire calls `lp`. It no longer names a file anywhere - it is a
    # synthetic identifier - but it must keep this exact shape: ochat derives
    # `record_uuid = Path(lp).stem` (ochat/llm_gateway.cur.py:181) and uses it as the name of
    # its result-cache directory clients/<company>/<record_uuid>/, which `reuse_processed_calls`
    # reads. Change the stem and every worker loses its cache and re-runs the model.
    legacy_lp_root: str = "/home/ubuntu/data/uploads"

    # order_llm_task() defaults a missing task_set to "1" (worker_acceptor_light.php:1239).
    default_taskset: str = "1"

    # RUNNING_INDICATOR_MAX_AGE_S (worker_acceptor_light.php:26). A marker older than this was
    # treated as absent and its task offered again; a lease expiring is the same decision made
    # explicitly. Unlike the marker, a lease is renewed by progress, not only by hand-out.
    lease_ttl_s: float = 30 * 60
    reaper_interval_s: float = 30.0

    # The short-transcript rule: less than 48 characters of SPEECH is not worth a model run,
    # but only settle it once the transcript has stopped changing - order_llm_task() rewrites
    # the vtt in place, so a poll landing in that window could read a truncated one
    # (SHORT_TRANSCRIPT_SETTLE_S, worker_acceptor_light.php:33).
    min_speech_chars: int = 48
    short_transcript_settle_s: float = 5 * 60
    settle_interval_s: float = 60.0

    # How long before a taskset generation is re-checked on disk. The PHP re-scandirs per poll
    # and so has no staleness at all; this is the price of a long-lived process.
    taskset_ttl_s: float = 60.0

    # A worker that already held an order is offered it last, not refused - a single-worker
    # fleet must still be able to retry its own job (asrhub/dispatch.py's same rule).
    claim_grace_s: float = 120.0

    # The engine being down costs time, not calls: the attempt is refunded and the order parked
    # this long. asrhub learned this on 2026-08-27, when 28 calls were buried in ~15 seconds
    # while the STT host was down; a local vLLM restart is the identical failure mode.
    unavailable_retry_s: float = 120.0

    # mediahub declares calls under this `source`, so DELETE and ownership checks there can
    # tell llmhub's rows from fw's and asrhub's.
    mediahub_source: str = "llmhub"
    mediahub_timeout_s: float = 20.0

    @staticmethod
    def from_obj(obj: dict) -> "Settings":
        known = {field: obj[field] for field in Settings.__dataclass_fields__ if field in obj}
        return Settings(**known)


_SETTINGS: Settings | None = None


def settings(cfg_root: Path = CFG_ROOT) -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        path = cfg_root / "settings.yaml"
        raw = {}
        if path.is_file():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _SETTINGS = Settings.from_obj(raw)
    return _SETTINGS


def reload(cfg_root: Path = CFG_ROOT) -> Settings:
    global _SETTINGS
    _SETTINGS = None
    return settings(cfg_root)
