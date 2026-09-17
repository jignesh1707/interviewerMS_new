import pytest

from app.llm.router import ModelRouter, RouterConfig
from app.llm.router import ModelCandidate
from app.core.errors import AllProvidersFailedError
from app.llm.providers.base import LLMMessage, ProviderCallError, ProviderResponse
from app.config import Settings


class FakeProvider:
    def __init__(self, name: str, *, fail: bool = False, auth_error: bool = False) -> None:
        self.name = name
        self.configured = True
        self.fail = fail
        self.auth_error = auth_error
        self.calls = 0

    async def complete(self, messages, model, temperature, max_tokens):
        self.calls += 1
        if self.fail:
            raise ProviderCallError(
                f"{self.name} down",
                retryable=not self.auth_error,
                auth_error=self.auth_error,
                status_code=503 if not self.auth_error else 401,
            )
        return ProviderResponse(text=f"ok:{self.name}", model=model, input_tokens=100, output_tokens=50)


def build_router(**overrides):
    settings = Settings(
        provider_cooldown_seconds=60.0,
        llm_max_attempts_per_provider=1,
        llm_timeout_seconds=5.0,
        **overrides,
    )
    config = RouterConfig(
        tiers={
            "cheap": [
                ModelCandidate(provider="deepseek", model="deepseek-chat", input_price=0.27, output_price=1.10),
                ModelCandidate(provider="openai", model="gpt-4o-mini", input_price=0.15, output_price=0.60),
            ],
            "premium": [
                ModelCandidate(provider="anthropic", model="claude-3-7-sonnet-latest", input_price=3.0, output_price=15.0),
            ],
        },
        tasks={"final_scoring": "premium", "question_generation": "cheap"},
        default_tier="cheap",
    )
    return ModelRouter(settings=settings, config=config)


def messages():
    return [LLMMessage(role="user", content="hi")]


@pytest.mark.asyncio
async def test_simple_task_uses_cheapest_tier():
    router = build_router()
    router._providers = {"deepseek": FakeProvider("deepseek"), "openai": FakeProvider("openai"), "anthropic": FakeProvider("anthropic")}
    result = await router.complete("question_generation", messages())
    assert router.config.tier_for_task("question_generation") == "cheap"
    assert result.provider == "deepseek"
    assert result.tier == "cheap"
    assert result.fallback_used is False
    assert router.usage.calls == 1


@pytest.mark.asyncio
async def test_complex_task_uses_premium_tier():
    router = build_router()
    router._providers = {"deepseek": FakeProvider("deepseek"), "openai": FakeProvider("openai"), "anthropic": FakeProvider("anthropic")}
    result = await router.complete("final_scoring", messages())
    assert result.tier == "premium"
    assert result.provider == "anthropic"


@pytest.mark.asyncio
async def test_falls_back_to_second_provider_when_cheapest_down():
    router = build_router()
    deepseek = FakeProvider("deepseek", fail=True)
    openai = FakeProvider("openai")
    router._providers = {"deepseek": deepseek, "openai": openai, "anthropic": FakeProvider("anthropic")}

    result = await router.complete("question_generation", messages())
    assert result.provider == "openai"
    assert result.fallback_used is True
    assert deepseek.calls == 1
    assert router.health("deepseek").consecutive_failures == 1
    assert router.health("deepseek").is_available(__import__("time").monotonic()) is False


@pytest.mark.asyncio
async def test_circuit_breaker_skips_down_provider_on_next_call():
    router = build_router()
    deepseek = FakeProvider("deepseek", fail=True)
    router._providers = {"deepseek": deepseek, "openai": FakeProvider("openai"), "anthropic": FakeProvider("anthropic")}

    await router.complete("question_generation", messages())
    await router.complete("question_generation", messages())
    assert deepseek.calls == 1


@pytest.mark.asyncio
async def test_all_providers_failed_raises():
    router = build_router()
    router._providers = {
        "deepseek": FakeProvider("deepseek", fail=True),
        "openai": FakeProvider("openai", fail=True),
        "anthropic": FakeProvider("anthropic", fail=True),
    }
    with pytest.raises(AllProvidersFailedError) as exc:
        await router.complete("question_generation", messages())
    assert len(exc.value.details["attempts"]) >= 2


@pytest.mark.asyncio
async def test_unconfigured_providers_are_skipped():
    router = build_router()
    class Unconfigured(FakeProvider):
        def __init__(self):
            super().__init__("deepseek")
            self.configured = False

    router._providers = {"deepseek": Unconfigured(), "openai": FakeProvider("openai"), "anthropic": FakeProvider("anthropic")}
    result = await router.complete("question_generation", messages())
    assert result.provider == "openai"


@pytest.mark.asyncio
async def test_cost_estimation_recorded():
    router = build_router()
    router._providers = {"deepseek": FakeProvider("deepseek"), "openai": FakeProvider("openai"), "anthropic": FakeProvider("anthropic")}
    result = await router.complete("final_scoring", messages())
    expected = 100 / 1_000_000 * 3.0 + 50 / 1_000_000 * 15.0
    assert result.estimated_cost_usd == pytest.approx(expected)
    assert router.usage.estimated_cost_usd == pytest.approx(expected)
