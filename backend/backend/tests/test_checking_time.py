"""The time the service spends checking an answer (speech-to-text, AI analysis) is not taken from the student."""

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.services import interview_service as interview_service_module
from app.llm.tasks import LLMTask
from app.services import storage as storage_module
from app.voice import stt
from tests.helpers import wait_for_analyses
from tests.test_pause_skip import _answer, _post, _status
from tests.test_plan_limits import ANSWER, HEADERS, client, create  # noqa: F401 (client is a fixture)


class Clock:
    """The service's clock, moved forward by hand to stand in for slow work."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset

    def wait(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


@pytest.fixture()
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(interview_service_module, "utcnow", fake)
    return fake


@pytest.fixture()
def slow_calls(monkeypatch, clock):
    """The AI calls an answer WAITS for (the follow-up decision) take `seconds` (set it on the returned holder).

    The per-answer analysis no longer runs on the student's wait, so it is not slowed here: `blocked` holds it back
    without moving the clock (set `release` to let it go), the way real background work does not move the clock.
    """
    holder = {"seconds": 0.0, "tasks": [], "blocked": False, "release": threading.Event()}
    original = interview_service_module.InterviewService._try_llm_json

    async def slow(self, task, *args, **kwargs):
        if task == LLMTask.ANSWER_ANALYSIS:
            while holder["blocked"] and not holder["release"].is_set():
                await asyncio.sleep(0.01)
        else:
            clock.wait(holder["seconds"])
        holder["tasks"].append(str(task))
        return await original(self, task, *args, **kwargs)

    monkeypatch.setattr(interview_service_module.InterviewService, "_try_llm_json", slow)
    return holder


def _deadline(client, interview_id):
    return datetime.fromisoformat(_status(client, interview_id)["deadline_at"])


def _send_audio(client, interview_id, index=0, question_id=None):
    return client.post(
        f"/api/v1/interviews/{interview_id}/answers/audio",
        data={"question_index": str(index), **({"question_id": question_id} if question_id else {})},
        files={"audio": ("a.webm", b"fake-audio", "audio/webm")},
        headers=HEADERS,
    )


def _slow_stt(monkeypatch, clock, seconds):
    async def transcribe(content, filename):
        clock.wait(seconds)
        return {"text": ANSWER, "language": "en", "duration_seconds": 40.0, "processing_ms": 1}

    monkeypatch.setattr(stt, "transcribe_bytes", transcribe)


def test_only_the_follow_up_decision_is_credited_for_a_typed_answer(client, slow_calls):
    interview_id = create(client)["interview"]["id"]
    before = _deadline(client, interview_id)
    slow_calls["seconds"] = 20

    assert _answer(client, interview_id, 0).status_code == 200
    wait_for_analyses(client, interview_id)  # the analysis ran too (in the background), but nobody waited for it

    assert slow_calls["tasks"].count("followup_generation") == 1
    moved = _deadline(client, interview_id) - before
    assert timedelta(seconds=20) <= moved <= timedelta(seconds=23)  # one 20 s call was waited for, no more
    config = storage_module.get_store().get_interview(interview_id)["config"]
    assert 20 <= config["checking_credit_seconds"] <= 23


def test_a_slow_analysis_does_not_delay_the_answer_or_the_deadline(client, slow_calls):
    interview_id = create(client)["interview"]["id"]
    before = _deadline(client, interview_id)
    slow_calls["blocked"] = True  # the analysis hangs; the answer must come back anyway

    response = _answer(client, interview_id, 0)
    assert response.status_code == 200
    body = response.json()
    assert body["analysis"] is None and body["analysis_status"] == "pending"
    assert body["followup"]  # the fast follow-up decision did not wait for it either
    assert _deadline(client, interview_id) - before <= timedelta(seconds=3)  # no analysis time was credited
    assert wait_for_analyses(client, interview_id, timeout=0.3)[0]["analysis"] is None  # still running

    slow_calls["release"].set()
    assert wait_for_analyses(client, interview_id)[0]["analysis"]["scores"]["overall"] == 76


def test_transcribing_and_deciding_the_follow_up_of_a_spoken_answer_is_given_back(
    client, monkeypatch, clock, slow_calls
):
    interview_id = create(client)["interview"]["id"]
    before = _deadline(client, interview_id)
    _slow_stt(monkeypatch, clock, 15)
    slow_calls["seconds"] = 10

    response = _send_audio(client, interview_id)
    assert response.status_code == 200, response.text
    assert response.json()["analysis_status"] == "pending"

    moved = _deadline(client, interview_id) - before
    assert timedelta(seconds=25) <= moved <= timedelta(seconds=28)  # 15 s speech-to-text + one 10 s call


def test_a_spoken_answer_sent_in_time_is_kept_when_the_deadline_passes_during_transcription(
    client, monkeypatch, clock
):
    interview_id = create(client)["interview"]["id"]
    store = storage_module.get_store()
    config = store.get_interview(interview_id)["config"]
    config["deadline_at"] = (clock() + timedelta(seconds=5)).isoformat()
    store.update_interview(interview_id, config=config)
    _slow_stt(monkeypatch, clock, 30)  # the deadline passes while the answer is transcribed

    response = _send_audio(client, interview_id)
    assert response.status_code == 200, response.text
    assert _status(client, interview_id)["seconds_remaining"] > 0  # the 30 s were given back


def test_a_duplicate_audio_upload_is_refused_before_it_is_transcribed(client, monkeypatch):
    interview_id = create(client)["interview"]["id"]
    calls = []

    async def transcribe(content, filename):
        calls.append(filename)
        return {"text": ANSWER, "language": "en", "duration_seconds": 40.0, "processing_ms": 1}

    monkeypatch.setattr(stt, "transcribe_bytes", transcribe)

    assert _send_audio(client, interview_id).status_code == 200
    assert calls == ["a.webm"]

    again = _send_audio(client, interview_id)  # for example the browser's retry after a timeout
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "conflict"
    assert calls == ["a.webm"]  # the second clip never reached speech-to-text

    out_of_range = _send_audio(client, interview_id, index=99)
    assert out_of_range.status_code == 422
    assert calls == ["a.webm"]


def test_an_answer_that_arrives_after_the_deadline_is_still_refused(client, clock):
    interview_id = create(client)["interview"]["id"]
    store = storage_module.get_store()
    config = store.get_interview(interview_id)["config"]
    config["deadline_at"] = (clock() - timedelta(seconds=1)).isoformat()
    store.update_interview(interview_id, config=config)

    refused = _answer(client, interview_id, 0)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "time_limit_reached"


def test_a_skip_gives_no_extra_time(client, clock):
    interview_id = create(client)["interview"]["id"]
    before = _deadline(client, interview_id)
    assert _post(client, interview_id, "skip", json={"question_index": 0}).status_code == 200
    assert _deadline(client, interview_id) == before


def test_an_audio_answer_with_a_question_id_lands_on_that_question_even_when_the_index_is_stale(client, monkeypatch):
    interview_id = create(client)["interview"]["id"]

    async def transcribe(content, filename):
        return {"text": ANSWER, "language": "en", "duration_seconds": 40.0, "processing_ms": 1}

    monkeypatch.setattr(stt, "transcribe_bytes", transcribe)
    first = _send_audio(client, interview_id).json()
    assert first["followup_question"]  # inserted at index 1: the base question "q1" is now number 2

    # A client that already played "q1" early still says index 1 (now the follow-up); the id sets it right.
    second = _send_audio(client, interview_id, index=1, question_id="q1")
    assert second.status_code == 200, second.text
    assert second.json()["question_id"] == "q1" and second.json()["question_index"] == 2

    # Without the id the index works exactly as before (here: the follow-up is at index 1).
    third = _send_audio(client, interview_id, index=1)
    assert third.status_code == 200 and third.json()["question_id"] == first["followup_question"]["id"]
    assert _send_audio(client, interview_id, index=1, question_id="q1").status_code == 409  # duplicate by id
