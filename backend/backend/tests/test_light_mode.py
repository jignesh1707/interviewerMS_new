"""LIGHT_MODE: a small always-on instance that serves only cheap reads and never loads the speech model."""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import BASE_DIR, DEFAULT_API_KEY, Settings, get_settings
from app.core import plans as plans_module
from app.core.light_mode import ALLOWED_ROUTES, LIGHT_MODE_CODE, is_allowed
from app.services import interview_service as interview_service_module
from app.voice import stt
from tests.fakes import FakeRouter

KEY = "k" * 40
HEADERS = {"X-API-Key": KEY}

# Every route the app serves that is NOT a cheap read. In light mode each must be refused.
REFUSED = [
    ("POST", "/api/v1/interviews"),
    ("POST", "/api/v1/interviews/upload"),
    ("GET", "/api/v1/interviews"),
    ("GET", "/api/v1/interviews/abc"),
    ("GET", "/api/v1/interviews/abc/status"),
    ("GET", "/api/v1/interviews/abc/report"),
    ("GET", "/api/v1/interviews/abc/events"),
    ("GET", "/api/v1/interviews/abc/transcript"),
    ("POST", "/api/v1/interviews/abc/answers"),
    ("POST", "/api/v1/interviews/abc/answers/audio"),
    ("POST", "/api/v1/interviews/abc/finish"),
    ("POST", "/api/v1/interviews/abc/skip"),
    ("POST", "/api/v1/interviews/abc/pause"),
    ("POST", "/api/v1/interviews/abc/resume"),
    ("DELETE", "/api/v1/interviews/abc"),
    ("DELETE", "/api/v1/interviews/by-ref/student-1"),
    ("POST", "/api/v1/speech/transcribe"),
    ("POST", "/api/v1/speech/synthesize"),
    ("POST", "/api/v1/packs/activate"),
    ("POST", "/api/v1/packs/revoke"),
    ("POST", "/api/v1/packs/expire"),
    ("POST", "/api/v1/packs/extend"),
    ("DELETE", "/api/v1/packs/by-ref/student-1"),
    ("GET", "/api/v1/models"),
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("GET", "/api/v1/nothing-here"),
]


@pytest.fixture()
def configure(monkeypatch):
    """Settings shared by every test here; returns the live settings object."""
    settings = get_settings()
    monkeypatch.setattr(settings, "api_keys", f"light:{KEY}")
    monkeypatch.setattr(settings, "plans_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 10_000)
    monkeypatch.setattr(settings, "rate_limit_expensive_per_minute", 10_000)
    monkeypatch.setattr(settings, "daily_expensive_budget", 0)
    monkeypatch.setattr(settings, "student_rate_limit_per_minute", 0)
    monkeypatch.setattr(settings, "student_daily_budget", 0)
    fake = FakeRouter()
    monkeypatch.setattr(interview_service_module, "get_router", lambda: fake)
    interview_service_module._service = None
    plans_module._cached.cache_clear()
    yield settings
    interview_service_module._service = None
    plans_module._cached.cache_clear()
    stt._preload_pending = False


@pytest.fixture()
def normal(configure, monkeypatch):
    monkeypatch.setattr(configure, "light_mode", False)
    with TestClient(main_module.app) as client:
        yield client


@pytest.fixture()
def light(configure, monkeypatch):
    monkeypatch.setattr(configure, "light_mode", True)
    with TestClient(main_module.app) as client:
        yield client


@pytest.fixture()
def spies(monkeypatch):
    """Replace every startup task with a recorder, so a test can see which ones the app starts."""
    started = []

    def fake_preload():
        started.append("preload")
        return asyncio.get_event_loop().create_task(asyncio.sleep(0))

    async def loop(name):
        started.append(name)

    from app.services import webhook

    monkeypatch.setattr(stt, "begin_preload", fake_preload)
    monkeypatch.setattr(main_module, "_build_sweep_loop", lambda: loop("sweep"))
    monkeypatch.setattr(main_module, "_retention_loop", lambda: loop("retention"))
    monkeypatch.setattr(webhook, "outbox_loop", lambda: loop("outbox"))
    return started


# ----------------------------------------------------------------------------- the setting


def test_light_mode_is_off_by_default():
    assert Settings().light_mode is False


def test_light_mode_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("LIGHT_MODE", "true")
    assert Settings().light_mode is True


# ----------------------------------------------------------------------------- off: nothing changes


def test_with_light_mode_off_health_is_exactly_what_it_was(normal):
    body = normal.get("/api/v1/health").json()
    assert set(body) == {"status", "service", "release"}  # no "mode" key


@pytest.mark.parametrize("method,path", REFUSED)
def test_with_light_mode_off_no_route_is_refused_as_a_light_route(normal, method, path):
    response = normal.request(method, path)  # no key: auth answers 401, or the path does not exist
    assert LIGHT_MODE_CODE not in response.text


def test_with_light_mode_off_heavy_routes_still_work(normal):
    assert normal.get("/api/v1/interviews", headers=HEADERS).status_code == 200
    assert normal.get("/api/v1/models", headers=HEADERS).status_code == 200
    assert normal.get("/docs").status_code == 200
    assert normal.post("/api/v1/speech/transcribe").status_code == 401  # reaches auth, not the light guard


def test_with_light_mode_off_ready_still_waits_for_the_speech_model(normal):
    stt._preload_pending = True
    not_ready = normal.get("/api/v1/ready")
    assert not_ready.status_code == 503 and "loading" in not_ready.json()["error"]["message"]
    stt._preload_pending = False
    assert normal.get("/api/v1/ready").status_code == 200


def test_with_light_mode_off_startup_still_preloads_and_runs_every_loop(configure, monkeypatch, spies):
    monkeypatch.setattr(configure, "light_mode", False)
    monkeypatch.setattr(configure, "stt_preload", True)
    monkeypatch.setattr(configure, "retention_days", 1)
    monkeypatch.setattr(configure, "webhook_outbox_worker", True)
    with TestClient(main_module.app):
        pass
    assert set(spies) == {"preload", "sweep", "retention", "outbox"}


# ----------------------------------------------------------------------------- on: reads are served


def test_light_health_is_public_and_says_it_is_the_light_instance(light):
    response = light.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["mode"] == "light"
    assert light.get("/").status_code == 200


def test_light_ready_answers_200_without_waiting_for_the_speech_model(light):
    stt._preload_pending = True  # a normal instance would answer 503 "still loading" here, forever
    response = light.get("/api/v1/ready")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_light_ready_is_still_503_when_the_database_is_down(light, monkeypatch):
    class Broken:
        async def ping(self):
            raise RuntimeError("db down")

    monkeypatch.setattr("app.api.routes_system.get_async_store", lambda: Broken())
    assert light.get("/api/v1/ready").status_code == 503


def test_light_ready_details_reports_the_model_is_not_loaded(light):
    body = light.get("/api/v1/ready/details", headers=HEADERS).json()
    assert body["voice"]["stt_model_loaded"] is False


def test_light_serves_a_students_packs_exactly_like_the_normal_instance(configure, monkeypatch):
    ref = "student-light-1"
    monkeypatch.setattr(configure, "light_mode", False)
    with TestClient(main_module.app) as normal:
        activated = normal.post(
            "/api/v1/packs/activate",
            headers=HEADERS,
            json={
                "external_ref": ref,
                "plan": "economy",
                "payment_id": "pay-light-1",
                "purchased_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert activated.status_code == 200, activated.text
        from_normal = normal.get(f"/api/v1/packs/{ref}", headers=HEADERS)
    monkeypatch.setattr(configure, "light_mode", True)
    with TestClient(main_module.app) as light:
        from_light = light.get(f"/api/v1/packs/{ref}", headers=HEADERS)
    assert from_light.status_code == 200
    packs = {p["plan"]: p for p in from_light.json()["packs"]}
    assert packs["economy"]["active"] is True and packs["economy"]["minutes_remaining"] > 0
    assert from_light.json() == from_normal.json()


def test_light_serves_the_plan_catalog(light):
    response = light.get("/api/v1/plans", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["default_plan"]


# ----------------------------------------------------------------------------- on: auth is unchanged


def test_light_packs_still_need_an_api_key(light):
    assert light.get("/api/v1/packs/student-1").status_code == 401
    assert light.get("/api/v1/packs/student-1", headers={"X-API-Key": "w" * 40}).status_code == 401
    assert light.get("/api/v1/plans").status_code == 401
    assert light.get("/api/v1/ready/details").status_code == 401


def test_light_accepts_a_bearer_token_too(light):
    response = light.get("/api/v1/packs/student-1", headers={"Authorization": f"Bearer {KEY}"})
    assert response.status_code == 200


# ----------------------------------------------------------------------------- on: heavy routes are refused


@pytest.mark.parametrize("method,path", REFUSED)
def test_light_refuses_every_other_route(light, method, path):
    for headers in ({}, HEADERS):  # refused before authentication, so a key makes no difference
        response = light.request(method, path, headers=headers)
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == LIGHT_MODE_CODE
        assert "light" in error["message"]


def test_light_turns_a_speech_upload_away_without_transcribing_it(light, monkeypatch):
    async def must_not_run(*args, **kwargs):
        raise AssertionError("the light instance must never transcribe")

    monkeypatch.setattr(stt, "transcribe_bytes", must_not_run)
    monkeypatch.setattr(stt, "_get_model", must_not_run)
    response = light.post(
        "/api/v1/interviews/abc/answers/audio",
        headers=HEADERS,
        data={"question_index": "0"},
        files={"audio": ("a.webm", b"\x00" * 2048, "audio/webm")},
    )
    assert response.status_code == 404 and response.json()["error"]["code"] == LIGHT_MODE_CODE
    response = light.post("/api/v1/speech/transcribe", headers=HEADERS, files={"audio": ("a.wav", b"x", "audio/wav")})
    assert response.status_code == 404 and response.json()["error"]["code"] == LIGHT_MODE_CODE
    assert stt._model is None


def test_light_still_answers_a_cors_preflight(light):
    response = light.options(
        "/api/v1/packs/student-1",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 200


def test_the_allowlist_matches_what_the_docs_promise():
    assert ALLOWED_ROUTES == (
        "GET /",
        "GET /api/v1/health",
        "GET /api/v1/ready",
        "GET /api/v1/ready/details",
        "GET /api/v1/plans",
        "GET /api/v1/packs/{external_ref}",
    )
    assert is_allowed("GET", "/api/v1/packs/student-1")
    assert is_allowed("HEAD", "/api/v1/health")
    assert not is_allowed("GET", "/api/v1/packs/by-ref/student-1")  # two segments: not a balance read
    assert not is_allowed("POST", "/api/v1/packs/student-1")
    assert not is_allowed("DELETE", "/api/v1/packs/student-1")


# ----------------------------------------------------------------------------- on: startup


def test_light_startup_loads_no_speech_model_and_runs_no_background_loops(configure, monkeypatch, spies):
    monkeypatch.setattr(configure, "light_mode", True)
    monkeypatch.setattr(configure, "stt_preload", True)  # even if the preload flag is left on
    monkeypatch.setattr(configure, "retention_days", 1)
    monkeypatch.setattr(configure, "webhook_outbox_worker", True)
    with TestClient(main_module.app) as client:
        assert client.get("/api/v1/ready").status_code == 200
    assert spies == []


def test_light_startup_still_refuses_a_plan_that_names_a_missing_model_profile(configure, monkeypatch, tmp_path):
    monkeypatch.setattr(configure, "light_mode", True)
    text = (BASE_DIR / "plans.yaml").read_text(encoding="utf-8")
    assert "llm_profile: premium" in text
    bad = tmp_path / "plans.yaml"
    bad.write_text(text.replace("llm_profile: premium", "llm_profile: does-not-exist"), encoding="utf-8")
    monkeypatch.setattr(configure, "plans_config_path", bad)
    plans_module._cached.cache_clear()
    with pytest.raises(ValueError, match="does-not-exist"):
        with TestClient(main_module.app):
            pass


def test_light_startup_still_checks_llm_allowed_providers(configure, monkeypatch, tmp_path):
    """A plan that promises 'anthropic only' while its model list also names OpenAI must stop the app from starting."""
    monkeypatch.setattr(configure, "light_mode", True)
    text = (BASE_DIR / "plans.yaml").read_text(encoding="utf-8")
    assert "llm_allowed_providers: [openai, anthropic]" in text
    bad = tmp_path / "plans.yaml"
    bad.write_text(
        text.replace("llm_allowed_providers: [openai, anthropic]", "llm_allowed_providers: [anthropic]", 1),
        encoding="utf-8",
    )
    monkeypatch.setattr(configure, "plans_config_path", bad)
    plans_module._cached.cache_clear()
    with pytest.raises(ValueError, match="only allows"):
        with TestClient(main_module.app):
            pass


def test_light_mode_does_not_relax_the_production_credential_checks():
    origins = "https://a.example"
    with pytest.raises(RuntimeError, match="default or a short key"):
        Settings(environment="production", light_mode=True, api_keys=DEFAULT_API_KEY, cors_origins=origins).validate_for_startup()
    with pytest.raises(RuntimeError, match="API_KEYS is empty"):
        Settings(environment="production", light_mode=True, api_keys="", cors_origins=origins).validate_for_startup()
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        Settings(environment="production", light_mode=True, api_keys="k" * 40, cors_origins="*").validate_for_startup()
    Settings(environment="production", light_mode=True, api_keys="k" * 40, cors_origins=origins).validate_for_startup()


# ----------------------------------------------------------------------------- the deploy config


def _fly(name):
    import tomllib

    with open(BASE_DIR / name, "rb") as handle:
        return tomllib.load(handle)


def test_the_light_app_is_a_separate_private_always_on_small_app():
    light, main = _fly("fly.light.toml"), _fly("fly.toml")
    assert light["app"] == "interviewer-ms-light" and light["app"] != main["app"]
    assert "http_service" not in light  # private only, like the main app
    service = light["services"][0]
    assert service["auto_stop_machines"] == "off" and service["min_machines_running"] == 1
    assert light["vm"][0] == {"size": "shared-cpu-1x", "memory": "512mb"}
    assert light["env"]["LIGHT_MODE"] == "true"
    assert light["build"] == main["build"]  # the same Dockerfile
    check_paths = {check["path"] for check in service["http_checks"]}
    assert check_paths == {"/api/v1/health", "/api/v1/ready"}


def test_the_main_app_config_does_not_turn_light_mode_on():
    assert "LIGHT_MODE" not in _fly("fly.toml")["env"]


def test_no_secret_is_written_into_the_light_config():
    text = (BASE_DIR / "fly.light.toml").read_text(encoding="utf-8")
    for name in ("API_KEYS =", "DATABASE_URL =", "REDIS_URL =", "WEBHOOK_SECRET =", "_API_KEY ="):
        assert name not in text
