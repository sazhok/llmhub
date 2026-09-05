# llmhub

The LLM job queue for the call-analytics pipeline, moved off `har`'s filesystem and onto this
box as a Postgres table.

`fw` orders analysis for a finished call; nine `ochat` workers poll for it, ask the local LLM
the taskset's questions, and report each answer back. Until now that queue was
`reporter/worker_acceptor_light.php` — 2112 lines of PHP over a directory tree on `har`, where
a task is "done" iff a file named `<uuid>.<taskset>.task-<name>` exists.

Both halves of that conversation already run **here**. The transcript travelled bq → har and
the questions came back har → bq for a queue whose two clients are on the same machine.

## Why it moved

- **Polling cost.** `peek_llm_job()` (`worker_acceptor_light.php:1334`) `scandir`s ~5044 call
  directories *per request*, stats the vtt, parses `status.yaml`, scans the taskset directory
  and stats every task file. Nine workers do this continuously. Here it is one indexed
  `UPDATE ... FOR UPDATE SKIP LOCKED`.
- **Alphabetical starvation.** `scandir` order is uuid order, and the first eligible call
  always wins, so a call that keeps failing holds its early position forever. Here the order
  is `ordered_at` — oldest first.
- **Two workers, one call.** The PHP writes its `.processing_<task>` marker *after* its
  eligibility checks (`:1580`), so two pollers genuinely can be handed the same call. One
  statement with `SKIP LOCKED` cannot.
- **"Empty" and "missing" were the same fact.** A zero-byte result file means "not
  applicable"; a truncated write looks identical. Here the answer is a row, `bytes = 0` is
  data, and the 2026-09-04 disk-full patch's whole problem class is gone.
- **Inodes.** har had ~1.07M free and `df -h` looked healthy; this box has 228M and 1.5 TB.

## What it is not

**Only `task=llm`.** ASR and MT stay on the PHP hub — a client that arrives here for them gets
a 404 rather than being quietly served nothing. **Only the queue.** The bodies (transcript in,
answers out) live in `mediahub`; llmhub stores references. **Not a reporting UI.** har keeps
the history it already has; nothing is mirrored back.

## Two surfaces

| | |
|---|---|
| `POST\|GET /hub/v1/worker_acceptor_light.php` | the PHP-compatible shim, HTTP Basic. Switching a client is replacing a host name |
| `/v1/*` | the native REST API with real leases, `x-api-key-token` |

The shim is bug-compatible on purpose: an empty 200 means "no work" (`yaml.safe_load(b"")` is
`None`, which is how `ochat` learns the queue is empty), `op=ordering` **always** answers an
empty 200 even when it refuses the order (`fw` retries five times on anything else), and the
`Content-Type` stays PHP's default `text/html`. `tests/test_legacy.py` is the gate on all of it.

## Running

```bash
uv venv --python 3.13 --seed
. activate_env.inc
uv sync --extra dev
bash scripts/setup_postgres.sh     # role + database on the :5433 cluster, once

bash serve.sh fg                   # 127.0.0.1:8008
bash serve.sh                      # background, logs/llmhub.log (rotated)
bash control/status.sh             # health + queue snapshot
bash control/stop.sh               # graceful: .exit_llmhub, then waits

python3 -m pytest                  # 65 tests; the PG and mediahub ones skip if unreachable
bash scripts/verify_e2e.sh         # the contract gate: order -> peek -> report -> mediahub
```

It binds **loopback**, not the tailnet IP: both clients run on this box. `0.0.0.0` stays
forbidden — this machine has a public IP and constant scanner traffic.

## Sources: the migration valve

Every company starts **disabled**, and enabling one is refused unless its taskset tree is on
this box. 17 of har's 26 companies have one; Azov `1`, Япіко `102` and the `develop-*` set do
not, and are **not being migrated**.

```bash
python3 scripts/seed_sources.py --import-params   # discover companies from data/params
python3 scripts/seed_sources.py --list            # who is served, and who could be
python3 scripts/seed_sources.py --enable 1call-203
```

An order for a company that is not enabled is **parked, not refused** — the legacy wire has no
way to say "not mine", so refusing would lose the call. Enabling the source releases
everything parked for it.

## Cutting a company over

Three steps, and the order matters:

1. **The consumer first.** Add `"http://127.0.0.1:8008/hub/v1/worker_acceptor_light.php": {}`
   to `ochat/gateway.yaml`'s `query_url2basic_info`, leaving har's URL enabled. Harmless: the
   queue is empty.
2. **Then the producer.** Point `fw`'s `llmmon` at llmhub for that company.
3. **Then `--enable` it here.**

Enabling the consumer alone costs nothing. Enabling the producer alone means orders land where
nobody is polling, and the call goes unanalysed. Rollback is one line of YAML.

Calls already in flight on har stay on har: llmhub takes only orders placed after the switch.

## Where things live

| | |
|---|---|
| `llmhub/dispatch.py` | the queue: claim, lease, report, reap, settle. The one file to read first |
| `llmhub/legacy/` | the PHP-compatible surface — `forms.py` (POST > GET > default), `render.py` (the batch entry's exact key set), `shim.py` |
| `llmhub/tasksets.py` | `data/params/<company>/tasksets/<n>/` read into a snapshot; never a `<n>-<YYMMDD>` backup |
| `llmhub/bodies.py` | mediahub write-through with a local spool fallback |
| `llmhub/vtt.py` | `text_by_vtt()` ported byte-faithfully, quirks included |
| `cfg/settings.yaml` | lease TTLs, the 48-character/300-second short-transcript rule, roots |

An order **snapshots** its taskset (the task list and a sha256) at ordering time, so a
redeploy mid-flight does not change the questions under a call in progress — and the database
records which generation judged it, which is exactly what nothing recorded before.
