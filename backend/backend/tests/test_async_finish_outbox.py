import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.services import interview_service as interview_service_module
from app.services import webhook
from app.services.storage import get_store
from tests.fakes import FakeRouter
from tests.test_api import HEADERS, client, create_interview  # noqa: F401  (client is a fixture)

URL = "https://hooks.example.com/interviews"
ANSWER = {
    "question_index": 0,
    "transcript": "Situation: slow API. Task: fix it. I added caching. Result: p95 down 60 percent.",
    "duration_seconds": 30,
}


# ---- webhook outbox ---------------------------------------------------------------------------------------------


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = f"status {status_code}"


class _Receiver:
    """Stands in for the main app's webhook endpoint."""

    def __init__(self) -> None:
        self.statuses: list[int] = [200]
        self.posts: list[dict] = []
        self.delay = 0.0

    def client_factory(self):
        receiver = self

        class _Client:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, content, headers):
                if receiver.delay:
                    await asyncio.sleep(receiver.delay)
                receiver.posts.append({"url": url, "headers": headers})
                status = receiver.statuses.pop(0) if len(receiver.statuses) > 1 else receiver.statuses[0]
                return _Response(status)

        return _Client


@pytest.fixture()
def receiver(monkeypatch):
    fake = _Receiver()
    monkeypatch.setattr(webhook.httpx, "AsyncClient", fake.client_factory())
    monkeypatch.setattr(webhook, "validate_callback_url", lambda url: None)
    monkeypatch.setattr(get_settings(), "webhook_secret", "s" * 40)
    get_store().db.execute("DELETE FROM webhook_outbox")
    return fake


def _rows():
    return get_store().db.query_all("SELECT * FROM webhook_outbox ORDER BY id")


def _make_due(row_id: int) -> None:
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    get_store().db.execute("UPDATE webhook_outbox SET next_attempt_at = ? WHERE id = ?", (past, row_id))


async def test_enqueue_without_a_target_is_a_noop(receiver, monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_url", "")
    assert await webhook.enqueue(None, "interview.completed", {"interview_id": "x"}) is False
    assert _rows() == []


async def test_queued_webhook_is_delivered_signed(receiver):
    assert await webhook.enqueue(URL, "interview.completed", {"interview_id": "i1", "overall_score": 80}) is True
    assert await webhook.drain_once() == 1

    row = _rows()[0]
    assert row["status"] == "delivered" and row["attempts"] == 1
    sent = receiver.posts[0]
    assert sent["url"] == URL
    assert sent["headers"]["X-Interview-Delivery"] == f"ob-{row['id']}"
    assert "X-Interview-Signature-V2" in sent["headers"]
    assert await webhook.drain_once() == 0  # delivered rows are not sent again


async def test_receiver_outage_is_retried_later_with_the_same_delivery_id(receiver):
    receiver.statuses = [503, 200]
    await webhook.enqueue(URL, "interview.completed", {"interview_id": "i2"})
    await webhook.drain_once()

    row = _rows()[0]
    assert row["status"] == "pending" and row["attempts"] == 1
    assert "503" in row["last_error"]
    assert row["next_attempt_at"] > datetime.now(timezone.utc).isoformat()  # backed off, not retried in a loop
    assert await webhook.drain_once() == 0

    _make_due(row["id"])
    await webhook.drain_once()
    row = _rows()[0]
    assert row["status"] == "delivered" and row["attempts"] == 2
    assert {post["headers"]["X-Interview-Delivery"] for post in receiver.posts} == {f"ob-{row['id']}"}


async def test_rejection_by_the_receiver_is_not_retried(receiver):
    receiver.statuses = [400]
    await webhook.enqueue(URL, "interview.completed", {"interview_id": "i3"})
    await webhook.drain_once()
    row = _rows()[0]
    assert row["status"] == "dead" and row["attempts"] == 1


async def test_gives_up_after_the_maximum_attempts(receiver, monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_outbox_max_attempts", 3)
    receiver.statuses = [500]
    await webhook.enqueue(URL, "interview.completed", {"interview_id": "i4"})
    for _ in range(3):
        _make_due(_rows()[0]["id"])
        await webhook.drain_once()
    row = _rows()[0]
    assert row["status"] == "dead" and row["attempts"] == 3
    assert len(receiver.posts) == 3


async def test_missing_secret_keeps_the_webhook_queued(receiver, monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_secret", "")
    monkeypatch.setattr(get_settings(), "environment", "production")
    await webhook.enqueue(URL, "interview.completed", {"interview_id": "i5"})
    await webhook.drain_once()
    row = _rows()[0]
    assert row["status"] == "pending" and "WEBHOOK_SECRET" in row["last_error"]
    assert receiver.posts == []


async def test_two_machines_do_not_send_the_same_webhook_twice(receiver):
    receiver.delay = 0.2
    for index in range(3):
        await webhook.enqueue(URL, "interview.completed", {"interview_id": f"m{index}"})
    await asyncio.gather(webhook.drain_once(), webhook.drain_once())
    assert len(receiver.posts) == 3
    assert [row["status"] for row in _rows()] == ["delivered"] * 3


async def test_pruning_removes_only_old_finished_rows(receiver):
    await webhook.enqueue(URL, "a", {"interview_id": "p1"})
    await webhook.enqueue(URL, "b", {"interview_id": "p2"})
    await webhook.drain_once()
    receiver.statuses = [500]
    await webhook.enqueue(URL, "c", {"interview_id": "p3"})
    store = get_store()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert store.outbox_prune(future) == 2
    assert [row["event"] for row in _rows()] == ["c"]  # still pending


def test_erasing_an_interview_erases_its_queued_webhooks(client, receiver, monkeypatch):
    monkeypatch.setattr(interview_service_module, "validate_callback_url", lambda url: None)
    interview_id = create_interview(client, callback_url=URL)["interview"]["id"]
    assert any(row["interview_id"] == interview_id for row in _rows())
    assert client.delete(f"/api/v1/interviews/{interview_id}", headers=HEADERS).status_code == 204
    assert not any(row["interview_id"] == interview_id for row in _rows())


# ---- asynchronous report generation -----------------------------------------------------------------------------


@pytest.fixture()
def async_client(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "finish_async", True)
    return client


def _answered(client) -> str:
    interview_id = create_interview(client)["interview"]["id"]
    response = client.post(f"/api/v1/interviews/{interview_id}/answers", json=ANSWER, headers=HEADERS)
    assert response.status_code == 200, response.text
    return interview_id


def _wait_for(client, interview_id: str, status: str, seconds: float = 10.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        body = client.get(f"/api/v1/interviews/{interview_id}/report", headers=HEADERS).json()
        if body["status"] == status:
            return body
        time.sleep(0.05)
    raise AssertionError(f"interview never reached {status}: {body}")


def _slow_final_scoring(client, seconds: float, calls: list):
    original = FakeRouter.complete_json

    async def slow(self, task, messages, **kwargs):
        if str(task) == "final_scoring":
            calls.append(task)
            await asyncio.sleep(seconds)
        return await original(self, task, messages, **kwargs)

    client.fake_router.complete_json = slow.__get__(client.fake_router)


def test_async_finish_answers_202_then_the_report_appears(async_client):
    calls: list = []
    _slow_final_scoring(async_client, 0.5, calls)
    interview_id = _answered(async_client)

    started = time.monotonic()
    response = async_client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS)
    assert response.status_code == 202, response.text
    assert time.monotonic() - started < 0.4  # did not wait for the AI calls
    assert response.json() == {
        "interview_id": interview_id, "status": "processing", "report": None, "created_at": None, "error": None,
    }
    status = async_client.get(f"/api/v1/interviews/{interview_id}/status", headers=HEADERS).json()
    assert status["status"] == "processing"

    done = _wait_for(async_client, interview_id, "completed")
    assert done["report"]["overall_score"] == 76
    # a finish after completion returns the saved report (200) without generating again
    again = async_client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS)
    assert again.status_code == 200 and again.json()["report"]["overall_score"] == 76
    assert len(calls) == 1


def test_a_second_finish_while_processing_does_not_start_a_second_build(async_client):
    calls: list = []
    _slow_final_scoring(async_client, 0.6, calls)
    interview_id = _answered(async_client)

    responses = [async_client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS) for _ in range(3)]
    assert [r.status_code for r in responses] == [202, 202, 202]
    _wait_for(async_client, interview_id, "completed")
    assert len(calls) == 1  # one scoring run, not three


def test_a_failed_build_is_reported_and_can_be_retried(async_client, monkeypatch):
    interview_id = _answered(async_client)
    service = interview_service_module.get_interview_service()
    real_build = service._build_report

    async def broken(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(service, "_build_report", broken)
    monkeypatch.setattr(get_settings(), "webhook_url", URL)
    get_store().db.execute("DELETE FROM webhook_outbox")
    assert async_client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS).status_code == 202

    failed = _wait_for(async_client, interview_id, "failed")
    assert failed["report"] is None and "database went away" in failed["error"]
    events = async_client.get(f"/api/v1/interviews/{interview_id}/events", headers=HEADERS).json()["items"]
    assert "interview.failed" in [item["event_type"] for item in events]
    assert [row["event"] for row in _rows()] == ["interview.failed"]

    monkeypatch.setattr(service, "_build_report", real_build)
    assert async_client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS).status_code == 202
    assert _wait_for(async_client, interview_id, "completed")["report"]["overall_score"] == 76


def test_sync_mode_failure_marks_the_interview_failed_instead_of_leaving_it_processing(client, monkeypatch):
    interview_id = _answered(client)
    service = interview_service_module.get_interview_service()

    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_build_report", broken)
    with pytest.raises(RuntimeError):  # TestClient re-raises; a real server answers 500
        client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS)
    body = client.get(f"/api/v1/interviews/{interview_id}/report", headers=HEADERS).json()
    assert body["status"] == "failed" and body["error"] == "boom"


def _age(interview_id: str, status: str, seconds: int) -> None:
    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    get_store().db.execute("UPDATE interviews SET status = ?, updated_at = ? WHERE id = ?", (status, old, interview_id))


def test_sweeper_finishes_a_build_that_a_dead_machine_left_behind(async_client):
    interview_id = _answered(async_client)
    _age(interview_id, "processing", 3600)  # its machine disappeared an hour ago
    service = interview_service_module.get_interview_service()

    assert async_client.portal.call(service.sweep_stalled_builds) == 1
    assert _wait_for(async_client, interview_id, "completed")["report"]["overall_score"] == 76
    assert async_client.portal.call(service.sweep_stalled_builds) == 0


def test_sweeper_leaves_a_recent_build_alone(async_client):
    interview_id = _answered(async_client)
    _age(interview_id, "processing", 5)
    service = interview_service_module.get_interview_service()
    assert async_client.portal.call(service.sweep_stalled_builds) == 0
    body = async_client.get(f"/api/v1/interviews/{interview_id}/report", headers=HEADERS).json()
    assert body["status"] == "processing"


def test_only_one_of_two_sweepers_takes_a_stalled_build(async_client):
    interview_id = _answered(async_client)
    _age(interview_id, "processing", 3600)
    store = get_store()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    assert [store.claim_stale_finish(interview_id, cutoff), store.claim_stale_finish(interview_id, cutoff)] == [
        True, False,
    ]


def test_claiming_finish_is_exclusive(client):
    interview_id = _answered(client)
    store = get_store()
    assert store.claim_finish(interview_id) is True
    assert store.claim_finish(interview_id) is False
