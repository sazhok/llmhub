#!/usr/bin/env python3
"""ochat's polling loop, with the model call replaced by a sleep.

The point is to exercise the wire, not the LLM: the same three verbs, the same form encoding,
the same `yaml.safe_load` of the response body that ochat does
(ochat/llm_gateway.cur.py:1338-1341), the same `lp`-stem-as-uuid derivation. (httpx rather
than ochat's `requests`, to avoid a dependency that only a test script would need - the bytes
on the wire are the same form POST.) If this drains a
queue, a real worker will too - and unlike a real worker it can be killed mid-batch on purpose.

    python3 scripts/fake_worker.py                       # poll until idle, then stop
    python3 scripts/fake_worker.py --follow --think 2     # keep polling
    python3 scripts/fake_worker.py --die-after 3          # exit mid-batch, for the reaper test
    python3 scripts/fake_worker.py --problem-every 5      # report `problem` now and then
"""
import argparse
import os
import sys
import time

import httpx
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from env_secrets import get_env_secret  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8008/hub/v1/worker_acceptor_light.php"


def _basic() -> tuple[str, str]:
    """The shim's Basic credential, from the same variable the real clients read."""
    raw = get_env_secret("LLMHUB_BASIC_USERS") or ""
    first = raw.split(",")[0].strip()
    if ":" not in first:
        raise SystemExit("LLMHUB_BASIC_USERS is not set to user:password")
    user, _, password = first.partition(":")
    return user, password


def poll(url: str, auth, worker_id: str) -> list[dict]:
    response = httpx.post(url, auth=auth,
                          data={"task": "llm", "worker_id": worker_id}, timeout=30)
    response.raise_for_status()
    if not response.content.strip():
        return []
    payload = yaml.safe_load(response.content)
    return (payload or {}).get("batch", [])


def report(url: str, auth, entry: dict, status: str, content: str) -> int:
    response = httpx.post(url, auth=auth, data={
        "task": "llm", "op": "reporting", "lp": entry["lp"],
        "task_name": entry["task_name"], "status": status, "content": content,
    }, timeout=30)
    return response.status_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--worker-id", default=f"fake-{os.getpid()}")
    parser.add_argument("--think", type=float, default=0.0, help="seconds per task")
    parser.add_argument("--follow", action="store_true", help="keep polling when idle")
    parser.add_argument("--idle-sleep", type=float, default=2.0)
    parser.add_argument("--die-after", type=int, default=0,
                        help="exit hard after N answers, leaving the lease to expire")
    parser.add_argument("--problem-every", type=int, default=0)
    parser.add_argument("--max-batches", type=int, default=0)
    args = parser.parse_args()

    auth = _basic()
    answered = batches = 0
    while True:
        batch = poll(args.url, auth, args.worker_id)
        if not batch:
            if not args.follow:
                print(f"queue empty after {batches} batch(es), {answered} answer(s)")
                return 0
            time.sleep(args.idle_sleep)
            continue

        batches += 1
        print(f"batch {batches}: {len(batch)} task(s) of {batch[0]['company_id']} "
              f"{batch[0]['lp']}")
        for entry in batch:
            if args.think:
                time.sleep(args.think)
            answered += 1
            if args.die_after and answered >= args.die_after:
                print(f"dying with {len(batch) - batch.index(entry)} task(s) unanswered - "
                      f"the lease must expire and be re-handed")
                os._exit(9)
            if args.problem_every and answered % args.problem_every == 0:
                code = report(args.url, auth, entry, "problem", "")
            else:
                # An empty answer every seventh task: "not applicable" is a real answer and
                # must survive the round trip as one.
                content = "" if answered % 7 == 0 else f"answer for {entry['task_name']}"
                code = report(args.url, auth, entry, "done", content)
            if code != 200:
                print(f"  task {entry['task_name']}: HTTP {code}", file=sys.stderr)
        if args.max_batches and batches >= args.max_batches:
            print(f"stopping after {batches} batch(es), {answered} answer(s)")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
