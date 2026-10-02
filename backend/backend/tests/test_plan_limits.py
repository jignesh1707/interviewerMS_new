"""Per-student quota, interview length, server-side time limit and per-answer caps (PLANS_ENABLED)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core import plans as plans_module
from app.services import interview_service as interview_service_module
from app.services import storage as storage_module
from app.voice import stt
from tests.fakes import FakeRouter

HEADERS = {"X-API-Key": "test-key"}

PLANS_YAML = """
default_plan: standard
grace_seconds: 60
profiles:
  15: {question_count: 7, max_followups: 2}
  20: {question_count: 9, max_followups: 3}
plans:
  standard:
    included_minutes: 30
    period: monthly
    durations: [15, 20]
    default_duration: 15
"""

ANSWER = (
    "Situation: our API was slow. My task was to fix latency. "
    "I built a caching layer and optimized queries. As a result we reduced p95 by 60 percent."
)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    path = tmp_path / "plans.yaml"
    path.write_text(PLANS_YAML, encoding="utf-8")
    settings = get_settings()
    monkeypatch.setattr(settings, "plans_enabled", True)
    monkeypatch.setattr(settings, "plans_config_path", path)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 10_000)
    monkeypatch.setattr(settings, "rate_limit_expensive_per_minute", 10_000)
    monkeypatch.setattr(settings, "daily_expensive_budget", 0)
    plans_module._cached.cache_clear()

    fake = FakeRouter()
    monkeypatch.setattr(interview_service_module, "get_router", lambda: fake)
    interview_service_module._service = None

    from app.main import app

    with TestClient(app) as test_client:
        storage_module.get_store().db.execute("DELETE FROM quotas")  # the test database is shared across tests
        test_client.fake_router = fake
        yield test_client

    interview_service_module._service = None
    plans_module._cached.cache_clear()


def create(client, ref="stu-1", expect=201, **overrides):
    payload = {
        "role": "Backend Engineer",
        "resume_text": "Senior backend engineer. Python, FastAPI, PostgreSQL. Reduced latency by 40 percent.",
        "jd_text": "Require Python and Kafka. 5+ years experience.",
        "external_ref": ref,
    }
    payload.update(overrides)
    response = client.post("/api/v1/interviews", json=payload, headers=HEADERS)
    assert response.status_code == expect, response.text
    return response.json()


def stored(interview_id):
    return storage_module.get_store().get_interview(interview_id)


# ----------------------------------------------------------------------------- required student id


def test_student_id_is_required_when_plans_are_on(client):
    response = client.post(
        "/api/v1/interviews",
        json={"role": "Backend Engineer", "resume_text": "Python engineer with ten years of experience."},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert "external_ref" in response.text


# ----------------------------------------------------------------------------- length and deadline


def test_default_length_comes_from_the_plan_and_sets_a_deadline(client):
    before = datetime.now(timezone.utc)
    body = create(client)
    status = body["interview"]
    assert status["duration_minutes"] == 15
    deadline = datetime.fromisoformat(status["deadline_at"])
    # 15 minutes plus the 60 second grace period
    assert before + timedelta(minutes=15, seconds=55) < deadline < before + timedelta(minutes=16, seconds=30)


def test_chosen_length_must_be_allowed_and_costs_nothing_when_refused(client):
    create(client, config={"duration_minutes": 25}, expect=422)
    quota = client.get("/api/v1/quotas/stu-1", headers=HEADERS).json()
    assert quota["used_minutes"] == 0


def test_length_profile_overrides_the_requested_question_count(client):
    body = create(client, config={"duration_minutes": 20, "question_count": 15, "max_followups": 15})
    config = stored(body["interview"]["id"])["config"]
    assert config["question_count"] == 9
    assert config["max_followups"] == 3
    prompt = " ".join(m.content for m in client.fake_router.calls[-1]["messages"])
    assert "Create 9 behavioural" in prompt


# ----------------------------------------------------------------------------- quota


def test_quota_is_spent_per_minute_booked(client):
    create(client)  # 15 of 30
    create(client)  # 30 of 30
    refused = client.post(
        "/api/v1/interviews",
        json={"role": "Backend Engineer", "resume_text": "Python engineer with ten years.", "external_ref": "stu-1"},
        headers=HEADERS,
    )
    assert refused.status_code == 402
    error = refused.json()["error"]
    assert error["code"] == "quota_exceeded"
    assert error["details"]["remaining_minutes"] == 0
    assert error["details"]["requested_minutes"] == 15


def test_a_longer_interview_that_does_not_fit_is_refused_but_a_shorter_one_still_fits(client):
    create(client, config={"duration_minutes": 20})  # 20 of 30, 10 left
    create(client, config={"duration_minutes": 20}, expect=402)
    create(client, config={"duration_minutes": 15}, expect=402)  # 15 > 10 left


def test_each_student_has_their_own_balance(client):
    create(client, ref="stu-1")
    create(client, ref="stu-1")
    create(client, ref="stu-2")  # unaffected by stu-1 being out of minutes


def test_failed_question_generation_gives_the_minutes_back(client):
    client.fake_router.overrides["question_generation"] = {"questions": []}
    create(client, expect=422)
    client.fake_router.overrides.pop("question_generation")
    assert client.get("/api/v1/quotas/stu-1", headers=HEADERS).json()["used_minutes"] == 0
    create(client)
    create(client)  # both bookings still fit: nothing was lost to the failure


def test_quota_endpoint_reports_balance(client):
    create(client, config={"duration_minutes": 20})
    quota = client.get("/api/v1/quotas/stu-1", headers=HEADERS).json()
    assert quota["plan"] == "standard"
    assert quota["period"] == "monthly"
    assert quota["period_key"] == datetime.now(timezone.utc).strftime("%Y-%m")
    assert quota["included_minutes"] == 30
    assert quota["bonus_minutes"] == 0
    assert quota["used_minutes"] == 20
    assert quota["remaining_minutes"] == 10


def test_grant_adds_minutes_for_a_top_up(client):
    create(client)
    create(client)
    create(client, expect=402)
    granted = client.post("/api/v1/quotas/stu-1/grant", json={"minutes": 15}, headers=HEADERS)
    assert granted.status_code == 200
    assert granted.json()["remaining_minutes"] == 15
    create(client)


def test_grant_rejects_non_positive_minutes(client):
    response = client.post("/api/v1/quotas/stu-1/grant", json={"minutes": 0}, headers=HEADERS)
    assert response.status_code == 422


def test_unknown_plan_is_refused(client):
    create(client, plan="platinum", expect=422)


# ----------------------------------------------------------------------------- server-side time limit


def _expire(interview_id):
    store = storage_module.get_store()
    config = store.get_interview(interview_id)["config"]
    config["deadline_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store.update_interview(interview_id, config=config)


def test_answers_are_accepted_before_the_deadline_and_refused_after(client):
    interview_id = create(client)["interview"]["id"]
    first = client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={"question_index": 0, "transcript": ANSWER},
        headers=HEADERS,
    )
    assert first.status_code == 200, first.text

    _expire(interview_id)
    late = client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={"question_index": 1, "transcript": ANSWER},
        headers=HEADERS,
    )
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "time_limit_reached"


def test_the_student_can_still_finish_after_time_runs_out(client):
    interview_id = create(client)["interview"]["id"]
    client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={"question_index": 0, "transcript": ANSWER},
        headers=HEADERS,
    )
    _expire(interview_id)
    finish = client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS)
    assert finish.status_code == 200, finish.text


def test_audio_after_the_deadline_is_refused_before_any_transcription(client, monkeypatch):
    called = []

    async def spy(content, filename="answer.webm"):
        called.append(filename)
        return {"text": ANSWER, "duration_seconds": 30}

    monkeypatch.setattr(stt, "transcribe_bytes", spy)
    interview_id = create(client)["interview"]["id"]
    _expire(interview_id)
    response = client.post(
        f"/api/v1/interviews/{interview_id}/answers/audio",
        data={"question_index": "0"},
        files={"audio": ("a.webm", b"fake-audio", "audio/webm")},
        headers=HEADERS,
    )
    assert response.status_code == 409
    assert called == []  # no speech-to-text compute was spent


# ----------------------------------------------------------------------------- per-answer caps


def test_defaults_cap_a_single_answer_at_three_minutes_and_4000_characters():
    settings = get_settings()
    assert settings.max_answer_seconds == 180
    assert settings.max_transcript_chars == 4000


def test_text_answer_claiming_more_than_the_cap_is_refused(client):
    interview_id = create(client)["interview"]["id"]
    response = client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={"question_index": 0, "transcript": ANSWER, "duration_seconds": 181},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert "180" in response.text


def test_audio_answer_longer_than_the_cap_is_refused(client, monkeypatch):
    async def long_audio(content, filename="answer.webm"):
        return {"text": ANSWER, "duration_seconds": 400.0}

    monkeypatch.setattr(stt, "transcribe_bytes", long_audio)
    interview_id = create(client)["interview"]["id"]
    response = client.post(
        f"/api/v1/interviews/{interview_id}/answers/audio",
        data={"question_index": "0"},
        files={"audio": ("a.webm", b"fake-audio", "audio/webm")},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert "180" in response.text


def test_audio_transcript_over_the_character_cap_is_refused(client, monkeypatch):
    async def wordy(content, filename="answer.webm"):
        return {"text": "word " * 1000, "duration_seconds": 60.0}

    monkeypatch.setattr(stt, "transcribe_bytes", wordy)
    interview_id = create(client)["interview"]["id"]
    response = client.post(
        f"/api/v1/interviews/{interview_id}/answers/audio",
        data={"question_index": "0"},
        files={"audio": ("a.webm", b"fake-audio", "audio/webm")},
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_a_refused_answer_is_not_stored(client):
    interview_id = create(client)["interview"]["id"]
    client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={"question_index": 0, "transcript": ANSWER, "duration_seconds": 500},
        headers=HEADERS,
    )
    answers = client.get(f"/api/v1/interviews/{interview_id}/answers", headers=HEADERS).json()["items"]
    assert answers == []


# ----------------------------------------------------------------------------- switched off


def test_everything_above_is_inert_when_plans_are_off(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "plans_enabled", False)
    body = client.post(
        "/api/v1/interviews",
        json={"role": "Backend Engineer", "resume_text": "Python engineer with ten years of experience."},
        headers=HEADERS,
    )
    assert body.status_code == 201
    status = body.json()["interview"]
    assert status["duration_minutes"] is None
    assert status["deadline_at"] is None
    assert client.get("/api/v1/quotas/stu-1", headers=HEADERS).status_code == 404
