"""The operator-grant wallets in the SHIPPED plans.yaml (admin_grant_economy / admin_grant_premium).

The cockpit's "Grant free minutes" action activates one of these skus. Each is its own hidden wallet, so a
grant never stretches the expiry of a paid pack, and /packs/revoke always works on it.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import BASE_DIR, get_settings
from app.core import plans as plans_module
from app.services import interview_service as interview_service_module
from app.services import storage as storage_module
from tests.fakes import FakeRouter
from tests.test_plan_limits import HEADERS, buy_sku, create, expire, now_iso, revoke, wallets


@pytest.fixture()
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "plans_enabled", True)
    monkeypatch.setattr(settings, "plans_config_path", BASE_DIR / "plans.yaml")
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
        db.execute("DELETE FROM packs")
        db.execute("DELETE FROM pack_payments")
        test_client.fake_router = fake
        test_client.bought = set()
        yield test_client

    interview_service_module._service = None
    plans_module._cached.cache_clear()


@pytest.mark.parametrize(
    ("sku", "plan", "profile"),
    [
        ("admin_grant_60_economy", "admin_grant_economy", "economy"),
        ("admin_grant_60_premium", "admin_grant_premium", "premium"),
    ],
)
def test_an_admin_grant_activates_a_60_minute_14_day_wallet(client, sku, plan, profile):
    body = buy_sku(client, sku, payment_id="admin:11111111-1111-1111-1111-111111111111")
    assert body["applied"] is True
    assert body["pack"]["plan"] == plan
    assert body["pack"]["minutes_total"] == 60 and body["pack"]["minutes_remaining"] == 60
    assert body["pack"]["days_remaining"] in (13, 14)
    assert body["pack"]["allowed_durations"] == [15, 20, 25]


def test_an_admin_grant_never_touches_a_paid_pack(client):
    buy_sku(client, "addon_5", payment_id="pi_1")
    before = wallets(client)["economy"]
    buy_sku(client, "admin_grant_60_economy", payment_id="admin:22222222-2222-2222-2222-222222222222")
    assert wallets(client)["economy"] == before


def test_a_replayed_admin_grant_applies_nothing(client):
    first = buy_sku(client, "admin_grant_60_economy", payment_id="admin:33333333-3333-3333-3333-333333333333")
    again = buy_sku(client, "admin_grant_60_economy", payment_id="admin:33333333-3333-3333-3333-333333333333")
    assert first["applied"] is True and again["applied"] is False
    assert again["pack"]["minutes_total"] == 60


def test_an_admin_grant_is_booked_on_its_own_wallet(client):
    buy_sku(client, "admin_grant_60_premium", payment_id="admin:44444444-4444-4444-4444-444444444444")
    created = create(client, plan="admin_grant_premium", paid=False, config={"duration_minutes": 20})
    config = storage_module.get_store().get_interview(created["interview"]["id"])["config"]
    assert (config["plan"], config["llm_profile"]) == ("admin_grant_premium", "premium")
    assert wallets(client)["admin_grant_premium"]["minutes_remaining"] == 40


def test_an_admin_grant_can_be_revoked_even_after_use(client):
    """The refund rule is wide open: /packs/revoke must always work so a mistaken grant can be undone."""
    buy_sku(client, "admin_grant_60_economy", payment_id="admin:55555555-5555-5555-5555-555555555555")
    create(client, plan="admin_grant_economy", paid=False)
    body = revoke(client, plan="admin_grant_economy", payment_id="admin:55555555-5555-5555-5555-555555555555")
    assert body["revoked"] is True


def test_expiring_an_admin_grant_leaves_other_wallets_alone(client):
    buy_sku(client, "addon_5", payment_id="pi_1")
    buy_sku(client, "admin_grant_60_economy", payment_id="admin:66666666-6666-6666-6666-666666666666")
    assert expire(client, plan="admin_grant_economy")["expired"] is True
    assert wallets(client)["economy"]["active"] is True


def test_admin_grants_are_not_listed_as_things_to_buy(client):
    listing = client.get("/api/v1/plans", headers=HEADERS).json()
    assert not {"admin_grant_60_economy", "admin_grant_60_premium"} & {s["name"] for s in listing["skus"]}
    assert not {"admin_grant_economy", "admin_grant_premium"} & {p["name"] for p in listing["plans"]}
    buy_sku(client, "admin_grant_60_economy", payment_id="admin:77777777-7777-7777-7777-777777777777")
    assert "admin_grant_economy" in wallets(client)  # but the wallet shows in the student's balances
