import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core import ratelimit
from app.core.errors import RateLimitError
from app.services import interview_service as interview_service_module
from tests.fakes import FakeRouter

KEY_A = "a" * 40
KEY_B = "b" * 40
HEADERS_A = {"X-API-Key": KEY_A}
HEADERS_B = {"X-API-Key": KEY_B}


@pytest.fixture()
def redis_server(monkeypatch):
    """One shared fake Redis, handed to every 'machine' (client) like Upstash would be."""
    server = fakeredis.FakeServer()
    monkeypatch.setattr(get_settings(), "redis_url", "redis://fake:6379/0")
    monkeypatch.setattr(
        ratelimit, "_make_client", lambda url: fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    )
    ratelimit._clients.clear()
    ratelimit._scripts.clear()
    yield server
    ratelimit._clients.clear()
    ratelimit._scripts.clear()


async def test_limit_enforced_through_redis_with_retry_after(redis_server):
    shared = ratelimit.SharedLimiter()
    for _ in range(3):
        await shared.check("k", 3)
    with pytest.raises(RateLimitError) as excinfo:
        await shared.check("k", 3)
    assert int(excinfo.value.headers["Retry-After"]) >= 1
    assert shared.memory._hits == {}  # nothing fell back to memory


async def test_two_machines_share_one_counter(redis_server):
    machine_a, machine_b = ratelimit.SharedLimiter(), ratelimit.SharedLimiter()
    await machine_a.check("tenant", 2)
    await machine_b.check("tenant", 2)
    with pytest.raises(RateLimitError):
        await machine_a.check("tenant", 2)
    with pytest.raises(RateLimitError):
        await machine_b.check("tenant", 2)


async def test_keys_are_prefixed_and_expire(redis_server):
    import fakeredis as fr

    await ratelimit.SharedLimiter().check("tenant:x", 5)
    client = fr.FakeAsyncRedis(server=redis_server, decode_responses=True)
    keys = await client.keys("*")
    assert len(keys) == 1 and keys[0].startswith("interviewer:rl:tenant:x:")
    assert 0 < await client.ttl(keys[0]) <= 61


async def test_failed_auth_counters_via_redis(redis_server):
    shared = ratelimit.SharedLimiter()
    assert await shared.is_blocked("authfail:1.2.3.4", 2) is False
    await shared.record("authfail:1.2.3.4")
    await shared.record("authfail:1.2.3.4")
    assert await shared.is_blocked("authfail:1.2.3.4", 2) is True
    assert await shared.is_blocked("authfail:other", 2) is False


async def test_daily_budget_shared_across_machines(redis_server):
    machine_a, machine_b = ratelimit.SharedBudget(), ratelimit.SharedBudget()
    await machine_a.consume("tenant", 2)
    await machine_b.consume("tenant", 2)
    with pytest.raises(RateLimitError, match="budget"):
        await machine_a.consume("tenant", 2)
    await machine_b.consume("other-tenant", 2)  # separate tenant unaffected


async def test_redis_outage_falls_back_to_memory_instead_of_failing(monkeypatch):
    class Down:
        async def __call__(self, *args, **kwargs):
            raise ConnectionError("upstash unreachable")

        async def get(self, *args, **kwargs):
            raise ConnectionError("upstash unreachable")

    monkeypatch.setattr(get_settings(), "redis_url", "redis://fake:6379/0")
    ratelimit._clients.clear()
    ratelimit._scripts.clear()
    monkeypatch.setattr(ratelimit, "_make_client", lambda url: type("C", (), {"register_script": lambda self, s: Down(), "get": Down().get})())
    shared = ratelimit.SharedLimiter()
    await shared.check("k", 2)
    await shared.check("k", 2)
    with pytest.raises(RateLimitError):  # still limited, per process
        await shared.check("k", 2)
    assert await shared.is_blocked("k", 2) is True
    budget = ratelimit.SharedBudget()
    await budget.consume("t", 1)
    with pytest.raises(RateLimitError):
        await budget.consume("t", 1)
    ratelimit._clients.clear()
    ratelimit._scripts.clear()


async def test_redis_status_reports_disabled_ok_and_unavailable(monkeypatch, redis_server):
    assert await ratelimit.redis_status() == "ok"
    monkeypatch.setattr(get_settings(), "redis_url", "")
    assert await ratelimit.redis_status() == "disabled"


@pytest.fixture()
def client(monkeypatch, redis_server):
    settings = get_settings()
    monkeypatch.setattr(settings, "api_keys", f"ra:{KEY_A},rb:{KEY_B}")
    monkeypatch.setattr(settings, "rate_limit_per_minute", 4)
    monkeypatch.setattr(settings, "rate_limit_expensive_per_minute", 10_000)
    monkeypatch.setattr(settings, "daily_expensive_budget", 0)
    fake = FakeRouter()
    monkeypatch.setattr(interview_service_module, "get_router", lambda: fake)
    interview_service_module._service = None

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    interview_service_module._service = None


def test_api_rate_limit_uses_redis_and_is_per_tenant(client):
    statuses = [client.get("/api/v1/interviews", headers=HEADERS_A).status_code for _ in range(5)]
    assert statuses == [200, 200, 200, 200, 429]
    assert client.get("/api/v1/interviews", headers=HEADERS_B).status_code == 200


def test_ready_details_reports_redis_and_database(client):
    body = client.get("/api/v1/ready/details", headers=HEADERS_B).json()
    assert body["redis"] == "ok"
    assert body["database"] in {"sqlite", "postgres"}
