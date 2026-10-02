"""Per-student rate limit and daily budget, keyed on the interview's external_ref (the student id)."""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.services import interview_service as interview_service_module
from app.voice import stt
from tests.fakes import FakeRouter

KEY_A = "a" * 40
KEY_B = "b" * 40
HEADERS = {"X-API-Key": KEY_A}
HEADERS_B = {"X-API-Key": KEY_B}

ANSWER = (
    "Situation: our API was slow. My task was to fix latency. "
    "I built a caching layer and optimized queries. As a result we reduced p95 by 60 percent."
)


@pytest.fixture()
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "api_keys", f"tenant-a:{KEY_A},tenant-b:{KEY_B}")
    monkeypatch.setattr(settings, "plans_enabled", False)
    # Tenant-wide limits stay out of the way so only the per-student limits can trip.
    monkeypatch.setattr(settings, "rate_limit_per_minute", 10_000)
    monkeypatch.setattr(settings, "rate_limit_expensive_per_minute", 10_000)
    monkeypatch.setattr(settings, "daily_expensive_budget", 0)
    monkeypatch.setattr(settings, "student_rate_limit_per_minute", 0)
    monkeypatch.setattr(settings, "student_daily_budget", 0)

    fake = FakeRouter()
    monkeypatch.setattr(interview_service_module, "get_router", lambda: fake)
    interview_service_module._service = None

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

    interview_service_module._service = None


def create(client, ref="stu-1", headers=HEADERS, expect=201):
    payload = {
        "role": "Backend Engineer",
        "resume_text": "Senior backend engineer. Python, FastAPI, PostgreSQL. Reduced latency by 40 percent.",
        "jd_text": "Require Python and Kafka. 5+ years experience.",
    }
    if ref is not None:
        payload["external_ref"] = ref
    response = client.post("/api/v1/interviews", json=payload, headers=headers)
    assert response.status_code == expect, response.text
    return response.json()


def answer(client, interview_id, index=0, headers=HEADERS, expect=200):
    response = client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={"question_index": index, "transcript": ANSWER},
        headers=headers,
    )
    assert response.status_code == expect, response.text
    return response


def test_defaults_are_on_and_generous():
    settings = get_settings()
    assert settings.student_rate_limit_per_minute == 30
    assert settings.student_daily_budget == 200


# ----------------------------------------------------------------------------- per minute


def test_one_student_is_rate_limited_per_minute(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "student_rate_limit_per_minute", 3)
    interview_id = create(client)["interview"]["id"]  # call 1
    answer(client, interview_id, 0)  # call 2
    answer(client, interview_id, 1)  # call 3
    limited = client.post(
        f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS
    )  # call 4
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


def test_other_students_are_not_affected(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "student_rate_limit_per_minute", 1)
    create(client, ref="stu-1")
    create(client, ref="stu-1", expect=429)
    create(client, ref="stu-2")  # a different student still gets through


def test_the_same_student_id_under_another_tenant_is_separate(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "student_rate_limit_per_minute", 1)
    create(client, ref="stu-1", headers=HEADERS)
    create(client, ref="stu-1", headers=HEADERS, expect=429)
    create(client, ref="stu-1", headers=HEADERS_B)


# ----------------------------------------------------------------------------- per day


def test_one_student_has_a_daily_budget(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "student_daily_budget", 2)
    interview_id = create(client)["interview"]["id"]
    answer(client, interview_id, 0)
    limited = answer(client, interview_id, 1, expect=429)
    assert "budget" in limited.json()["error"]["message"]
    assert "student" in limited.json()["error"]["message"]
    create(client, ref="stu-2")  # another student has their own budget


# ----------------------------------------------------------------------------- scope


def test_calls_without_a_student_id_are_not_limited_per_student(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "student_rate_limit_per_minute", 1)
    monkeypatch.setattr(settings, "student_daily_budget", 1)
    for _ in range(3):
        create(client, ref=None)


def test_zero_switches_the_student_limits_off(client):
    for _ in range(5):
        create(client, ref="stu-1")


def test_audio_is_refused_before_any_transcription_when_the_student_is_limited(client, monkeypatch):
    called = []

    async def spy(content, filename="answer.webm"):
        called.append(filename)
        return {"text": ANSWER, "duration_seconds": 30}

    monkeypatch.setattr(stt, "transcribe_bytes", spy)
    monkeypatch.setattr(get_settings(), "student_rate_limit_per_minute", 1)
    interview_id = create(client)["interview"]["id"]  # uses the student's one call this minute
    response = client.post(
        f"/api/v1/interviews/{interview_id}/answers/audio",
        data={"question_index": "0"},
        files={"audio": ("a.webm", b"fake-audio", "audio/webm")},
        headers=HEADERS,
    )
    assert response.status_code == 429
    assert called == []


def test_upload_route_counts_toward_the_student_limit(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "student_rate_limit_per_minute", 1)
    data = {
        "role": "Backend Engineer",
        "external_ref": "stu-1",
        "resume_text": "Senior backend engineer. Python, FastAPI, PostgreSQL. Reduced latency by 40 percent.",
    }
    assert client.post("/api/v1/interviews/upload", data=data, headers=HEADERS).status_code == 201
    assert client.post("/api/v1/interviews/upload", data=data, headers=HEADERS).status_code == 429
