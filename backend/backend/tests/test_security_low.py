import asyncio
import hashlib
import hmac
import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.errors import ValidationAppError
from app.services import interview_service as interview_service_module
from app.services import webhook
from app.voice import tts
from app.voice.audio import safe_audio_suffix
from tests.fakes import FakeRouter

KEY = "k" * 40
HEADERS = {"X-API-Key": KEY}
SECRET = "w" * 32
PUBLIC_URL = "https://93.184.216.34/hook"


@pytest.fixture()
def fake():
    return FakeRouter()


@pytest.fixture()
def client(monkeypatch, fake):
    monkeypatch.setattr(interview_service_module, "get_router", lambda: fake)
    interview_service_module._service = None
    settings = get_settings()
    monkeypatch.setattr(settings, "api_keys", f"low:{KEY}")
    monkeypatch.setattr(settings, "rate_limit_per_minute", 10_000)
    monkeypatch.setattr(settings, "rate_limit_expensive_per_minute", 10_000)
    monkeypatch.setattr(settings, "daily_expensive_budget", 0)

    async def no_delivery(url, event, data):
        return {"delivered": False}

    monkeypatch.setattr(webhook, "deliver", no_delivery)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    interview_service_module._service = None


def _create(client):
    response = client.post(
        "/api/v1/interviews",
        json={
            "role": "Backend Engineer",
            "resume_text": "Senior backend engineer. Python, FastAPI, PostgreSQL. Reduced latency by 40 percent.",
            "jd_text": "Require Python and Kafka. 5+ years experience.",
            "config": {"question_count": 3, "ask_followups": False},
        },
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()["interview"]["id"]


ANSWER = {"question_index": 0, "transcript": "I built a cache and reduced p95 latency by 60 percent."}


# ---- /ready -------------------------------------------------------------------------------


def test_ready_is_public_and_minimal(client):
    response = client.get("/api/v1/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_details_needs_key_and_hides_paths(client):
    assert client.get("/api/v1/ready/details").status_code == 401
    response = client.get("/api/v1/ready/details", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert "llm_providers" in body and "voice" in body
    assert "storage" not in body
    assert str(get_settings().database_path) not in json.dumps(body)


def test_ready_reports_503_when_storage_down(client, monkeypatch):
    import app.api.routes_system as routes_system

    class Broken:
        async def ping(self):
            raise RuntimeError("db gone at /secret/path")

    monkeypatch.setattr(routes_system, "get_async_store", lambda: Broken())
    response = client.get("/api/v1/ready")
    assert response.status_code == 503
    assert "secret" not in response.text


# ---- webhooks -----------------------------------------------------------------------------


class _Response:
    status_code = 200
    text = ""


class _FakeClient:
    posts: list[dict] = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content, headers):
        _FakeClient.posts.append({"url": url, "content": content, "headers": headers, "client": self.kwargs})
        return _Response()


@pytest.fixture()
def real_webhook(monkeypatch):
    module = importlib.reload(webhook)
    _FakeClient.posts = []
    monkeypatch.setattr(module.httpx, "AsyncClient", _FakeClient)
    return module


async def test_webhook_signed_with_timestamp_and_delivery_id(real_webhook, monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_secret", SECRET)
    result = await real_webhook.deliver(PUBLIC_URL, "interview.completed", {"x": 1})
    assert result["delivered"] is True
    sent = _FakeClient.posts[0]
    headers, body = sent["headers"], sent["content"]
    assert sent["client"]["follow_redirects"] is False
    assert len(headers["X-Interview-Delivery"]) == 32
    timestamp = headers["X-Interview-Timestamp"]
    assert abs(int(timestamp) - json.loads(body)["sent_at"]) <= 1
    expected_v2 = hmac.new(SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(headers["X-Interview-Signature-V2"], expected_v2)
    legacy = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert headers["X-Interview-Signature"] == legacy


async def test_unsigned_webhook_refused_outside_development(real_webhook, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "webhook_secret", "")
    result = await real_webhook.deliver(PUBLIC_URL, "interview.completed", {})
    assert result["delivered"] is False
    assert "WEBHOOK_SECRET" in result["reason"]
    assert _FakeClient.posts == []


async def test_unsigned_webhook_allowed_in_development(real_webhook, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "webhook_secret", "")
    result = await real_webhook.deliver(PUBLIC_URL, "interview.completed", {})
    assert result["delivered"] is True
    assert "X-Interview-Signature-V2" not in _FakeClient.posts[0]["headers"]


# ---- speech inputs ------------------------------------------------------------------------


async def test_tts_rejects_voice_not_in_allowlist():
    with pytest.raises(ValidationAppError, match="voice is not allowed"):
        await tts.synthesize("hello", "../../models/evil")


def test_tts_allowlist_extended_by_setting(monkeypatch):
    monkeypatch.setattr(get_settings(), "piper_allowed_voices", "en_GB-alan-medium")
    voices = get_settings().piper_voice_set
    assert "en_GB-alan-medium" in voices
    assert get_settings().piper_default_voice in voices


@pytest.mark.parametrize(
    "name, expected",
    [
        ("a.WAV", ".wav"),
        ("answer.webm", ".webm"),
        ("../../evil.sh", ".webm"),
        ("noext", ".webm"),
        (None, ".webm"),
        ("x.exe", ".webm"),
    ],
)
def test_audio_suffix_whitelist(name, expected):
    assert safe_audio_suffix(name) == expected


# ---- answer / finish state ----------------------------------------------------------------


def test_duplicate_answer_for_same_question_is_conflict(client):
    interview_id = _create(client)
    first = client.post(f"/api/v1/interviews/{interview_id}/answers", json=ANSWER, headers=HEADERS)
    assert first.status_code == 200, first.text
    second = client.post(f"/api/v1/interviews/{interview_id}/answers", json=ANSWER, headers=HEADERS)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"
    other = client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={**ANSWER, "question_index": 1},
        headers=HEADERS,
    )
    assert other.status_code == 200


def test_answer_after_finish_is_conflict(client):
    interview_id = _create(client)
    assert client.post(f"/api/v1/interviews/{interview_id}/answers", json=ANSWER, headers=HEADERS).status_code == 200
    assert client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS).status_code == 200
    late = client.post(
        f"/api/v1/interviews/{interview_id}/answers", json={**ANSWER, "question_index": 1}, headers=HEADERS
    )
    assert late.status_code == 409


def test_finish_is_idempotent_and_does_not_rescore(client, fake):
    interview_id = _create(client)
    client.post(f"/api/v1/interviews/{interview_id}/answers", json=ANSWER, headers=HEADERS)
    first = client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS)
    calls_after_first = len(fake.calls)
    second = client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS)
    assert first.status_code == second.status_code == 200
    assert len(fake.calls) == calls_after_first
    assert second.json()["report"] == first.json()["report"]


# ---- operational files --------------------------------------------------------------------


def test_run_script_only_reloads_when_asked():
    root = Path(__file__).resolve().parents[3]
    script = (root / "scripts" / "scripts" / "run_backend.sh").read_text()
    assert "RELOAD:-0" in script
    exec_line = next(line for line in script.splitlines() if line.startswith("exec uvicorn"))
    assert "--reload" not in exec_line


def test_dockerfile_runs_unprivileged_in_production_mode():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "USER appuser" in dockerfile
    assert "ENVIRONMENT=production" in dockerfile
    assert "requirements.lock.txt" in dockerfile
