"""The time the service spends checking an answer (speech-to-text, AI analysis) is not taken from the student."""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import interview_service as interview_service_module
from app.services import storage as storage_module
from app.voice import stt
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
def slow_analysis(monkeypatch, clock):
    """Every AI call during an answer takes `seconds` (set it on the returned holder)."""
    holder = {"seconds": 0.0, "calls": 0}
    original = interview_service_module.InterviewService._try_llm_json

    async def slow(self, *args, **kwargs):
        clock.wait(holder["seconds"])
        holder["calls"] += 1
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(interview_service_module.InterviewService, "_try_llm_json", slow)
    return holder


def _deadline(client, interview_id):
    return datetime.fromisoformat(_status(client, interview_id)["deadline_at"])


def _send_audio(client, interview_id, index=0):
    return client.post(
        f"/api/v1/interviews/{interview_id}/answers/audio",
        data={"question_index": str(index)},
        files={"audio": ("a.webm", b"fake-audio", "audio/webm")},
        headers=HEADERS,
    )


def _slow_stt(monkeypatch, clock, seconds):
    async def transcribe(content, filename):
        clock.wait(seconds)
        return {"text": ANSWER, "language": "en", "duration_seconds": 40.0, "processing_ms": 1}

    monkeypatch.setattr(stt, "transcribe_bytes", transcribe)


def test_the_time_spent_analysing_a_typed_answer_is_given_back(client, slow_analysis):
    interview_id = create(client)["interview"]["id"]
    before = _deadline(client, interview_id)
    slow_analysis.update(seconds=20, calls=0)

    assert _answer(client, interview_id, 0).status_code == 200

    waited = timedelta(seconds=20 * slow_analysis["calls"])  # the analysis, plus a follow-up when one is written
    moved = _deadline(client, interview_id) - before
    assert waited <= moved <= waited + timedelta(seconds=3)
    config = storage_module.get_store().get_interview(interview_id)["config"]
    assert config["checking_credit_seconds"] >= waited.total_seconds()


def test_transcribing_and_analysing_a_spoken_answer_is_given_back(client, monkeypatch, clock, slow_analysis):
    interview_id = create(client)["interview"]["id"]
    before = _deadline(client, interview_id)
    _slow_stt(monkeypatch, clock, 15)
    slow_analysis.update(seconds=10, calls=0)

    response = _send_audio(client, interview_id)
    assert response.status_code == 200, response.text

    waited = timedelta(seconds=15 + 10 * slow_analysis["calls"])
    moved = _deadline(client, interview_id) - before
    assert waited <= moved <= waited + timedelta(seconds=3)


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
