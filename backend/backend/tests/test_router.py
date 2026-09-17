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
                ModelCandidate(provider="openai", model="gpt-4o-mini", input_price=0.15, output_price=0.60, priority=0),
                ModelCandidate(provider="deepseek", model="deepseek-chat", input_price=0.27, output_price=1.10, priority=1),
                ModelCandidate(provider="anthropic", model="claude-3-5-haiku-latest", input_price=0.80, output_price=4.00, priority=2),
            ],
            "premium": [
                ModelCandidate(provider="openai", model="gpt-4o", input_price=2.50, output_price=10.00, priority=0),
                ModelCandidate(provider="deepseek", model="deepseek-reasoner", input_price=0.55, output_price=2.19, priority=1),
                ModelCandidate(provider="anthropic", model="claude-3-7-sonnet-latest", input_price=3.0, output_price=15.0, priority=2),
            ],
        },
        tasks={"final_scoring": "premium", "question_generation": "cheap"},
        default_tier="cheap",
    )
    return ModelRouter(settings=settings, config=config)


def messages():
    return [LLMMessage(role="user", content="hi")]


def wired(router, **providers):
    defaults = {
        "openai": FakeProvider("openai"),
        "deepseek": FakeProvider("deepseek"),
        "anthropic": FakeProvider("anthropic"),
    }
    defaults.update(providers)
    router._providers = defaults
    return router


@pytest.mark.asyncio
async def test_simple_task_uses_openai_first():
    router = wired(build_router())
    result = await router.complete("question_generation", messages())
    assert router.config.tier_for_task("question_generation") == "cheap"
    assert result.provider == "openai"
    assert result.tier == "cheap"
    assert result.fallback_used is False
    assert router.usage.calls == 1


@pytest.mark.asyncio
async def test_complex_task_uses_premium_tier_openai_first():
    router = wired(build_router())
    result = await router.complete("final_scoring", messages())
    assert result.tier == "premium"
    assert result.provider == "openai"


@pytest.mark.asyncio
async def test_falls_back_to_deepseek_when_openai_down():
    router = build_router()
    openai = FakeProvider("openai", fail=True)
    deepseek = FakeProvider("deepseek")
    wired(router, openai=openai, deepseek=deepseek)

    result = await router.complete("question_generation", messages())
    assert result.provider == "deepseek"
    assert result.fallback_used is True
    assert openai.calls == 1
    assert router.health("openai").consecutive_failures == 1
    assert router.health("openai").is_available(__import__("time").monotonic()) is False


@pytest.mark.asyncio
async def test_falls_back_to_anthropic_when_openai_and_deepseek_down():
    router = build_router()
    openai = FakeProvider("openai", fail=True)
    deepseek = FakeProvider("deepseek", fail=True)
    anthropic = FakeProvider("anthropic")
    wired(router, openai=openai, deepseek=deepseek, anthropic=anthropic)

    result = await router.complete("question_generation", messages())
    assert result.provider == "anthropic"
    assert result.fallback_used is True
    assert openai.calls == 1
    assert deepseek.calls == 1


@pytest.mark.asyncio
async def test_circuit_breaker_skips_down_provider_on_next_call():
    router = build_router()
    openai = FakeProvider("openai", fail=True)
    wired(router, openai=openai)

    await router.complete("question_generation", messages())
    await router.complete("question_generation", messages())
    assert openai.calls == 1


@pytest.mark.asyncio
async def test_all_providers_failed_raises():
    router = wired(
        build_router(),
        openai=FakeProvider("openai", fail=True),
        deepseek=FakeProvider("deepseek", fail=True),
        anthropic=FakeProvider("anthropic", fail=True),
    )
    with pytest.raises(AllProvidersFailedError) as exc:
        await router.complete("question_generation", messages())
    assert len(exc.value.details["attempts"]) >= 2


@pytest.mark.asyncio
async def test_unconfigured_providers_are_skipped():
    router = build_router()

    class Unconfigured(FakeProvider):
        def __init__(self):
            super().__init__("openai")
            self.configured = False

    wired(router, openai=Unconfigured())
    result = await router.complete("question_generation", messages())
    assert result.provider == "deepseek"


@pytest.mark.asyncio
async def test_cost_estimation_recorded():
    router = wired(build_router())
    result = await router.complete("final_scoring", messages())
    expected = 100 / 1_000_000 * 2.50 + 50 / 1_000_000 * 10.00
    assert result.estimated_cost_usd == pytest.approx(expected)
    assert router.usage.estimated_cost_usd == pytest.approx(expected)


def test_yaml_order_is_openai_then_deepseek_then_anthropic_even_if_later_is_cheaper():
    config = RouterConfig(
        tiers={
            "cheap": [
                ModelCandidate(provider="openai", model="gpt-4o-mini", input_price=2.50, output_price=10.00),
                ModelCandidate(provider="deepseek", model="deepseek-chat", input_price=0.27, output_price=1.10),
                ModelCandidate(provider="anthropic", model="claude-3-5-haiku-latest", input_price=0.15, output_price=0.60),
            ]
        }
    )
    assert [candidate.provider for candidate in config.candidates("cheap")] == ["openai", "deepseek", "anthropic"]


def test_priority_overrides_yaml_order():
    config = RouterConfig(
        tiers={
            "cheap": [
                ModelCandidate(provider="openai", model="gpt-4o-mini", priority=2),
                ModelCandidate(provider="deepseek", model="deepseek-chat", priority=0),
                ModelCandidate(provider="anthropic", model="claude-3-5-haiku-latest", priority=1),
            ]
        }
    )
    assert [candidate.provider for candidate in config.candidates("cheap")] == ["deepseek", "anthropic", "openai"]


def test_production_models_yaml_uses_openai_then_deepseek_then_anthropic():
    from pathlib import Path

    from app.llm.router import load_router_config

    config = load_router_config(Path(__file__).resolve().parents[1] / "models.yaml")
    for tier in ("cheap", "standard", "premium"):
        assert [candidate.provider for candidate in config.candidates(tier)] == [
            "openai",
            "deepseek",
            "anthropic",
        ]
