"""Reading a deployed taskset off disk: `<params_root>/<company>/tasksets/<n>/<task>/config.json`.

`load_taskset` and `task_order` are ported from symphony/backend/history.py:117-149, which
already walks this exact tree. What is deliberately NOT ported is symphony/backend/params.py:
it normalises a taskset into symphony's own model (variables, bindings, prompt graphs), while
ochat's TaskQuery (ochat/llm_gateway.cur.py:160-205) consumes `yes_no`, `command`,
`param_name2value`, `nonapp`, `source` and `answer2crits` **raw**. Normalising here would
silently change what the model is asked.

Where the tree lives, and why here: `/home/ubuntu/hrm/local/data/params` on this box is the
*upstream* copy - `reporter/prompts/prompts2tasks.py` writes it and
`reporter/prompts/deploy_params_remote.sh:17-18` rsyncs it to har. Moving the acceptor onto
the box that already generates tasksets removes a deploy hop rather than adding one; har's
copy stays only for the companies still served by the PHP.

Two rules the directory layout imposes:

  - Only `<n>` is a live generation. `prompts2tasks.deploy_tasks_dir()` keeps the previous
    deploy beside it as `<n>-<YYMMDD>`, so anything with a dash is history and must never be
    served.
  - `config.json`'s `name` is the directory name. Checked across every generation on this box
    (1926 tasks, zero mismatches) and asserted in tests/test_tasksets.py, because the wire
    protocol depends on it: the worker is told `task_name` from the config and reports under
    that same string, which is what the PHP turned back into the `task-<dir>` file name.

The PHP re-`scandir`s the taskset directory once per call per poll
(worker_acceptor_light.php:1416) and so picks up a redeploy instantly, for free. A long-lived
process does not get that for free, hence the mtime-keyed cache below.
"""
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from logly import logger

_DIGITS = re.compile(r"(\d+)")


def task_order(name: str) -> tuple:
    """The order a task's item takes in the checklist.

    Natural, not lexicographic: `10` follows `9`. The numbering is the table's own ordering
    (prompts2tasks.py walks the rows in order), so this is what puts the items back in the
    order somebody wrote them in. Ported from symphony/backend/history.py:117-126.
    """
    return tuple((0, int(part)) if part.isdigit() else (1, part)
                 for part in _DIGITS.split(name) if part != "")


def load_taskset(path: str) -> dict[str, dict[str, Any]]:
    """One deployed generation: `{task_name: config}`.

    A task directory with no readable `config.json` is dropped rather than guessed at - a task
    whose rule cannot be read has no question this side can honestly ask. Ported from
    symphony/backend/history.py:128-149.
    """
    tasks: dict[str, dict[str, Any]] = {}
    if not os.path.isdir(path):
        return tasks
    for name in sorted(os.listdir(path)):
        config_path = os.path.join(path, name, "config.json")
        if not os.path.isfile(config_path):
            continue
        try:
            with open(config_path, encoding="utf-8") as handle:
                config = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(config, dict):
            tasks[name] = config
    return tasks


def _tree_mtime(path: str) -> float:
    """Newest mtime among the generation's config.json files, plus the directory itself.

    The directory's own mtime catches a task being added or removed; the files' catch a task
    being rewritten in place, which is what `deploy_tasks_dir()`'s rmtree+copytree does.
    """
    newest = 0.0
    try:
        newest = os.path.getmtime(path)
    except OSError:
        return 0.0
    for name in os.listdir(path):
        config_path = os.path.join(path, name, "config.json")
        try:
            newest = max(newest, os.path.getmtime(config_path))
        except OSError:
            continue
    return newest


@dataclass(frozen=True)
class Taskset:
    company_id: str
    generation: str
    sha256: str
    # Ordered by task_order, which is the order the checklist was written in.
    tasks: tuple[tuple[str, dict[str, Any]], ...]

    @property
    def task_count(self) -> int:
        """What the wire calls `total_tasks`: the size of the TASKSET, not of a batch."""
        return len(self.tasks)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {name: config for name, config in self.tasks}


def _digest(tasks: tuple[tuple[str, dict[str, Any]], ...]) -> str:
    """A stable identity for one generation's content, so an order can record which
    generation judged its call - the fact nothing has ever recorded, and the reason
    symphony/backend/history.py needed an identify() to guess it back afterwards."""
    canonical = json.dumps(
        [[name, config] for name, config in tasks],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_taskset(params_root: str, company_id: str, generation: str) -> Optional[Taskset]:
    """Read one generation straight off disk, no cache. None when it does not exist."""
    if "-" in generation:
        # `<n>-<YYMMDD>` is prompts2tasks.py's backup of the previous deploy. Serving one
        # would answer today's calls with last month's questions.
        logger.warning(f"refusing to serve backup generation {company_id}/{generation}")
        return None
    path = os.path.join(params_root, company_id, "tasksets", generation)
    raw = load_taskset(path)
    if not raw:
        return None
    ordered = tuple(sorted(raw.items(), key=lambda item: task_order(item[0])))
    return Taskset(
        company_id=company_id, generation=generation,
        sha256=_digest(ordered), tasks=ordered,
    )


# (company_id, generation) -> (taskset, tree_mtime, checked_at)
_CACHE: dict[tuple[str, str], tuple[Optional[Taskset], float, float]] = {}


def get(params_root: str, company_id: str, generation: str, ttl_s: float = 60.0
        ) -> Optional[Taskset]:
    """Cached read. Revalidates by mtime at most once per `ttl_s`, so a redeploy takes effect
    without a restart and without stat-ing the tree on every single order."""
    key = (company_id, generation)
    entry = _CACHE.get(key)
    monotonic = time.monotonic()
    if entry is not None:
        taskset, seen_mtime, checked_at = entry
        if monotonic - checked_at < ttl_s:
            return taskset
        path = os.path.join(params_root, company_id, "tasksets", generation)
        if _tree_mtime(path) == seen_mtime:
            _CACHE[key] = (taskset, seen_mtime, monotonic)
            return taskset

    taskset = read_taskset(params_root, company_id, generation)
    path = os.path.join(params_root, company_id, "tasksets", generation)
    _CACHE[key] = (taskset, _tree_mtime(path), monotonic)
    if taskset is not None:
        logger.info(
            f"taskset {company_id}/{generation}: {taskset.task_count} task(s), "
            f"sha256={taskset.sha256[:12]}"
        )
    return taskset


def invalidate() -> int:
    """Drop the whole cache (POST /v1/admin/reload-tasksets). Returns what was dropped."""
    count = len(_CACHE)
    _CACHE.clear()
    return count


def generations(params_root: str, company_id: str) -> list[str]:
    """Live generations for a company, backups excluded."""
    root = os.path.join(params_root, company_id, "tasksets")
    if not os.path.isdir(root):
        return []
    return sorted(
        (name for name in os.listdir(root)
         if "-" not in name and os.path.isdir(os.path.join(root, name))),
        key=task_order,
    )


def has_taskset(params_root: str, company_id: str, generation: str = "") -> bool:
    """Whether a company can be enabled here at all. Enforced at --enable time rather than at
    first poll, so a company whose tables never reached this box fails loudly instead of
    quietly producing an empty queue."""
    if generation:
        return read_taskset(params_root, company_id, generation) is not None
    return bool(generations(params_root, company_id))
