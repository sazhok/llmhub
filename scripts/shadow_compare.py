#!/usr/bin/env python3
"""Compare llmhub's batch against the live PHP hub's, key by key.

The cheapest proof available before a cutover: ochat parses whatever the hub returns with
`yaml.safe_load` and then reads fixed keys out of it (ochat/llm_gateway.cur.py:1338-1360), so
"compatible" means *the same key set with the same kinds of value* - not merely valid JSON.

    # 1. once, and it costs something - see below
    python3 scripts/shadow_compare.py --capture-har --out samples/har-1call-203.json

    # 2. as often as you like, free
    python3 scripts/shadow_compare.py --sample samples/har-1call-203.json --company 1call-203

**What `--capture-har` costs.** Peeking the production hub is not read-only: `peek_llm_job()`
marks the call it hands out with `.processing_<task>` (worker_acceptor_light.php:1580), and
this script never reports an answer. The marker ages out after RUNNING_INDICATOR_MAX_AGE_S
(30 minutes), after which the call is offered again and analysed normally. So the price is
**one real call delayed by up to half an hour**, and nothing else: no answer is written, no
customer webhook is called, nothing is deleted. It is deliberately behind a flag, and the
captured sample is written to disk so the price is paid once rather than once per comparison.
"""
import argparse
import json
import os
import sys

import httpx
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from env_secrets import get_env_secret  # noqa: E402

HAR_URL = "https://api.harmonica.cloud/hub/v1/worker_acceptor_light.php"
LOCAL_URL = (os.environ.get("LLMHUB_URL") or "http://100.97.153.111:8008") + \
            "/hub/v1/worker_acceptor_light.php"

# worker_acceptor_light.php:1546-1580. Present on every entry, unconditionally.
ALWAYS = {"company_id", "task", "task_name", "task_prompt", "body", "total_tasks", "lp",
          "remote_local_path"}
# Emitted only when the taskset row or the order carries them - PHP's isset() is false for a
# null, so an absent key and a null key are the same statement.
CONDITIONAL = {"taskset", "task_afterword", "uuid", "yes_no", "type", "command",
               "param_name2value", "nonapp", "source", "answer2crits", "url", "sequrity_key",
               "context", "order_status"}


def _har_auth() -> tuple[str, str]:
    """The same credential fw and ochat use. Read, never printed."""
    user = get_env_secret("HARMONICA_BASIC_AUTH_USER") or get_env_secret("DEFAULT_BASIC_AUTH_USER")
    password = (get_env_secret("HARMONICA_BASIC_AUTH_PASSWORD")
                or get_env_secret("DEFAULT_BASIC_AUTH_PASSWORD"))
    if not (user and password):
        raise SystemExit("no harmonica Basic credential in .env "
                         "(HARMONICA_BASIC_AUTH_USER / _PASSWORD)")
    return user, password


def _local_auth() -> tuple[str, str]:
    raw = (get_env_secret("LLMHUB_BASIC_USERS") or "").split(",")[0].strip()
    if ":" not in raw:
        raise SystemExit("LLMHUB_BASIC_USERS is not set to user:password")
    user, _, password = raw.partition(":")
    return user, password


def peek(url: str, auth, include: str = "", exclude: str = "") -> list[dict]:
    data = {"task": "llm"}
    if include:
        data["include_company_ids"] = include
    if exclude:
        data["exclude_company_ids"] = exclude
    response = httpx.post(url, auth=auth, data=data, timeout=60)
    response.raise_for_status()
    if not response.content.strip():
        return []
    return (yaml.safe_load(response.content) or {}).get("batch", [])


def _kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def describe(batch: list[dict], label: str) -> dict:
    if not batch:
        print(f"{label}: empty batch")
        return {}
    entry = batch[0]
    print(f"{label}: {len(batch)} entr(y|ies), company={entry.get('company_id')}, "
          f"task_set={entry.get('taskset')}, total_tasks={entry.get('total_tasks')}")
    print(f"  task names: {[e.get('task_name') for e in batch]}")
    return {key: _kind(value) for key, value in entry.items()}


def compare(har: dict, local: dict) -> int:
    problems = 0
    har_keys, local_keys = set(har), set(local)

    missing_always = ALWAYS - local_keys
    if missing_always:
        print(f"  MISSING (always required): {sorted(missing_always)}")
        problems += 1

    only_har = har_keys - local_keys
    if only_har:
        # A key the PHP emitted and llmhub did not. Fatal if ochat reads it.
        print(f"  har has, llmhub lacks: {sorted(only_har)}")
        problems += 1

    only_local = local_keys - har_keys
    if only_local:
        # Extra keys are usually harmless - ochat reads by name - but an unexpected one is
        # worth seeing, since it may mean a value that should have been omitted was emitted
        # as an empty string instead (PHP's isset() rule).
        print(f"  llmhub has, har lacks: {sorted(only_local)}")

    for key in sorted(har_keys & local_keys):
        if har[key] != local[key] and not {har[key], local[key]} <= {"string", "number"}:
            print(f"  type differs for {key!r}: har={har[key]} llmhub={local[key]}")
            problems += 1

    unknown = (har_keys | local_keys) - ALWAYS - CONDITIONAL
    if unknown:
        print(f"  keys in neither documented set (check the PHP source): {sorted(unknown)}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--capture-har", action="store_true",
                        help="peek the LIVE hub once - delays one real call by up to 30 min")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation. For a script, which must never LOOP on "
                             "this: the prompt is written without a newline, so a wrapper "
                             "matching '^saved ' on the output never matches and captures "
                             "again and again - five real calls were marked in progress that "
                             "way on 2026-09-05 instead of one")
    parser.add_argument("--out", default="", help="where to save a captured sample")
    parser.add_argument("--sample", default="", help="a previously captured har batch")
    parser.add_argument("--company", default="", help="restrict the peek to one company")
    parser.add_argument("--har-url", default=HAR_URL)
    parser.add_argument("--local-url", default=LOCAL_URL)
    args = parser.parse_args()

    if args.capture_har:
        print("This peeks the PRODUCTION hub. One real call will be marked in-progress and,")
        print("because nothing here answers it, will wait up to 30 minutes before being")
        print("offered again. No answer is written and no customer webhook is called.")
        if not args.yes and input("type 'yes' to continue: ").strip() != "yes":
            return 1
        print()
        batch = peek(args.har_url, _har_auth(), include=args.company)
        if not batch:
            print("the live hub had no work to hand out - try again later")
            return 1
        out = args.out or f"samples/har-{batch[0].get('company_id', 'unknown')}.json"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as handle:
            json.dump(batch, handle, ensure_ascii=False, indent=2)
        print(f"saved {len(batch)} entr(y|ies) to {out}")
        print(f"NOTE: call {batch[0].get('lp')} is now waiting out its marker.")
        har_batch = batch
    elif args.sample:
        with open(args.sample, encoding="utf-8") as handle:
            har_batch = json.load(handle)
    else:
        parser.error("give either --sample FILE or --capture-har")

    local_batch = peek(args.local_url, _local_auth(), include=args.company)

    print()
    har_shape = describe(har_batch, "har  ")
    local_shape = describe(local_batch, "llmhub")
    if not local_shape:
        print("\nllmhub had nothing to offer - order a call for this company first, or pass "
              "--company")
        return 1

    print("\ndifferences:")
    problems = compare(har_shape, local_shape)
    if problems:
        print(f"\n{problems} incompatibilit(y|ies) - do NOT switch this company yet")
        return 1
    print("  none - the key sets and value kinds agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
