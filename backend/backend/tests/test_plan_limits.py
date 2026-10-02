"""Packs, interview length, server-side time limit and per-answer caps (PLANS_ENABLED)."""

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
default_plan: economy
grace_seconds: 60
profiles:
  15: {question_count: 7, max_followups: 2}
  20: {question_count: 9, max_followups: 3}
plans:
  economy:
    pack_minutes: 30
    pack_days: 30
    durations: [15, 20]
    default_duration: 15
    llm_profile: economy
  premium:
    pack_minutes: 60
    pack_days: 30
    durations: [15, 20]
    default_duration: 15
    llm_profile: premium
    llm_allowed_providers: [anthropic, openai]
  strict:
    pack_minutes: 60
    pack_days: 30
    durations: [15, 20]
    default_duration: 15
    llm_profile: economy
    refund: {max_interviews_started: 1, max_minutes_used: 15, rule: all}
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
    monkeypatch.setattr(settings, "student_rate_limit_per_minute", 0)
    monkeypatch.setattr(settings, "student_daily_budget", 0)
    plans_module._cached.cache_clear()

    fake = FakeRouter()
    monkeypatch.setattr(interview_service_module, "get_router", lambda: fake)
    interview_service_module._service = None

    from app.main import app

    with TestClient(app) as test_client:
        db = storage_module.get_store().db
        db.execute("DELETE FROM packs")  # the test database is shared across tests
        db.execute("DELETE FROM pack_payments")
        test_client.fake_router = fake
        test_client.bought = set()
        yield test_client

    interview_service_module._service = None
    plans_module._cached.cache_clear()


def now_iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def buy(client, ref="stu-1", plan="economy", payment_id=None, days_ago=0, expect=200):
    payment_id = payment_id or f"pay-{ref}-{plan}-{len(client.bought)}"
    response = client.post(
        "/api/v1/packs/activate",
        json={"external_ref": ref, "plan": plan, "payment_id": payment_id, "purchased_at": now_iso(days_ago)},
        headers=HEADERS,
    )
    assert response.status_code == expect, response.text
    client.bought.add((ref, plan, payment_id))
    return response.json()


def create(client, ref="stu-1", plan=None, expect=201, paid=True, **overrides):
    """Create an interview; by default the student first buys the plan's pack (once per test)."""
    plan_name = plan or "economy"
    if paid and not any(r == ref and p == plan_name for r, p, _ in client.bought):
        buy(client, ref, plan_name)
    payload = {
        "role": "Backend Engineer",
        "resume_text": "Senior backend engineer. Python, FastAPI, PostgreSQL. Reduced latency by 40 percent.",
        "jd_text": "Require Python and Kafka. 5+ years experience.",
        "external_ref": ref,
    }
    if plan:
        payload["plan"] = plan
    payload.update(overrides)
    response = client.post("/api/v1/interviews", json=payload, headers=HEADERS)
    assert response.status_code == expect, response.text
    return response.json()


def pack(client, ref="stu-1", plan="economy"):
    response = client.get(f"/api/v1/packs/{ref}", headers=HEADERS)
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["packs"] if item["plan"] == plan)


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


# ----------------------------------------------------------------------------- no pack, no interview


def test_a_student_without_a_pack_cannot_start_an_interview(client):
    refused = create(client, paid=False, expect=402)
    error = refused["error"]
    assert error["code"] == "quota_exceeded"
    assert error["details"]["reason"] == "no_active_pack"
    assert error["details"]["remaining_minutes"] == 0
    assert error["details"]["days_remaining"] == 0


def test_a_pack_for_another_plan_does_not_count(client):
    buy(client, plan="economy")
    refused = create(client, plan="premium", paid=False, expect=402)
    assert refused["error"]["details"]["reason"] == "no_active_pack"
    assert refused["error"]["details"]["plan"] == "premium"


def test_an_expired_pack_is_refused(client):
    buy(client, days_ago=40)  # bought 40 days ago, 30 day pack
    refused = create(client, paid=False, expect=402)
    assert refused["error"]["details"]["reason"] == "pack_expired"
    assert pack(client)["active"] is False


def test_unknown_plan_is_refused(client):
    create(client, plan="platinum", paid=False, expect=422)


# ----------------------------------------------------------------------------- spending minutes


def test_minutes_are_spent_per_booking_until_the_pack_is_used_up(client):
    create(client)  # 15 of 30
    create(client)  # 30 of 30
    refused = create(client, expect=402)
    error = refused["error"]
    assert error["details"]["reason"] == "no_minutes_left"
    assert error["details"]["requested_minutes"] == 15
    assert error["details"]["remaining_minutes"] == 0


def test_a_longer_interview_that_does_not_fit_is_refused_but_a_shorter_one_still_fits(client):
    create(client, config={"duration_minutes": 20})  # 20 of 30, 10 left
    refused = create(client, config={"duration_minutes": 20}, expect=402)
    assert refused["error"]["details"]["reason"] == "insufficient_minutes"
    assert refused["error"]["details"]["remaining_minutes"] == 10
    create(client, config={"duration_minutes": 15}, expect=402)  # 15 > 10 left


def test_each_student_and_each_plan_has_their_own_balance(client):
    create(client, ref="stu-1")
    create(client, ref="stu-1")
    create(client, ref="stu-2")  # stu-1 being out of minutes does not affect stu-2
    create(client, ref="stu-1", plan="premium")  # nor does it affect stu-1's separate premium pack
    assert pack(client, "stu-1", "premium")["minutes_used"] == 15


def test_failed_question_generation_gives_the_minutes_back(client):
    buy(client)
    client.fake_router.overrides["question_generation"] = {"questions": []}
    create(client, expect=422)
    client.fake_router.overrides.pop("question_generation")
    state = pack(client)
    assert (state["minutes_used"], state["interviews_started"]) == (0, 0)
    create(client)
    create(client)  # both bookings still fit: nothing was lost to the failure


# ----------------------------------------------------------------------------- length and deadline


def test_default_length_comes_from_the_plan_and_sets_a_deadline(client):
    before = datetime.now(timezone.utc)
    status = create(client)["interview"]
    assert status["duration_minutes"] == 15
    deadline = datetime.fromisoformat(status["deadline_at"])
    assert before + timedelta(minutes=15, seconds=55) < deadline < before + timedelta(minutes=16, seconds=30)


def test_status_reports_time_remaining_so_a_client_can_run_a_countdown(client):
    """seconds_remaining is computed by the server, so a browser with a wrong clock still counts down correctly."""
    status = create(client)["interview"]
    assert status["grace_seconds"] == 60
    assert 15 * 60 + 60 - 5 <= status["seconds_remaining"] <= 15 * 60 + 60

    interview_id = status["id"]
    polled = client.get(f"/api/v1/interviews/{interview_id}/status", headers=HEADERS).json()
    assert polled["seconds_remaining"] <= status["seconds_remaining"]
    assert polled["grace_seconds"] == 60

    _expire(interview_id)
    expired = client.get(f"/api/v1/interviews/{interview_id}/status", headers=HEADERS).json()
    assert expired["seconds_remaining"] == 0  # never negative


def test_chosen_length_must_be_allowed_and_costs_nothing_when_refused(client):
    buy(client)
    create(client, config={"duration_minutes": 25}, expect=422)
    assert pack(client)["minutes_used"] == 0


def test_length_profile_overrides_the_requested_question_count(client):
    body = create(client, config={"duration_minutes": 20, "question_count": 15, "max_followups": 15})
    config = stored(body["interview"]["id"])["config"]
    assert config["question_count"] == 9
    assert config["max_followups"] == 3
    prompt = " ".join(m.content for m in client.fake_router.calls[-1]["messages"])
    assert "Create 9 behavioural" in prompt


# ----------------------------------------------------------------------------- packs API


def test_activation_creates_a_pack_with_minutes_and_days_left(client):
    result = buy(client, payment_id="evt_1")
    assert result["applied"] is True
    state = result["pack"]
    assert state["plan"] == "economy"
    assert state["active"] is True
    assert state["minutes_total"] == 30 and state["minutes_remaining"] == 30
    assert state["days_remaining"] == 30
    assert datetime.fromisoformat(state["expires_at"]) > datetime.now(timezone.utc) + timedelta(days=29)


def test_the_same_payment_is_applied_only_once(client):
    buy(client, payment_id="evt_1")
    again = buy(client, payment_id="evt_1")
    assert again["applied"] is False
    assert pack(client)["minutes_total"] == 30


def test_buying_again_stacks_minutes_and_extends_the_expiry(client):
    buy(client, payment_id="evt_1")
    create(client)  # uses 15
    result = buy(client, payment_id="evt_2")
    state = result["pack"]
    assert state["minutes_total"] == 60
    assert state["minutes_remaining"] == 45
    assert state["days_remaining"] == 60  # 30 left plus 30 more


def test_buying_after_the_pack_is_used_up_starts_a_fresh_pack(client):
    create(client)
    create(client)  # all 30 minutes used
    state = buy(client, payment_id="evt_again")["pack"]
    assert state["minutes_total"] == 30 and state["minutes_used"] == 0
    assert state["days_remaining"] == 30  # counted from this purchase, not stacked


def test_balance_lists_every_plan(client):
    buy(client, plan="premium")
    response = client.get("/api/v1/packs/stu-1", headers=HEADERS).json()
    assert response["external_ref"] == "stu-1"
    by_plan = {item["plan"]: item for item in response["packs"]}
    assert set(by_plan) == {"economy", "premium", "strict"}
    assert by_plan["economy"]["active"] is False and by_plan["economy"]["minutes_remaining"] == 0
    assert by_plan["premium"]["active"] is True and by_plan["premium"]["minutes_remaining"] == 60


def test_activation_validates_its_input(client):
    base = {"external_ref": "stu-1", "plan": "economy", "payment_id": "p1"}
    bad_plan = client.post("/api/v1/packs/activate", json={**base, "plan": "gold", "purchased_at": now_iso()}, headers=HEADERS)
    assert bad_plan.status_code == 422
    naive = client.post("/api/v1/packs/activate", json={**base, "purchased_at": "2026-10-02T12:00:00"}, headers=HEADERS)
    assert naive.status_code == 422  # no timezone
    future = client.post(
        "/api/v1/packs/activate", json={**base, "purchased_at": now_iso(days_ago=-2)}, headers=HEADERS
    )
    assert future.status_code == 422
    missing = client.post("/api/v1/packs/activate", json={"plan": "economy"}, headers=HEADERS)
    assert missing.status_code == 422


def test_pack_endpoints_need_an_api_key(client):
    assert client.get("/api/v1/packs/stu-1").status_code == 401
    assert client.post("/api/v1/packs/activate", json={}).status_code == 401
    assert client.post("/api/v1/packs/revoke", json={}).status_code == 401


# ----------------------------------------------------------------------------- refunds


def revoke(client, ref="stu-1", plan="economy", payment_id="evt_1", expect=200):
    response = client.post(
        "/api/v1/packs/revoke",
        json={"external_ref": ref, "plan": plan, "payment_id": payment_id},
        headers=HEADERS,
    )
    assert response.status_code == expect, response.text
    return response.json()


def test_an_unused_pack_can_be_refunded(client):
    buy(client, payment_id="evt_1")
    body = revoke(client)
    assert body["revoked"] is True
    assert body["pack"]["minutes_remaining"] == 0
    assert body["pack"]["active"] is False
    refused = create(client, paid=False, expect=402)
    assert refused["error"]["details"]["reason"] == "no_active_pack"  # a refunded pack is gone, not "expired"


def test_refund_is_allowed_after_the_first_interview_and_refused_after_the_second(client):
    buy(client, payment_id="evt_1")
    create(client)  # one interview started: still refundable
    assert pack(client)["refund_eligible"] is True
    create(client)  # second interview started, 30 minutes used
    assert pack(client)["refund_eligible"] is False
    refused = revoke(client, expect=409)
    assert refused["error"]["code"] == "refund_not_allowed"
    assert pack(client)["minutes_total"] == 30  # untouched


def test_one_long_first_interview_is_still_refundable_when_either_condition_is_enough(client):
    buy(client, payment_id="evt_1")
    create(client, config={"duration_minutes": 20})  # one interview started, but 20 > 15 minutes used
    assert pack(client)["refund_eligible"] is True  # "before the 2nd interview" still holds
    revoke(client)


def test_a_plan_can_require_both_refund_conditions(client):
    buy(client, plan="strict", payment_id="evt_s")
    create(client, plan="strict", config={"duration_minutes": 20})  # first interview, but over 15 minutes
    assert pack(client, plan="strict")["refund_eligible"] is False
    refused = revoke(client, plan="strict", payment_id="evt_s", expect=409)
    assert refused["error"]["details"]["rule"] == "all"


def test_refunding_twice_is_harmless(client):
    buy(client, payment_id="evt_1")
    revoke(client)
    again = revoke(client)
    assert again["already_revoked"] is True


def test_refund_of_an_unknown_payment_is_not_found(client):
    buy(client, payment_id="evt_1")
    revoke(client, payment_id="evt_nope", expect=404)
    revoke(client, ref="stu-2", payment_id="evt_1", expect=404)  # someone else's payment


# ----------------------------------------------------------------------------- which models serve which plan


def _run_whole_interview(client, plan):
    interview_id = create(client, plan=plan)["interview"]["id"]
    for index in (0, 1):
        answered = client.post(
            f"/api/v1/interviews/{interview_id}/answers",
            json={"question_index": index, "transcript": ANSWER},
            headers=HEADERS,
        )
        assert answered.status_code == 200, answered.text
    finished = client.post(f"/api/v1/interviews/{interview_id}/finish", headers=HEADERS)
    assert finished.status_code == 200, finished.text
    return interview_id


def test_economy_interviews_use_the_economy_profile_for_every_ai_call(client):
    _run_whole_interview(client, plan=None)
    calls = client.fake_router.calls
    assert {c["task"] for c in calls} >= {"question_generation", "answer_analysis", "final_scoring", "report_narrative"}
    assert all(c["profile"] == "economy" for c in calls)
    assert all(c["authorized_providers"] is None for c in calls)  # no extra restriction: DeepSeek first, as configured


def test_premium_interviews_use_the_premium_profile_and_us_providers_only_for_every_ai_call(client):
    """From the resume summary to the final narrative, nothing in a Premium interview may reach DeepSeek."""
    _run_whole_interview(client, plan="premium")
    calls = client.fake_router.calls
    assert {c["task"] for c in calls} >= {
        "resume_summary", "question_generation", "answer_analysis", "followup_generation",
        "final_scoring", "tips_generation", "report_narrative",
    }
    assert all(c["profile"] == "premium" for c in calls), [(c["task"], c["profile"]) for c in calls]
    assert all(c["authorized_providers"] == ("anthropic", "openai") for c in calls)


def test_the_profile_an_interview_started_with_follows_it_to_the_end(client):
    """A student's later calls use their plan from when the interview began, not the default."""
    economy_id = create(client)["interview"]["id"]
    premium_id = create(client, plan="premium")["interview"]["id"]
    assert stored(economy_id)["config"]["llm_profile"] == "economy"
    assert stored(premium_id)["config"]["llm_profile"] == "premium"
    assert stored(premium_id)["config"]["plan"] == "premium"


def test_the_report_records_which_profile_served_it(client):
    interview_id = _run_whole_interview(client, plan="premium")
    report = client.get(f"/api/v1/interviews/{interview_id}/report", headers=HEADERS).json()["report"]
    assert report["routing_trace"]["final_scoring"]["profile"] == "premium"


def test_the_service_refuses_to_start_when_plans_and_models_disagree(monkeypatch, tmp_path):
    bad = tmp_path / "plans.yaml"
    bad.write_text(PLANS_YAML.replace("llm_profile: premium", "llm_profile: does-not-exist"), encoding="utf-8")
    settings = get_settings()
    monkeypatch.setattr(settings, "plans_enabled", True)
    monkeypatch.setattr(settings, "plans_config_path", bad)
    plans_module._cached.cache_clear()
    from app.main import app

    with pytest.raises(ValueError, match="does-not-exist"):
        with TestClient(app):
            pass
    plans_module._cached.cache_clear()


# ----------------------------------------------------------------------------- plan catalog


def test_the_catalog_lists_each_plan_with_its_pack_and_the_ai_providers_it_may_use(client):
    body = client.get("/api/v1/plans", headers=HEADERS).json()
    assert body["default_plan"] == "economy"
    by_name = {plan["name"]: plan for plan in body["plans"]}
    assert set(by_name) == {"economy", "premium", "strict"}
    premium = by_name["premium"]
    assert (premium["pack_minutes"], premium["pack_days"]) == (60, 30)
    assert premium["durations"] == [15, 20]
    assert premium["refund"] == {"max_interviews_started": 1, "max_minutes_used": 15, "rule": "any"}
    # these are the companies that may see a student's data on each plan: it is what a consent screen must name
    assert set(premium["llm_providers"]) == {"anthropic", "openai"}
    assert "deepseek" in by_name["economy"]["llm_providers"]
    assert "deepseek" not in premium["llm_providers"]


def test_the_catalog_leaves_out_providers_that_are_switched_off(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_disabled_providers", "deepseek,openrouter")
    economy = next(p for p in client.get("/api/v1/plans", headers=HEADERS).json()["plans"] if p["name"] == "economy")
    assert "deepseek" not in economy["llm_providers"] and "openrouter" not in economy["llm_providers"]
    assert "openai" in economy["llm_providers"]


def test_the_catalog_needs_an_api_key_and_plans_enabled(client, monkeypatch):
    assert client.get("/api/v1/plans").status_code == 401
    monkeypatch.setattr(get_settings(), "plans_enabled", False)
    assert client.get("/api/v1/plans", headers=HEADERS).status_code == 404


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
    assert status["seconds_remaining"] is None
    assert status["grace_seconds"] is None
    assert client.get("/api/v1/packs/stu-1", headers=HEADERS).status_code == 404
