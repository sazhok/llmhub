"""Rendering one batch entry exactly as peek_llm_job() does.

Transcribed from worker_acceptor_light.php:1543-1574. Key order is preserved even though the
consumer parses with `yaml.safe_load` and does not care, because `scripts/shadow_compare.py`
diffs this against a real response from the live PHP and an ordered diff is easier to read.

Two subtleties carried over deliberately:

  - `isset()` is false for a null value, so a config key present but null must be treated as
    absent. `config.get(key) is not None` is the faithful translation, NOT `key in config`.
  - `command` drags `param_name2value` and `nonapp` with it as a unit: the PHP emits all three
    whenever `command` is set, defaulting only `nonapp` to "". `param_name2value` is emitted
    unguarded, so a config with `command` and no `param_name2value` yields a null - and
    ochat's FieldLogicFilter is written against that.
  - every value read out of config.json makes a round trip through PHP's array type, which
    turns some objects into arrays. See `php_shape`.
"""
from typing import Any


def php_shape(value: Any) -> Any:
    """Re-encode a config.json value the way the PHP hub does.

    `json_decode($json, true)` gives assoc arrays, and `json_encode` then writes an array as a
    JSON *list* whenever its keys are exactly 0..n-1 - PHP cannot tell the two apart. So
    monolead-1's `"param_name2value": {"0": "{Representative position}"}` leaves har as
    `["{Representative position}"]`, and an empty object leaves it as `[]`.

    Found by scripts/shadow_compare.py against a live monolead-1 batch on 2026-09-05, and it
    is not cosmetic: ochat branches on the type it gets (llm_gateway.cur.py:144-165) - a dict
    is reduced to its first value before FieldLogicFilter sees it, a list is handed over whole
    and reduced inside the filter instead (llm_logic.py:26). Both work today for the
    single-entry case every deployed taskset uses, which is exactly why this must not be left
    to chance: the shim's job is to be the same hub, not a better one.
    """
    if isinstance(value, dict):
        shaped = {key: php_shape(item) for key, item in value.items()}
        keys = list(shaped)
        if keys == [str(index) for index in range(len(keys))]:
            return [shaped[key] for key in keys]   # includes {} -> [], as PHP does
        return shaped
    if isinstance(value, list):
        return [php_shape(item) for item in value]
    return value


def batch_entry(
    *,
    order: dict,
    task_name: str,
    config: dict[str, Any],
    body: str,
    total_tasks: int,
    lp: str,
    task: str = "llm",
) -> dict[str, Any]:
    """One element of the `batch` list the worker receives."""
    entry: dict[str, Any] = {
        "company_id": order["company_id"],
        "task": task,
        # The PHP sends config.json's `name`, which is the task directory's name - checked
        # equal across every generation on this box. The worker reports back under this same
        # string, and that is what identifies the task again.
        "task_name": config.get("name", task_name),
        "task_prompt": php_shape(config.get("prompt", "")),
        # The WHOLE vtt, cue timings included: a checklist verdict has to be traceable back to
        # the moment of the call an auditor's comment points at. Only the too-short check runs
        # on the timing-free text (worker_acceptor_light.php:1505-1516).
        "body": body,
        # The size of the TASKSET, not of this batch.
        "total_tasks": total_tasks,
        "lp": lp,
        "remote_local_path": lp,
    }

    taskset = order.get("taskset") or ""
    if taskset != "":
        entry["taskset"] = taskset
    if config.get("afterword") is not None:
        entry["task_afterword"] = php_shape(config["afterword"])
    if config.get("uuid") is not None:
        entry["uuid"] = php_shape(config["uuid"])
    if config.get("yes_no") is not None:
        entry["yes_no"] = php_shape(config["yes_no"])
    if config.get("type") is not None:
        entry["type"] = php_shape(config["type"])
    if config.get("command") is not None:
        entry["command"] = php_shape(config["command"])
        entry["param_name2value"] = php_shape(config.get("param_name2value"))
        entry["nonapp"] = php_shape(config.get("nonapp", "")) if config.get("nonapp") is not None else ""
    if config.get("source") is not None:
        entry["source"] = php_shape(config["source"])
    if config.get("answer2crits") is not None:
        entry["answer2crits"] = php_shape(config["answer2crits"])

    # From the order (status.yaml in the PHP). `url` and `language` are always written there,
    # the other three only when the ordering request carried them.
    entry["url"] = order.get("url", "")
    if order.get("sequrity_key"):
        entry["sequrity_key"] = order["sequrity_key"]
    if order.get("context"):
        entry["context"] = order["context"]
    if order.get("order_status"):
        entry["order_status"] = order["order_status"]
    return entry


def legacy_lp(lp_root: str, call_uuid: str) -> str:
    """The `lp` string the worker is handed.

    It no longer names a file anywhere - llmhub touches no uploads tree - but it must keep
    this exact shape. ochat derives `record_uuid = Path(lp).stem`
    (ochat/llm_gateway.cur.py:181) and uses it as the name of its result-cache directory
    `clients/<company>/<record_uuid>/`, which `reuse_processed_calls` reads on the next run.
    Change the stem and nine workers lose their cache and re-run the model on every call.
    """
    return f"{lp_root.rstrip('/')}/{call_uuid}/{call_uuid}.vtt"


def call_uuid_from_lp(lp: str) -> str:
    """The inverse, and the client's own rule: the stem of the file name."""
    name = lp.rstrip("/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name
