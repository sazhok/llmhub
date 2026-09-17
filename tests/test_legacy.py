"""The contract gate: how the shim decodes a request and encodes a batch entry.

These are the pure halves of the PHP compatibility promise. The stateful half (empty-200,
507, stale reports) lives in test_dispatch.py, which needs a database.
"""
from llmhub.legacy import forms, render


class _Query(dict):
    """Stands in for Starlette's QueryParams: .get and .keys are all forms.Params uses."""


def _params(form=None, query=None):
    return forms.Params(form, _Query(query or {}))


# --------------------------------------------------------------------------------------
# get_option(): POST wins over GET, per parameter (worker_acceptor_light.php:2074-2082)
# --------------------------------------------------------------------------------------

def test_post_wins_over_get_for_the_same_name():
    assert _params({"task": "llm"}, {"task": "asr"}).get("task") == "llm"


def test_get_is_used_when_the_body_lacks_the_name():
    assert _params({"op": "reporting"}, {"task": "llm"}).get("task") == "llm"


def test_precedence_is_per_parameter_not_per_request():
    """A caller may pass `task` in the query string and `content` in the body; both are read.
    That is why the dispatch decision itself has to go through this."""
    params = _params({"content": "answer"}, {"task": "llm", "op": "reporting"})
    assert params.get("task") == "llm"
    assert params.get("op") == "reporting"
    assert params.get("content") == "answer"


def test_default_is_returned_only_when_absent_everywhere():
    assert _params({}, {}).get("task", "asr") == "asr"
    # An empty string is present, not absent - the PHP's isset() says so too.
    assert _params({"task": ""}, {}).get("task", "asr") == ""


# --------------------------------------------------------------------------------------
# lp: a synthetic identifier that must keep its exact shape
# --------------------------------------------------------------------------------------

def test_lp_round_trips_through_the_client_rule():
    uuid = "7b1e2c34-0000-4444-8888-aaaabbbbcccc"
    lp = render.legacy_lp("/home/ubuntu/data/uploads", uuid)
    assert lp == f"/home/ubuntu/data/uploads/{uuid}/{uuid}.vtt"
    # ochat derives the call id as Path(lp).stem (ochat/llm_gateway.cur.py:181) and uses it as
    # its result-cache directory name; a different stem costs nine workers their cache.
    assert render.call_uuid_from_lp(lp) == uuid


def test_call_uuid_from_lp_tolerates_a_name_without_an_extension():
    assert render.call_uuid_from_lp("/a/b/uuid") == "uuid"


# --------------------------------------------------------------------------------------
# The batch entry (worker_acceptor_light.php:1543-1574)
# --------------------------------------------------------------------------------------

_ORDER = {
    "company_id": "1call-203", "taskset": "1", "url": "https://example.invalid/hook",
    "sequrity_key": "CUSTOMER-TOKEN", "context": "", "order_status": "",
}


def _entry(config, order=None, total_tasks=17, **kwargs):
    return render.batch_entry(
        order=order or _ORDER, task_name="1", config=config, body="BODY",
        total_tasks=total_tasks, lp="/x/uuid/uuid.vtt", **kwargs,
    )


def test_always_present_keys():
    entry = _entry({"name": "1", "prompt": "ask something"})
    for key in ("company_id", "task", "task_name", "task_prompt", "body", "total_tasks",
                "lp", "remote_local_path", "url"):
        assert key in entry
    assert entry["task"] == "llm"
    assert entry["lp"] == entry["remote_local_path"] == "/x/uuid/uuid.vtt"
    assert entry["body"] == "BODY"


def test_total_tasks_is_the_taskset_size_not_the_batch_size():
    assert _entry({"name": "1"}, total_tasks=17)["total_tasks"] == 17


def test_optional_keys_are_omitted_when_absent():
    entry = _entry({"name": "1", "prompt": ""})
    for key in ("task_afterword", "uuid", "yes_no", "type", "command", "param_name2value",
                "nonapp", "source", "answer2crits"):
        assert key not in entry


def test_a_null_value_counts_as_absent_like_php_isset():
    entry = _entry({"name": "1", "yes_no": None, "uuid": None, "source": None})
    assert "yes_no" not in entry and "uuid" not in entry and "source" not in entry


def test_command_drags_param_name2value_and_nonapp_along():
    entry = _entry({"name": "1", "command": "field",
                    "param_name2value": {"0": "{X}"}, "nonapp": "{call dropped}"})
    assert entry["command"] == "field"
    # A list, not the {"0": ...} the config file holds - see php_shape below.
    assert entry["param_name2value"] == ["{X}"]
    assert entry["nonapp"] == "{call dropped}"


# --------------------------------------------------------------------------------------
# php_shape(): the config value's round trip through PHP's one array type
# --------------------------------------------------------------------------------------

def test_a_zero_indexed_object_leaves_as_a_list():
    """json_decode($json, true) then json_encode: PHP cannot tell {"0": x} from [x].

    Caught by scripts/shadow_compare.py against a live monolead-1 batch, 2026-09-05: har was
    sending ["{Representative position}"] where the config.json on disk holds
    {"0": "{Representative position}"}. ochat takes a different branch for each shape."""
    assert render.php_shape({"0": "a"}) == ["a"]
    assert render.php_shape({"0": "a", "1": "b"}) == ["a", "b"]


def test_an_empty_object_leaves_as_an_empty_list():
    assert render.php_shape({}) == []


def test_a_non_sequential_object_stays_an_object():
    assert render.php_shape({"1": "a"}) == {"1": "a"}
    assert render.php_shape({"0": "a", "name": "b"}) == {"0": "a", "name": "b"}


def test_the_rule_is_recursive_and_leaves_scalars_alone():
    assert render.php_shape({"k": {"0": {"0": "deep"}}}) == {"k": [["deep"]]}
    assert render.php_shape("plain") == "plain"
    assert render.php_shape(None) is None
    assert render.php_shape(["a", {"0": "b"}]) == ["a", ["b"]]


def test_every_config_derived_value_goes_through_it():
    entry = _entry({"name": "1", "command": "field", "yes_no": {"0": "так/ні"},
                    "answer2crits": {"0": {"0": "crit"}}, "source": {"0": "script"}})
    assert entry["yes_no"] == ["так/ні"]
    assert entry["answer2crits"] == [["crit"]]
    assert entry["source"] == ["script"]


def test_command_without_param_name2value_still_emits_it_as_null():
    """The PHP reads $task_info["param_name2value"] unguarded, so it emits null - and ochat's
    FieldLogicFilter is written against exactly that."""
    entry = _entry({"name": "1", "command": "field"})
    assert "param_name2value" in entry and entry["param_name2value"] is None
    assert entry["nonapp"] == ""


def test_pass_through_values_come_from_the_order():
    entry = _entry({"name": "1"})
    assert entry["url"] == "https://example.invalid/hook"
    assert entry["sequrity_key"] == "CUSTOMER-TOKEN"
    # Empty context/order_status are omitted, as the PHP omits what status.yaml never got.
    assert "context" not in entry and "order_status" not in entry

    order = {**_ORDER, "context": "prior call", "order_status": "reordered"}
    entry = _entry({"name": "1"}, order=order)
    assert entry["context"] == "prior call" and entry["order_status"] == "reordered"


def test_taskset_is_emitted_when_the_order_has_one():
    assert _entry({"name": "1"})["taskset"] == "1"
    assert "taskset" not in _entry({"name": "1"}, order={**_ORDER, "taskset": ""})


def test_task_name_comes_from_the_config_not_the_directory():
    """They are equal across every generation on this box (test_tasksets asserts it), but the
    PHP reads the config, so this does too - if they ever diverge, both agree on which wins."""
    assert _entry({"name": "renamed"})["task_name"] == "renamed"
