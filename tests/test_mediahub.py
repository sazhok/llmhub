"""The body store, against a real mediahub.

Five traps, each of which loses data silently rather than loudly, which is why they are worth
a test each:

  1. An audio-less call must be legal - llmhub arrives with a transcript and no audio.
  2. `upsert_call`'s freshness guard answers **200 with `accepted=false`** and writes nothing
     when the stored row already has a `source_mtime` and the incoming one is older or absent
     (mediahub/db.py:289-291). A second POST for an existing call is therefore not a harmless
     no-op - it is a whole push dropped without an error.
  3. Another writer's transcript is not ours to overwrite.
  4. A `call_uuid` colliding across two companies must not quietly join two customers' data.
  5. Re-answering a task replaces that answer rather than stacking a second one beside it.

Skips rather than fails when mediahub is unreachable, so `pytest` still passes on a box
without it.
"""
import asyncio
import uuid as uuidlib

import pytest

from env_secrets import get_env_secret
from llmhub import mediahub_client as mh

COMPANY = "test-9902"
OTHER_COMPANY = "test-9903"
SOURCE = "llmhub"

pytestmark = pytest.mark.skipif(not mh.configured(),
                                reason="MEDIAHUB_URL / MEDIAHUB_API_KEY not set")

VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nдобрий день\n"


# One event loop for the whole module. mediahub_client caches a single httpx.AsyncClient at
# module scope, and a client built on a loop that asyncio.run() has since closed raises
# "Event loop is closed" on its next use - which is exactly how the real service uses it (one
# long-lived loop, one long-lived client), so the tests share one too.
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    assert _LOOP is not None, "the `reachable` fixture has not run"
    return _LOOP.run_until_complete(coro)


async def _delete(call_uuid: str, source: str = SOURCE) -> None:
    client = mh._client(20.0)
    await client.delete(f"/v1/calls/{call_uuid}", params={"expect_source": source})


@pytest.fixture(scope="module", autouse=True)
def reachable():
    global _LOOP
    import httpx
    url = (get_env_secret("MEDIAHUB_URL") or "").rstrip("/")
    try:
        response = httpx.get(f"{url}/health", timeout=5.0)
        response.raise_for_status()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"mediahub unreachable: {e}")
    _LOOP = asyncio.new_event_loop()
    yield
    _run(mh.close())
    _LOOP.close()
    _LOOP = None


@pytest.fixture
def call_uuid():
    value = f"pytest-{uuidlib.uuid4()}"
    yield value
    try:
        _run(_delete(value))
    except Exception:  # noqa: BLE001 - a test that never created it
        pass


def test_a_call_with_no_audio_is_legal(call_uuid):
    created = _run(mh.create_call(call_uuid=call_uuid, company_id=COMPANY, source=SOURCE,
                                  language="uk"))
    assert created is not None
    assert created["call_uuid"] == call_uuid
    assert created.get("channels", []) == []


def test_the_transcript_round_trips(call_uuid):
    _run(mh.create_call(call_uuid=call_uuid, company_id=COMPANY, source=SOURCE))
    assert _run(mh.put_transcript(call_uuid, VTT)) is True
    assert _run(mh.get_transcript(call_uuid)) == VTT


def test_get_call_reports_absence_as_none_not_as_an_error(call_uuid):
    assert _run(mh.get_call(call_uuid)) is None


def test_a_second_create_is_never_issued_because_the_row_is_checked_first(call_uuid):
    """The guard llmhub relies on: look before pushing.

    A blind second POST is what the freshness rule silently swallows, so the client contract
    is "create only when get_call says there is nothing".
    """
    assert _run(mh.get_call(call_uuid)) is None
    _run(mh.create_call(call_uuid=call_uuid, company_id=COMPANY, source=SOURCE,
                        source_mtime=1_700_000_000.0))
    existing = _run(mh.get_call(call_uuid))
    assert existing is not None and existing["source"] == SOURCE

    # And this is the trap being avoided: an older mtime is accepted with a 200 that changed
    # nothing. Asserting the shape here so a future mediahub that starts erroring instead is
    # noticed by this test rather than in production.
    stale = _run(mh.create_call(call_uuid=call_uuid, company_id=COMPANY, source=SOURCE,
                                language="ro", source_mtime=1_600_000_000.0))
    after = _run(mh.get_call(call_uuid))
    assert stale is not None
    assert after["language"] != "ro", "the stale push was applied - the guard is gone"


def test_a_company_mismatch_is_detectable_before_pushing(call_uuid):
    _run(mh.create_call(call_uuid=call_uuid, company_id=COMPANY, source=SOURCE))
    existing = _run(mh.get_call(call_uuid))
    assert existing["company_id"] == COMPANY
    # bodies.store_transcript raises CompanyMismatch on exactly this comparison rather than
    # pushing into a row belonging to another customer.
    assert existing["company_id"] != OTHER_COMPANY


def test_another_writers_call_is_recognisable_as_theirs(call_uuid):
    """llmhub creates a call row only when nothing else has. `source` is how it tells."""
    _run(mh.create_call(call_uuid=call_uuid, company_id=COMPANY, source="fw"))
    existing = _run(mh.get_call(call_uuid))
    assert existing["source"] == "fw"
    _run(_delete(call_uuid, source="fw"))


# --------------------------------------------------------------------------------------
# Annotations
# --------------------------------------------------------------------------------------

def test_an_annotation_upsert_is_idempotent(call_uuid):
    _run(mh.create_call(call_uuid=call_uuid, company_id=COMPANY, source=SOURCE))
    first = _run(mh.put_annotation(call_uuid=call_uuid, kind="llm_task_result", ref_id="1/3",
                                   payload={"content": "так", "empty": False}))
    second = _run(mh.put_annotation(call_uuid=call_uuid, kind="llm_task_result", ref_id="1/3",
                                    payload={"content": "ні", "empty": False}))
    assert first is not None and first == second, "a re-answer created a second row"

    client = mh._client(20.0)
    rows = _run(client.get(f"/v1/calls/{call_uuid}/annotations")).json()
    assert len(rows) == 1
    assert rows[0]["payload"]["content"] == "ні"
    assert rows[0]["source"] == SOURCE, "the writer came from the body, not the key"


def test_an_empty_answer_is_stored_as_a_row_not_as_an_absence(call_uuid):
    """The whole reason the queue moved: "no file" and "not applicable" were the same fact."""
    _run(mh.create_call(call_uuid=call_uuid, company_id=COMPANY, source=SOURCE))
    ref = _run(mh.put_annotation(call_uuid=call_uuid, kind="llm_task_result", ref_id="1/7",
                                 payload={"content": "", "bytes": 0, "empty": True}))
    assert ref is not None
    client = mh._client(20.0)
    rows = _run(client.get(f"/v1/calls/{call_uuid}/annotations",
                           params={"kind": "llm_task_result"})).json()
    assert [r["payload"] for r in rows] == [{"content": "", "bytes": 0, "empty": True}]


def test_different_tasks_of_one_call_are_different_rows(call_uuid):
    _run(mh.create_call(call_uuid=call_uuid, company_id=COMPANY, source=SOURCE))
    for name in ("1", "2", "script"):
        _run(mh.put_annotation(call_uuid=call_uuid, kind="llm_task_result",
                               ref_id=f"1/{name}", payload={"content": name}))
    client = mh._client(20.0)
    rows = _run(client.get(f"/v1/calls/{call_uuid}/annotations")).json()
    assert sorted(r["ref_id"] for r in rows) == ["1/1", "1/2", "1/script"]


def test_the_same_task_under_a_different_taskset_is_a_different_row(call_uuid):
    """A redeploy changes the questions; the old generation's answers stay readable."""
    _run(mh.create_call(call_uuid=call_uuid, company_id=COMPANY, source=SOURCE))
    _run(mh.put_annotation(call_uuid=call_uuid, kind="llm_task_result", ref_id="1/1",
                           payload={"content": "old"}))
    _run(mh.put_annotation(call_uuid=call_uuid, kind="llm_task_result", ref_id="2/1",
                           payload={"content": "new"}))
    client = mh._client(20.0)
    rows = _run(client.get(f"/v1/calls/{call_uuid}/annotations")).json()
    assert len(rows) == 2


def test_annotations_cannot_be_deleted_in_another_writers_name(call_uuid):
    _run(mh.create_call(call_uuid=call_uuid, company_id=COMPANY, source=SOURCE))
    _run(mh.put_annotation(call_uuid=call_uuid, kind="llm_task_result", ref_id="1/1",
                           payload={"content": "mine"}))
    client = mh._client(20.0)
    refused = _run(client.delete(f"/v1/calls/{call_uuid}/annotations",
                                 params={"expect_source": "symphony"}))
    assert refused.status_code == 409
    allowed = _run(client.delete(f"/v1/calls/{call_uuid}/annotations",
                                 params={"expect_source": SOURCE}))
    assert allowed.status_code == 200 and allowed.json()["deleted"] == 1


def test_annotations_go_with_the_call(call_uuid):
    _run(mh.create_call(call_uuid=call_uuid, company_id=COMPANY, source=SOURCE))
    _run(mh.put_annotation(call_uuid=call_uuid, kind="llm_task_result", ref_id="1/1",
                           payload={"content": "x"}))
    _run(_delete(call_uuid))
    client = mh._client(20.0)
    assert _run(client.get(f"/v1/calls/{call_uuid}/annotations")).status_code == 404
