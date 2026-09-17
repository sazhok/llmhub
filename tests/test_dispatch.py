"""The queue itself, under contention, against a real Postgres.

The invariant everything rests on: **a ready order is leased to exactly one worker at a time.**
If it breaks, two ochat workers ask the model the same twenty questions about the same call,
one of them loses the reporting race, and the fleet quietly does half the work it appears to.
That is not hypothetical here - it is what the PHP does today, since `peek_llm_job()` touches
`.processing_<task>` only *after* its eligibility checks (worker_acceptor_light.php:1580).

These tests use the real database with reserved fixture companies and clean up after
themselves. They **skip rather than fail** when no server is reachable, like
asrhub/tests/test_dispatch.py, so `pytest` still passes on a box with no cluster.
"""
import threading
import time

import pytest

try:
    from llmhub import db, dispatch, sources
    _IMPORT_ERROR = ""
except Exception as e:  # noqa: BLE001
    _IMPORT_ERROR = repr(e)

# Reserved for tests, and distinct from anything verify_e2e.sh uses, so a test run and an e2e
# run cannot disturb each other.
TEST_COMPANY = "test-9902"
OFF_COMPANY = "test-9903"
COMPANIES = (TEST_COMPANY, OFF_COMPANY)

pytestmark = pytest.mark.skipif(bool(_IMPORT_ERROR),
                                reason=f"database deps unavailable: {_IMPORT_ERROR}")

LEASE = dict(lease_ttl_s=1800.0, min_speech_chars=48, grace_s=120.0)


@pytest.fixture(scope="module")
def database():
    try:
        db.init_pool()
        db.ensure_schema()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no database: {e}")
    conn = db.connect()
    try:
        for company, enabled in ((TEST_COMPANY, True), (OFF_COMPANY, False)):
            conn.execute(
                "INSERT INTO sources (company_id, enabled, note) VALUES (%s, %s, 'pytest') "
                "ON CONFLICT (company_id) DO UPDATE SET enabled = EXCLUDED.enabled",
                (company, enabled))
        conn.commit()
    finally:
        db.release(conn)
    _cleanup()
    yield
    _cleanup()
    conn = db.connect()
    try:
        conn.execute("DELETE FROM sources WHERE company_id = ANY(%s)", (list(COMPANIES),))
        conn.commit()
    finally:
        db.release(conn)
    db.close_pool()


def _cleanup():
    conn = db.connect()
    try:
        conn.execute(
            "DELETE FROM orders WHERE company_id = ANY(%s)", (list(COMPANIES),))
        conn.commit()
    finally:
        db.release(conn)


@pytest.fixture
def clean(database):
    _cleanup()
    yield
    _cleanup()


def _order(uuid_suffix: str, *, company: str = TEST_COMPANY, tasks: int = 3,
           speech_chars: int = 500) -> dict:
    return dispatch.create_order(
        call_uuid=f"pytest-{uuid_suffix}",
        company_id=company, taskset="1", taskset_sha256="deadbeef",
        tasks=[(str(i + 1), {"name": str(i + 1), "prompt": "ask"}) for i in range(tasks)],
        speech_chars=speech_chars, url="https://example.invalid/hook",
        sequrity_key="CUSTOMER-TOKEN")


def _state(call_uuid: str) -> str:
    conn = db.connect()
    try:
        return conn.execute("SELECT state FROM orders WHERE call_uuid = %s "
                            "ORDER BY order_id DESC LIMIT 1",
                            (f"pytest-{call_uuid}",)).fetchone()["state"]
    finally:
        db.release(conn)


def _task_states(call_uuid: str) -> dict[str, str]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT t.task_name, t.state FROM tasks t "
            " WHERE t.order_id = (SELECT max(order_id) FROM orders WHERE call_uuid = %s)",
            (f"pytest-{call_uuid}",)).fetchall()
        return {r["task_name"]: r["state"] for r in rows}
    finally:
        db.release(conn)


def _expire(call_uuid: str) -> None:
    conn = db.connect()
    try:
        conn.execute("UPDATE orders SET lease_expires_at = now() - interval '1 second' "
                     " WHERE call_uuid = %s", (f"pytest-{call_uuid}",))
        conn.commit()
    finally:
        db.release(conn)


# --------------------------------------------------------------------------------------
# Hand-out
# --------------------------------------------------------------------------------------

def test_a_whole_call_is_leased_at_once_not_one_task_at_a_time(clean):
    """ochat's TaskQuery.sources makes a call's tasks depend on each other (`script` feeds
    items 1..20), so splitting one call across workers is a deadlock, not a speed-up."""
    _order("whole", tasks=5)
    batch = dispatch.claim(worker_id="w1", want=10, **LEASE)
    assert len(batch) == 1
    assert len(batch[0]["tasks"]) == 5


def test_nine_concurrent_claimers_never_hand_out_the_same_order_twice(clean):
    for i in range(9):
        _order(f"race-{i}")
    seen: list[int] = []
    lock = threading.Lock()

    def run(n: int):
        got = dispatch.claim(worker_id=f"w{n}", want=3, **LEASE)
        with lock:
            # Only this test's rows: claim() takes whatever is ready in the database, and this
            # is the live one (there is no separate test cluster), so an order left ready by
            # verify_e2e.sh or by a shadow-compare fixture would otherwise be counted here and
            # fail an assertion about nine.
            seen.extend(b["order"]["order_id"] for b in got
                        if b["order"]["call_uuid"].startswith("pytest-"))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seen) == len(set(seen)), "an order was handed to two workers"
    assert len(seen) == 9, "nine ready orders, nine hand-outs"


def test_oldest_first_not_alphabetical(clean):
    """The whole point of the move: scandir's order is alphabetical by uuid, so a call that
    keeps failing holds its early position forever and starves everything after it."""
    _order("zzz-first")
    time.sleep(0.01)
    _order("aaa-second")
    order = [b["order"]["call_uuid"] for b in dispatch.claim(worker_id="w1", want=5, **LEASE)]
    assert order == ["pytest-zzz-first", "pytest-aaa-second"]


def test_a_parked_order_is_not_handed_out_until_its_source_is_enabled(clean):
    _order("parked", company=OFF_COMPANY)
    assert _state("parked") == "parked"
    assert dispatch.claim(worker_id="w1", want=5, **LEASE) == []
    try:
        assert dispatch.release_parked(OFF_COMPANY) == 1
        assert _state("parked") == "ready"
    finally:
        conn = db.connect()
        try:
            conn.execute("UPDATE sources SET enabled = FALSE WHERE company_id = %s",
                         (OFF_COMPANY,))
            conn.commit()
        finally:
            db.release(conn)


def test_include_and_exclude_are_applied_here_not_after_the_hand_out(clean):
    _order("filtered")
    assert dispatch.claim(worker_id="w1", want=5, include=["other"], **LEASE) == []
    assert dispatch.claim(worker_id="w1", want=5, exclude=[TEST_COMPANY], **LEASE) == []
    assert len(dispatch.claim(worker_id="w1", want=5, include=[TEST_COMPANY], **LEASE)) == 1


def test_too_little_speech_is_never_handed_out(clean):
    _order("mute", speech_chars=10)
    assert dispatch.claim(worker_id="w1", want=5, **LEASE) == []


def test_retry_after_holds_an_order_back(clean):
    _order("backoff")
    batch = dispatch.claim(worker_id="w1", want=1, **LEASE)
    dispatch.mark_unavailable(str(batch[0]["order"]["lease_token"]), 600.0, "vLLM restarting")
    assert _state("backoff") == "ready"
    assert dispatch.claim(worker_id="w1", want=5, **LEASE) == []


def test_unavailable_refunds_the_attempt_but_a_failure_spends_it(clean):
    """asrhub's 2026-08-27 lesson: an engine outage costs time, not calls."""
    _order("refund")
    batch = dispatch.claim(worker_id="w1", want=1, **LEASE)
    assert batch[0]["order"]["attempts"] == 1
    dispatch.mark_unavailable(str(batch[0]["order"]["lease_token"]), 0.0, "down")
    again = dispatch.claim(worker_id="w1", want=1, **LEASE)
    assert again[0]["order"]["attempts"] == 1, "the refunded attempt was charged twice"
    dispatch.release_lease(str(again[0]["order"]["lease_token"]))
    third = dispatch.claim(worker_id="w1", want=1, **LEASE)
    assert third[0]["order"]["attempts"] == 1


def test_an_order_that_burns_its_attempts_gives_up_instead_of_looping(clean):
    _order("giveup")
    # A fresh worker each round: prior_holders is a *preference*, so the same worker would be
    # held off by grace_s and the order would idle rather than burn its attempts.
    for attempt in range(6):
        batch = dispatch.claim(worker_id=f"w{attempt}", want=1, **LEASE)
        if not batch:
            break
        _expire("giveup")
        dispatch.reap_expired()
    assert _state("giveup") == "failed"
    assert dispatch.claim(worker_id="w1", want=5, **LEASE) == []


def test_prior_holders_is_a_preference_a_lone_worker_still_overcomes(clean):
    _order("prior")
    batch = dispatch.claim(worker_id="w1", want=1, **LEASE)
    assert batch
    _expire("prior")
    dispatch.reap_expired()
    # w2 is preferred, but with nobody else asking w1 must still get it back rather than the
    # call sitting unanswered forever.
    assert dispatch.claim(worker_id="w1", want=1, grace_s=0.0,
                          lease_ttl_s=1800.0, min_speech_chars=48)


# --------------------------------------------------------------------------------------
# Leases
# --------------------------------------------------------------------------------------

def test_expiry_mints_a_new_token_and_the_stale_holder_gets_409(clean):
    _order("stale", tasks=2)
    first = dispatch.claim(worker_id="w1", want=1, **LEASE)[0]
    stale_token = str(first["order"]["lease_token"])
    _expire("stale")
    assert dispatch.reap_expired() == 1
    second = dispatch.claim(worker_id="w2", want=1, **LEASE)[0]
    assert str(second["order"]["lease_token"]) != stale_token

    with pytest.raises(dispatch.StaleReport):
        dispatch.report_result(call_uuid="pytest-stale", task_name="1", status="done",
                               result_bytes=12, lease_token=stale_token)
    # The new holder is unaffected.
    dispatch.report_result(call_uuid="pytest-stale", task_name="1", status="done",
                           result_bytes=12,
                           lease_token=str(second["order"]["lease_token"]))
    assert _task_states("stale")["1"] == "done"


def test_a_late_answer_is_kept_when_nobody_else_answered(clean):
    """A slow-but-correct answer is worth more than a lost call; only an answer to an
    already-answered task is refused."""
    _order("late", tasks=2)
    dispatch.claim(worker_id="w1", want=1, **LEASE)
    _expire("late")
    dispatch.reap_expired()
    out = dispatch.report_result(call_uuid="pytest-late", task_name="1", status="done",
                                 result_bytes=5)
    assert out["task_state"] == "done"


def test_a_second_answer_to_the_same_task_is_stale(clean):
    _order("twice", tasks=2)
    token = str(dispatch.claim(worker_id="w1", want=1, **LEASE)[0]["order"]["lease_token"])
    dispatch.report_result(call_uuid="pytest-twice", task_name="1", status="done",
                           result_bytes=1, lease_token=token)
    with pytest.raises(dispatch.StaleReport):
        dispatch.report_result(call_uuid="pytest-twice", task_name="1", status="done",
                               result_bytes=1, lease_token=token)


def test_a_polite_return_does_not_blame_the_worker(clean):
    _order("polite")
    token = str(dispatch.claim(worker_id="w1", want=1, **LEASE)[0]["order"]["lease_token"])
    row = dispatch.release_lease(token)
    assert row["state"] == "ready"
    assert row["prior_holders"] == []


def test_boot_requeues_orphaned_leases(clean):
    _order("orphan")
    dispatch.claim(worker_id="w1", want=1, **LEASE)
    assert dispatch.requeue_orphaned_leases() == 1
    assert _state("orphan") == "ready"
    assert set(_task_states("orphan").values()) == {"pending"}


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

def test_a_partial_report_leaves_only_the_unreported_tasks_pending(clean):
    _order("partial", tasks=4)
    token = str(dispatch.claim(worker_id="w1", want=1, **LEASE)[0]["order"]["lease_token"])
    for name in ("1", "3"):
        dispatch.report_result(call_uuid="pytest-partial", task_name=name, status="done",
                               result_bytes=7, lease_token=token)
    _expire("partial")
    dispatch.reap_expired()
    batch = dispatch.claim(worker_id="w2", want=1, **LEASE)[0]
    assert sorted(t["task_name"] for t in batch["tasks"]) == ["2", "4"]


def test_an_empty_answer_is_an_answer_not_a_missing_file(clean):
    """The root cause of the 2026-09-04 write_task_result() patch: the PHP can only ask
    whether a file exists, so a legitimate "not applicable" and a truncated write look the
    same. Here zero bytes is data."""
    _order("empty", tasks=1)
    token = str(dispatch.claim(worker_id="w1", want=1, **LEASE)[0]["order"]["lease_token"])
    out = dispatch.report_result(call_uuid="pytest-empty", task_name="1", status="done",
                                 result_bytes=0, lease_token=token)
    assert out["task_state"] == "done"
    assert out["order_state"] == "done"


def test_a_problem_closes_the_task_and_the_order(clean):
    _order("problem", tasks=1)
    token = str(dispatch.claim(worker_id="w1", want=1, **LEASE)[0]["order"]["lease_token"])
    out = dispatch.report_result(call_uuid="pytest-problem", task_name="1", status="problem",
                                 result_bytes=len("<problem>\n"), lease_token=token)
    assert out["task_state"] == "problem"
    assert _state("problem") == "done"


def test_failed_returns_the_task_to_the_queue(clean):
    _order("failed", tasks=2)
    dispatch.claim(worker_id="w1", want=1, **LEASE)
    dispatch.fail_task(call_uuid="pytest-failed", task_name="1", error="model 500")
    assert _task_states("failed")["1"] == "pending"


def test_reordering_supersedes_and_starts_every_task_over(clean):
    """order_llm_task() unlinks every `*.task-*` for the call (:1310-1330)."""
    _order("resend", tasks=2)
    token = str(dispatch.claim(worker_id="w1", want=1, **LEASE)[0]["order"]["lease_token"])
    dispatch.report_result(call_uuid="pytest-resend", task_name="1", status="done",
                           result_bytes=3, lease_token=token)
    _order("resend", tasks=2)
    assert _state("resend") == "ready"
    assert set(_task_states("resend").values()) == {"pending"}
    with pytest.raises(dispatch.StaleReport):
        dispatch.report_result(call_uuid="pytest-resend", task_name="2", status="done",
                               result_bytes=3, lease_token=token)


def test_a_reorder_inherits_the_pass_through_fields_it_omits(clean):
    """status.yaml is merged over, not replaced (:1280-1296) - dropping `sequrity_key` would
    leave the worker with no credential for the customer's backend."""
    _order("inherit")
    again = dispatch.create_order(
        call_uuid="pytest-inherit", company_id=TEST_COMPANY, taskset="1",
        taskset_sha256="deadbeef", tasks=[("1", {"name": "1"})], speech_chars=500)
    assert again["sequrity_key"] == "CUSTOMER-TOKEN"
    assert again["url"] == "https://example.invalid/hook"


def test_only_one_live_order_exists_per_call(clean):
    _order("single")
    _order("single")
    conn = db.connect()
    try:
        n = conn.execute(
            "SELECT count(*) AS n FROM orders WHERE call_uuid = %s "
            " AND state IN ('parked','ready','leased')", ("pytest-single",)).fetchone()["n"]
    finally:
        db.release(conn)
    assert n == 1


# --------------------------------------------------------------------------------------
# Settling
# --------------------------------------------------------------------------------------

def test_the_settler_waits_for_the_transcript_to_stop_changing(clean):
    _order("settle", speech_chars=10)
    assert dispatch.settle_short(48, 300.0) == 0, "settled before the 300s window"
    conn = db.connect()
    try:
        conn.execute("UPDATE orders SET transcript_at = now() - interval '10 minutes' "
                     " WHERE call_uuid = %s", ("pytest-settle",))
        conn.commit()
    finally:
        db.release(conn)
    assert dispatch.settle_short(48, 300.0) == 1
    assert _state("settle") == "settled_empty"
    assert set(_task_states("settle").values()) == {"settled_empty"}


def test_the_settler_never_touches_a_leased_order(clean):
    """mark_tasks_empty() skips a task with a running marker (:900-918); somebody may already
    be answering it."""
    _order("leased-short", speech_chars=10)
    conn = db.connect()
    try:
        conn.execute("UPDATE orders SET state = 'leased', speech_chars = 10, "
                     "       transcript_at = now() - interval '10 minutes' "
                     " WHERE call_uuid = %s", ("pytest-leased-short",))
        conn.commit()
    finally:
        db.release(conn)
    assert dispatch.settle_short(48, 300.0) == 0
    assert _state("leased-short") == "leased"
