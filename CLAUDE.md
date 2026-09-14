# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`llmhub` is the LLM job queue itself, moved off `../reporter`'s filesystem and onto this box as a Postgres table (port 8008). `../fw`'s `llmmon` orders analysis for a finished call and `../ochat`'s nine gateway workers poll for it; until now that queue was `worker_acceptor_light.php` scanning ~5044 call directories per request on `har`, for a conversation between two processes on the same machine.

Scope is deliberately narrow: **`task=llm` only** (ASR and MT stay on the PHP, and a client arriving here for them gets a 404), bodies in `../mediahub` rather than here, and no reporting UI. It is to `ochat`'s hub what `../asrhub` is to `fw`'s gateway — a per-company migration behind a valve, with every company disabled until its taskset tree is on this box. **`1call-203` has been live on it since 2026-09-05**; every other company still orders from `har`.

Read `README.md`; see `../CLAUDE.md` for how this sits beside the other projects.

It is an ordinary FastAPI request/response service, not a filesystem-polling loop — the thing it *replaced* was the filesystem.

## Commands

```bash
uv venv --python 3.13 --seed   # first time only
. activate_env.inc
uv sync

bash scripts/setup_postgres.sh     # role + database on the :5433 cluster, once

bash serve.sh fg                   # foreground, 100.97.153.111:8008 (the tailnet IP)
bash serve.sh                      # background, logs/llmhub.log (rotated)
bash control/status.sh             # health + queue snapshot + who is served
bash control/stop.sh               # graceful: .exit_llmhub, then waits for real exit

# the migration valve - every company starts disabled and cannot be enabled
# without its taskset tree under ../data/params
python3 scripts/seed_sources.py --import-params
python3 scripts/seed_sources.py --list
python3 scripts/seed_sources.py --enable 1call-203

# tests
python3 -m pytest                  # 65; the PG and mediahub ones skip if unreachable
bash scripts/verify_e2e.sh         # the contract gate: order -> peek -> report -> mediahub
python3 scripts/fake_worker.py     # drains the queue against the real API
python3 scripts/shadow_compare.py --sample <file> --company <id>   # key-by-key vs har's PHP
```

`scripts/shadow_compare.py --capture-har` is the one thing here that is **not** free: peeking
the live PHP hub marks the call it hands out (`.processing_<task>`), so the price is one real
call delayed by up to 30 minutes. The sample is written to disk so it is paid once.

It owns a role and a database on the isolated PostgreSQL cluster on port **5433** (`LLMHUB_PG_*` in `.env`), so it cannot read or corrupt `../mediahub`'s, `../asrhub`'s or `../symphony`'s tables. The schema is created on first start by `db.ensure_schema()` — there are **no migrations**, and adding a column means an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` line that runs idempotently on every boot.

## Architecture

The same shape as `../asrhub` one layer up the pipeline, and for the same reason: a queue whose
two clients both run on this box had been living as a directory tree on `har`.

`llmhub/dispatch.py` is the file to read first — claim, lease, report, reap, settle. A claim is
one `UPDATE ... FOR UPDATE SKIP LOCKED` ordered by `ordered_at`, which replaces three separate
properties of the PHP: the per-request `scandir` of every call directory, `scandir`'s uuid
ordering (a call that keeps failing held its early position forever), and the window between
`peek_llm_job()`'s eligibility checks and its `.processing_<task>` marker in which two pollers
could be handed the same call. **A zero-byte answer is data** — "not applicable" and "the write
was truncated" were the same fact on a filesystem and are different rows here.

Two surfaces, because the clients are not changing for the cutover: `llmhub/legacy/` is the
PHP-compatible shim at `/hub/v1/worker_acceptor_light.php` (HTTP Basic, the same credential
`har`'s `.htaccess` checked), bug-compatible on purpose — an empty 200 means "no work" because
`yaml.safe_load(b"")` is `None`, `op=ordering` answers an empty 200 even when it refuses the
order (`fw` retries five times on anything else), and the `Content-Type` stays PHP's default
`text/html`; `tests/test_legacy.py` is the gate on all of it. `/v1/*` is the native API with
real leases and `x-api-key-token`. The one deliberate deviation: a `task` other than `llm`
returns **404** rather than falling through, since ASR and MT stay on the PHP.

An order **snapshots** its taskset (`llmhub/tasksets.py`, read from `data/params/<company>/
tasksets/<n>/`, never a `<n>-<YYMMDD>` backup) with a sha256 at ordering time, so a redeploy
mid-flight cannot change the questions under a call in progress — and the database records
which generation judged it, which nothing recorded before. Bodies are `mediahub`'s
(`llmhub/bodies.py`, write-through with a local spool fallback so the queue never stops
because the store is down); `llmhub` stores references.

It listens on the **tailnet IP**, not loopback, and that is a fact about where the clients are: `ochat`'s gateway workers poll from this box, but the producer does not run here at all. `fw`'s `llmmon` lives on `oa`/`qd`/`az`, and `send_job` is followed by an unconditional `llm_task_ok = True` (`fw/llmmon.py:624`) that deletes the job file regardless — so a hub the producer cannot reach does not retry, it loses the call.

Sources are the migration valve (`llmhub/sources.py`): every company starts disabled, enabling
one is refused unless its taskset tree is on this box, and an order for a company not yet
enabled is **parked, not refused** — the legacy wire has no way to say "not mine", so refusing
would lose the call. Enabling the source releases what was parked.

It is also the second writer into `../mediahub` after `asrhub`: it writes the transcript and every answer there, one `annotations` row per answered checklist task.

### Conventions

- **Migrating a company from the PHP hub to `llmhub` is a three-sided switch, and the order is not interchangeable.** (1) the consumer: `../ochat/gateway.yaml`'s `query_url2basic_info` gains `"http://100.97.153.111:8008/hub/v1/worker_acceptor_light.php": {}` with `har`'s URL still enabled, and the gateway workers are restarted (that file is read at startup); (2) the producer: the company is listed under that URL in `../fw/cfg/query_url_template2company_ids.yaml` and `llmmon` is restarted; (3) `python3 scripts/seed_sources.py --enable <company_id>` here. Step 1 alone is harmless — the queue is empty and the extra poll returns an empty 200. Step 2 alone is not: orders land in a queue nobody polls and the call is never analysed. Rollback is deleting one line of YAML; calls already ordered stay on the hub that took them.
- **Binds the tailnet IP, never `0.0.0.0` and deliberately not loopback** — for the mirror of `asrhub`'s reason: the consumer is on this box but the producer is not. `serve.sh` refuses to bind anything but loopback with no `LLMHUB_BASIC_USERS`, since the shim authenticates nothing without it.
- **Secrets via `.env`, never hardcoded** — same as every other project in this directory.
- **No migrations**: `db.ensure_schema()` runs on every boot; adding a column is an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` line.
