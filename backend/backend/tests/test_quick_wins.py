import asyncio
import threading
import time

import pytest

from app.config import get_settings
from app.core.errors import ServiceUnavailableError
from app.services import webhook
from app.services.storage import get_store
from app.voice import stt
from tests.fakes import FakeRouter
from tests.test_api import HEADERS, client, create_interview  # noqa: F401  (client is a fixture)

ANSWER = {
    "question_index": 0,
    "transcript": "Situation: slow API. Task: fix it. I added caching. Result: p95 down 60 percent.",
    "duration_seconds": 30,
}


def _answer_one(client):
    interview_id = create_interview(client)["interview"]["id"]
    response = client.post(f"/api/v1/interviews/{interview_id}/answers", json=ANSWER, headers=HEADERS)
    assert response.status_code == 200, response.text
    return interview_id


def test_finish_does_not_wait_for_the_webhook(client, monkeypatch):
    sent = []

    async def never_called(*args, **kwargs):
        sent.append(args)
        await asyncio.sleep(30)  # a dead receiver

    monkeypatch.setattr(webhook, "send_once", never_called)
    monkeypatch.setattr(webhook, "deliver", never_called)
    monkeypatch.setattr(get_settings(), "webhook_url", "https://hooks.example.com/interviews")
    interview_id = _answer_one(client)

    started = time.monotonic()
    response = client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS)
    elapsed = time.monotonic() - started

    assert response.status_code == 200, response.text
    assert response.json()["report"]["overall_score"] == 76
    assert elapsed < 5
    assert sent == []  # nothing was sent inside the request; the outbox loop sends it later
    queued = get_store().db.query_all(
        "SELECT event FROM webhook_outbox WHERE interview_id = ? ORDER BY id", (interview_id,)
    )
    assert [row["event"] for row in queued] == ["interview.created", "interview.completed"]


def test_tips_and_narrative_run_in_parallel(client):
    in_flight = {"now": 0, "max": 0}
    original = FakeRouter.complete_json

    async def tracked(self, task, messages, **kwargs):
        if str(task) not in {"tips_generation", "report_narrative"}:
            return await original(self, task, messages, **kwargs)
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        try:
            await asyncio.sleep(0.2)
            return await original(self, task, messages, **kwargs)
        finally:
            in_flight["now"] -= 1

    client.fake_router.complete_json = tracked.__get__(client.fake_router)
    interview_id = _answer_one(client)
    response = client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS)

    assert response.status_code == 200, response.text
    report = response.json()["report"]
    assert in_flight["max"] == 2
    assert report["improvement_plan"]["quick_wins"]
    assert report["narrative"]["recommendation"] == "hire"
    assert set(report["routing_trace"]) >= {"final_scoring", "tips", "narrative"}


async def test_transcriptions_are_capped_per_machine(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stt_max_concurrent", 2)
    monkeypatch.setattr(settings, "stt_queue_timeout_seconds", 5.0)
    stt._slots = None
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def fake_transcribe(_path):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.1)
        with lock:
            state["now"] -= 1
        return {"text": "ok"}

    monkeypatch.setattr(stt, "_transcribe_sync", fake_transcribe)
    results = await asyncio.gather(*(stt._transcribe_limited("x.wav") for _ in range(6)))

    assert len(results) == 6
    assert state["max"] == 2


async def test_busy_machine_answers_503_with_retry_after(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "stt_max_concurrent", 1)
    monkeypatch.setattr(settings, "stt_queue_timeout_seconds", 0.1)
    stt._slots = None
    release = threading.Event()

    def blocking(_path):
        release.wait(5)
        return {"text": "ok"}

    monkeypatch.setattr(stt, "_transcribe_sync", blocking)
    first = asyncio.create_task(stt._transcribe_limited("a.wav"))
    await asyncio.sleep(0.05)
    with pytest.raises(ServiceUnavailableError) as busy:
        await stt._transcribe_limited("b.wav")
    assert busy.value.status_code == 503
    assert busy.value.headers == {"Retry-After": "5"}

    release.set()
    assert (await first)["text"] == "ok"
    # the slot is released after the wait, so a later request goes through
    assert (await stt._transcribe_limited("c.wav"))["text"] == "ok"


async def test_preload_skips_quietly_when_voice_is_not_installed(monkeypatch):
    from app.core.errors import SpeechUnavailableError

    async def missing():
        raise SpeechUnavailableError("faster-whisper is not installed")

    monkeypatch.setattr(stt, "_get_model", missing)
    await stt.preload()  # must not raise


async def test_startup_does_not_wait_for_the_model_but_ready_does(client, monkeypatch):
    release = asyncio.Event()

    async def slow_model():
        await release.wait()

    monkeypatch.setattr(stt, "_get_model", slow_model)
    task = stt.begin_preload()
    await asyncio.sleep(0)
    assert stt.preload_pending() is True
    assert client.get("/api/v1/health").status_code == 200  # alive straight away
    not_ready = client.get("/api/v1/ready")
    assert not_ready.status_code == 503 and "loading" in not_ready.json()["error"]["message"]

    release.set()
    await task
    assert stt.preload_pending() is False
    assert client.get("/api/v1/ready").status_code == 200


async def test_preload_failure_does_not_leave_the_service_unready(monkeypatch):
    async def broken():
        raise RuntimeError("corrupt model file")

    monkeypatch.setattr(stt, "_get_model", broken)
    await stt.begin_preload()
    assert stt.preload_pending() is False
