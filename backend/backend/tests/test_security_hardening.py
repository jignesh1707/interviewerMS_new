import socket

import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_API_KEY, Settings, get_settings
from app.core.errors import ValidationAppError
from app.core.url_safety import validate_callback_url
from app.services import interview_service as interview_service_module
from app.services import webhook
from tests.fakes import FakeRouter

KEY_A = "a" * 40
KEY_B = "b" * 40
HEADERS_A = {"X-API-Key": KEY_A}
HEADERS_B = {"X-API-Key": KEY_B}
STRONG = "s3cr3t-" + "x" * 40


@pytest.fixture()
def client(monkeypatch):
    fake = FakeRouter()
    monkeypatch.setattr(interview_service_module, "get_router", lambda: fake)
    interview_service_module._service = None
    monkeypatch.setattr(get_settings(), "api_keys", f"tenant-a:{KEY_A},tenant-b:{KEY_B}")

    async def no_delivery(url, event, data):
        return {"delivered": False, "reason": "stubbed"}

    monkeypatch.setattr(webhook, "deliver", no_delivery)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    interview_service_module._service = None


def _payload(**overrides):
    payload = {
        "role": "Backend Engineer",
        "resume_text": "Senior backend engineer. Python, FastAPI, PostgreSQL. Reduced latency by 40 percent.",
        "jd_text": "Require Python and Kafka. 5+ years experience.",
        "config": {"question_count": 3},
    }
    payload.update(overrides)
    return payload


def _create(client, headers=HEADERS_A, **overrides):
    response = client.post("/api/v1/interviews", json=_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["interview"]["id"]


# ---- 1. startup / default key -------------------------------------------------------------


def test_default_key_is_allowed_in_development():
    Settings(environment="development", api_keys=DEFAULT_API_KEY, cors_origins="*").validate_for_startup()


@pytest.mark.parametrize("keys", [DEFAULT_API_KEY, "short-key", f"{STRONG},short", ""])
def test_weak_keys_refused_in_production(keys):
    with pytest.raises(RuntimeError):
        Settings(environment="production", api_keys=keys, cors_origins="https://app.example.com").validate_for_startup()


def test_wildcard_cors_refused_in_production():
    with pytest.raises(RuntimeError):
        Settings(environment="production", api_keys=STRONG, cors_origins="*").validate_for_startup()


def test_strong_config_accepted_in_production():
    Settings(environment="production", api_keys=STRONG, cors_origins="https://app.example.com").validate_for_startup()


def test_default_bind_is_loopback():
    assert Settings().host == "127.0.0.1"


# ---- 2. SSRF via callback_url -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://127.0.0.1:8080/x",
        "https://localhost/x",
        "https://10.0.0.5/x",
        "https://[::1]/x",
        "https://[::ffff:10.0.0.5]/x",
        "https://user:pw@93.184.216.34/x",
        "file:///etc/passwd",
        "https:///nohost",
    ],
)
def test_unsafe_callback_urls_rejected(url, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValidationAppError):
        validate_callback_url(url)


def test_public_literal_ip_accepted():
    assert validate_callback_url("https://93.184.216.34/hook")


def test_hostname_resolving_to_private_ip_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.3", 443))])
    with pytest.raises(ValidationAppError):
        validate_callback_url("https://rebind.example.com/hook")


def test_hostname_resolving_to_public_ip_accepted(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert validate_callback_url("https://hooks.example.com/hook")


def test_allowlist_enforced(monkeypatch):
    monkeypatch.setattr(get_settings(), "callback_allowed_hosts", "resumetojob.example.com")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])
    assert validate_callback_url("https://resumetojob.example.com/hook")
    with pytest.raises(ValidationAppError):
        validate_callback_url("https://other.example.com/hook")


def test_create_rejects_internal_callback(client):
    response = client.post(
        "/api/v1/interviews",
        json=_payload(callback_url="https://169.254.169.254/latest/meta-data"),
        headers=HEADERS_A,
    )
    assert response.status_code == 422
    assert "private" in response.json()["error"]["message"]


async def test_deliver_blocks_internal_target():
    # restore the real deliver (the fixture stubs it only inside `client` tests)
    import importlib

    real = importlib.reload(webhook)
    result = await real.deliver("https://10.0.0.5/hook", "interview.completed", {"x": 1})
    assert result["delivered"] is False
    assert result["reason"].startswith("blocked")


# ---- 3. tenant isolation ------------------------------------------------------------------


def test_tenant_cannot_read_other_tenants_interview(client):
    interview_id = _create(client, HEADERS_A)
    for path in ("", "/questions", "/answers", "/transcript", "/report", "/events", "/status"):
        own = client.get(f"/api/v1/interviews/{interview_id}{path}", headers=HEADERS_A)
        other = client.get(f"/api/v1/interviews/{interview_id}{path}", headers=HEADERS_B)
        assert own.status_code == 200, path
        assert other.status_code == 404, path


def test_tenant_cannot_write_to_other_tenants_interview(client):
    interview_id = _create(client, HEADERS_A)
    answer = client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={"question_index": 0, "transcript": "I did a thing"},
        headers=HEADERS_B,
    )
    finish = client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS_B)
    audio = client.post(
        f"/api/v1/interviews/{interview_id}/answers/audio",
        data={"question_index": "0"},
        files={"audio": ("a.webm", b"1234")},
        headers=HEADERS_B,
    )
    assert answer.status_code == finish.status_code == audio.status_code == 404


def test_list_is_scoped_to_tenant(client):
    id_a = _create(client, HEADERS_A)
    id_b = _create(client, HEADERS_B)
    ids_a = {item["id"] for item in client.get("/api/v1/interviews", headers=HEADERS_A).json()["items"]}
    ids_b = {item["id"] for item in client.get("/api/v1/interviews", headers=HEADERS_B).json()["items"]}
    assert id_a in ids_a and id_b not in ids_a
    assert id_b in ids_b and id_a not in ids_b


def test_untagged_legacy_rows_are_not_visible(client):
    from app.services.storage import get_store

    legacy = get_store().create_interview(
        role="Legacy", candidate_name=None, resume_text="x" * 40, jd_text=None,
        config={}, callback_url=None, metadata={},
    )
    response = client.get(f"/api/v1/interviews/{legacy['id']}", headers=HEADERS_A)
    assert response.status_code == 404


def test_wrong_key_rejected(client):
    assert client.get("/api/v1/interviews", headers={"X-API-Key": "z" * 40}).status_code == 401


# ---- 4. size limits -----------------------------------------------------------------------


def test_oversized_resume_text_rejected(client):
    limit = get_settings().max_text_chars
    response = client.post(
        "/api/v1/interviews", json=_payload(resume_text="a" * (limit + 1)), headers=HEADERS_A
    )
    assert response.status_code == 422


def test_oversized_metadata_rejected(client):
    response = client.post(
        "/api/v1/interviews", json=_payload(metadata={"k": "v" * 20_000}), headers=HEADERS_A
    )
    assert response.status_code == 422


def test_oversized_transcript_rejected(client):
    interview_id = _create(client)
    response = client.post(
        f"/api/v1/interviews/{interview_id}/answers",
        json={"question_index": 0, "transcript": "word " * 5000},
        headers=HEADERS_A,
    )
    assert response.status_code == 422


def test_body_limit_rejects_by_content_length(client):
    too_big = (2 * get_settings().max_doc_upload_mb + 2) * 1024 * 1024
    response = client.post(
        "/api/v1/interviews",
        content=b"{}",
        headers={**HEADERS_A, "Content-Type": "application/json", "Content-Length": str(too_big)},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_body_limit_rejects_streamed_body(client):
    too_big = b"x" * ((2 * get_settings().max_doc_upload_mb + 2) * 1024 * 1024)

    def chunks():
        for start in range(0, len(too_big), 1024 * 1024):
            yield too_big[start : start + 1024 * 1024]

    response = client.post(
        "/api/v1/interviews", content=chunks(), headers={**HEADERS_A, "Content-Type": "application/json"}
    )
    assert response.status_code == 413


def test_upload_file_over_doc_limit_rejected(client):
    big = b"a" * (get_settings().max_doc_upload_mb * 1024 * 1024 + 1)
    response = client.post(
        "/api/v1/interviews/upload",
        data={"role": "Backend Engineer"},
        files={"resume_file": ("resume.txt", big)},
        headers=HEADERS_A,
    )
    assert response.status_code == 413


def test_upload_invalid_form_payload_is_422_not_500(client):
    response = client.post(
        "/api/v1/interviews/upload",
        data={"role": "Backend Engineer", "metadata_json": '{"k": "' + "v" * 20_000 + '"}', "resume_text": "x" * 50},
        headers=HEADERS_A,
    )
    assert response.status_code == 422


def test_audio_over_limit_rejected_before_transcription(client, monkeypatch):
    interview_id = _create(client)
    monkeypatch.setattr(get_settings(), "stt_max_upload_mb", 1)
    response = client.post(
        f"/api/v1/interviews/{interview_id}/answers/audio",
        data={"question_index": "0"},
        files={"audio": ("a.webm", b"a" * (1024 * 1024 + 10))},
        headers=HEADERS_A,
    )
    assert response.status_code == 413


def test_synthesize_rejects_bad_voice_and_long_text(client):
    long_text = client.post("/api/v1/speech/synthesize", data={"text": "a" * 3001}, headers=HEADERS_A)
    bad_voice = client.post(
        "/api/v1/speech/synthesize", data={"text": "hi", "voice": "../../etc/passwd"}, headers=HEADERS_A
    )
    assert long_text.status_code == 422
    assert bad_voice.status_code == 422
