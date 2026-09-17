import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.services import interview_service as interview_service_module
from app.services import storage as storage_module
from tests.fakes import FakeRouter

HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture()
def client(monkeypatch):
    fake = FakeRouter()
    monkeypatch.setattr(interview_service_module, "get_router", lambda: fake)
    interview_service_module._service = None

    from app.main import app

    with TestClient(app) as test_client:
        test_client.fake_router = fake
        yield test_client

    interview_service_module._service = None


def create_interview(client, **overrides):
    payload = {
        "role": "Backend Engineer",
        "candidate_name": "Jane Doe",
        "resume_text": "Senior backend engineer. Python, FastAPI, PostgreSQL. Reduced latency by 40 percent.",
        "jd_text": "Require Python and Kafka. 5+ years experience.",
        "config": {"question_count": 3, "analyze_per_answer": True, "ask_followups": True},
    }
    payload.update(overrides)
    response = client.post("/api/v1/interviews", json=payload, headers=HEADERS)
    assert response.status_code == 201, response.text
    return response.json()


def test_health_is_public(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_requires_api_key(client):
    response = client.post("/api/v1/interviews", json={"role": "Backend Engineer"})
    assert response.status_code == 401


def test_incomplete_interview_rejected(client):
    response = client.post("/api/v1/interviews", json={"role": "Backend Engineer"}, headers=HEADERS)
    assert response.status_code == 422


def test_full_interview_flow(client):
    created = create_interview(client)
    interview_id = created["interview"]["id"]
    assert created["interview"]["status"] == "questions_ready"
    assert len(created["questions"]) == 2

    answer = client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={
            "question_index": 0,
            "transcript": (
                "Situation: our API was slow. My task was to fix latency. "
                "I built a caching layer and optimized queries. As a result we reduced p95 by 60 percent."
            ),
            "duration_seconds": 45,
        },
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["heuristic_scores"]["overall"] > 0
    assert body["analysis"]["scores"]["overall"] == 76
    assert body["followup"]

    transcript = client.get(f"/api/v1/interviews/{interview_id}/transcript", headers=HEADERS)
    assert transcript.status_code == 200
    assert len(transcript.json()["items"]) == 1

    finish = client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS)
    assert finish.status_code == 200, finish.text
    report = finish.json()["report"]
    assert report["overall_score"] == 76
    assert report["readiness_level"] == "almost_ready"
    assert report["improvement_plan"]["quick_wins"]
    assert report["routing_trace"]["final_scoring"]["tier"] == "standard"

    stored = client.get(f"/api/v1/interviews/{interview_id}/report", headers=HEADERS)
    assert stored.status_code == 200
    assert stored.json()["report"]["interview_id"] == interview_id
    assert stored.json()["status"] == "completed"


def test_finish_without_answers_rejected(client):
    created = create_interview(client)
    interview_id = created["interview"]["id"]
    response = client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS)
    assert response.status_code == 422


def test_answer_index_out_of_range(client):
    created = create_interview(client)
    interview_id = created["interview"]["id"]
    response = client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={"question_index": 99, "transcript": "Hello there"},
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_unknown_interview_returns_404(client):
    response = client.get("/api/v1/interviews/does-not-exist", headers=HEADERS)
    assert response.status_code == 404


def test_models_endpoint_reports_router_status(client):
    response = client.get("/api/v1/models", headers=HEADERS)
    assert response.status_code == 200
    assert "providers" in response.json()
