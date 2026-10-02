"""The copy of a resume or answer that goes to an AI provider is redacted; what the service stores is not."""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.services import interview_service as interview_service_module
from app.services import storage as storage_module
from tests.fakes import FakeRouter

HEADERS = {"X-API-Key": "test-key"}

RESUME = (
    "Priya Raman\n"
    "priya.raman@example.com | +1 (415) 555-0132 | linkedin.com/in/priyaraman\n"
    "Work authorization: F-1 OPT until 2027\n"
    "Senior backend engineer. Python, FastAPI, PostgreSQL. Reduced latency by 40 percent.\n"
)
ANSWER = (
    "Situation: our API was slow. I built a caching layer and the result was p95 down 60 percent. "
    "You can reach me at priya.raman@example.com or 415-555-0132."
)
SECRETS = ("priya.raman@example.com", "415-555-0132", "(415) 555-0132", "priyaraman", "F-1", "OPT")


@pytest.fixture()
def client(monkeypatch):
    fake = FakeRouter()
    monkeypatch.setattr(interview_service_module, "get_router", lambda: fake)
    interview_service_module._service = None
    monkeypatch.setattr(get_settings(), "plans_enabled", False)
    from app.main import app

    with TestClient(app) as test_client:
        test_client.fake_router = fake
        yield test_client
    interview_service_module._service = None


def prompt_of(call) -> str:
    return " ".join(message.content for message in call["messages"])


def create(client, **overrides):
    payload = {
        "role": "Backend Engineer",
        "candidate_name": "Priya Raman",
        "resume_text": RESUME,
        "jd_text": "Require Python and Kafka. 5+ years experience.",
        "config": {"question_count": 3},
    }
    payload.update(overrides)
    response = client.post("/api/v1/interviews", json=payload, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()["interview"]["id"]


def calls_for(client, task):
    return [call for call in client.fake_router.calls if call["task"] == task]


def test_the_resume_summary_prompt_has_no_contact_details_name_or_visa_lines(client):
    create(client)
    (call,) = calls_for(client, "resume_summary")
    prompt = prompt_of(call)
    for secret in (*SECRETS, "Priya", "Raman"):
        assert secret not in prompt, secret
    assert "Senior backend engineer" in prompt and "FastAPI" in prompt  # the substance is still there


def test_nothing_the_question_prompt_sends_contains_identifiers(client):
    create(client)
    for task in ("question_generation", "resume_summary"):
        for call in calls_for(client, task):
            for secret in SECRETS:
                assert secret not in prompt_of(call), (task, secret)


def test_the_original_resume_is_still_stored_untouched(client):
    interview_id = create(client)
    stored = storage_module.get_store().get_interview(interview_id)
    assert stored["resume_text"] == RESUME  # redaction only changes the copy sent to the provider


def test_answer_prompts_have_no_contact_details_but_the_stored_transcript_does(client):
    interview_id = create(client)
    response = client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={"question_index": 0, "transcript": ANSWER},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    for task in ("answer_analysis", "followup_generation"):
        for call in calls_for(client, task):
            prompt = prompt_of(call)
            assert "priya.raman@example.com" not in prompt and "415-555-0132" not in prompt, task
            assert "caching layer" in prompt  # the answer itself still reaches the model
    answers = client.get(f"/api/v1/interviews/{interview_id}/answers", headers=HEADERS).json()["items"]
    assert "priya.raman@example.com" in answers[0]["transcript"]


def test_redaction_can_be_switched_off(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "redact_pii_for_llm", False)
    create(client)
    (call,) = calls_for(client, "resume_summary")
    assert "priya.raman@example.com" in prompt_of(call)


def test_redaction_is_on_by_default():
    assert get_settings().redact_pii_for_llm is True
