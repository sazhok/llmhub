"""The golden corpus: every taskset generation deployed on this box.

A silent regression here means wrong prompts, wrong checklist item uuids and wrong verdicts
posted to a customer's backend, so the test reads the real tree rather than a fixture - the
same corpus symphony's `export_taskset.py --corpus-check` uses.
"""
import json
import os

import pytest

from llmhub import config, tasksets
from llmhub.legacy import render

PARAMS_ROOT = config.Settings().params_root
_HAS_CORPUS = os.path.isdir(PARAMS_ROOT)
corpus = pytest.mark.skipif(not _HAS_CORPUS, reason=f"no taskset tree at {PARAMS_ROOT}")


def _companies() -> list[str]:
    return sorted(
        name for name in os.listdir(PARAMS_ROOT)
        if os.path.isdir(os.path.join(PARAMS_ROOT, name, "tasksets"))
    )


def test_task_order_is_natural_not_lexicographic():
    names = ["1", "10", "2", "9", "a_3_2_10", "a_3_2_9"]
    assert sorted(names, key=tasksets.task_order) == [
        "1", "2", "9", "10", "a_3_2_9", "a_3_2_10"
    ]


@corpus
def test_every_generation_on_this_box_loads():
    seen_tasks = 0
    seen_generations = 0
    for company in _companies():
        for generation in tasksets.generations(PARAMS_ROOT, company):
            taskset = tasksets.read_taskset(PARAMS_ROOT, company, generation)
            assert taskset is not None, f"{company}/{generation} did not load"
            assert taskset.task_count > 0
            assert len(taskset.sha256) == 64
            seen_generations += 1
            seen_tasks += taskset.task_count
    assert seen_generations > 0 and seen_tasks > 0


@corpus
def test_config_name_equals_directory_name_everywhere():
    """The wire depends on it: the worker is told `task_name` from the config and reports
    under that string, and that is what identifies the task again."""
    for company in _companies():
        for generation in tasksets.generations(PARAMS_ROOT, company):
            taskset = tasksets.read_taskset(PARAMS_ROOT, company, generation)
            for name, cfg in taskset.tasks:
                assert str(cfg.get("name", name)) == name, f"{company}/{generation}/{name}"


@corpus
def test_every_task_renders_a_batch_entry_with_the_required_keys():
    required = {"company_id", "task", "task_name", "task_prompt", "body", "total_tasks",
                "lp", "remote_local_path", "url"}
    order = {"company_id": "c", "taskset": "1", "url": "u", "sequrity_key": "",
             "context": "", "order_status": ""}
    for company in _companies():
        for generation in tasksets.generations(PARAMS_ROOT, company):
            taskset = tasksets.read_taskset(PARAMS_ROOT, company, generation)
            for name, cfg in taskset.tasks:
                entry = render.batch_entry(
                    order=order, task_name=name, config=cfg, body="b",
                    total_tasks=taskset.task_count, lp="/x/y.vtt",
                )
                assert required <= set(entry), f"{company}/{generation}/{name}"
                # `command` drags its two companions along as a unit, exactly as the PHP does.
                if "command" in entry:
                    assert "param_name2value" in entry and "nonapp" in entry


@corpus
def test_backup_generations_are_never_served():
    """`<n>-<YYMMDD>` is prompts2tasks.py's copy of the previous deploy. Serving one would
    answer today's calls with last month's questions."""
    for company in _companies():
        root = os.path.join(PARAMS_ROOT, company, "tasksets")
        for name in os.listdir(root):
            if "-" in name and os.path.isdir(os.path.join(root, name)):
                assert name not in tasksets.generations(PARAMS_ROOT, company)
                assert tasksets.read_taskset(PARAMS_ROOT, company, name) is None
                break


@corpus
def test_digest_is_stable_and_content_sensitive():
    company = _companies()[0]
    generation = tasksets.generations(PARAMS_ROOT, company)[0]
    first = tasksets.read_taskset(PARAMS_ROOT, company, generation)
    second = tasksets.read_taskset(PARAMS_ROOT, company, generation)
    assert first.sha256 == second.sha256

    mutated = dict(first.tasks)
    name = first.tasks[0][0]
    mutated[name] = {**mutated[name], "prompt": json.dumps(mutated[name]) + "!"}
    changed = tasksets.Taskset(
        company_id=company, generation=generation,
        sha256=tasksets._digest(tuple(sorted(mutated.items(),
                                             key=lambda i: tasksets.task_order(i[0])))),
        tasks=first.tasks,
    )
    assert changed.sha256 != first.sha256


def test_missing_taskset_is_none_not_an_exception(tmp_path):
    assert tasksets.read_taskset(str(tmp_path), "nobody", "1") is None
    assert tasksets.generations(str(tmp_path), "nobody") == []
    assert tasksets.has_taskset(str(tmp_path), "nobody") is False
