"""`python -m app.llm_smoke`: send one tiny request to every configured model, to check keys, model names and options."""

import pytest

from app.config import BASE_DIR, Settings
from app.llm.providers.base import ProviderCallError, ProviderResponse
from app.llm.router import ModelRouter, load_router_config
from app.llm_smoke import render, smoke


class Provider:
    def __init__(self, name, *, configured=True, fail_models=()):
        self.name = name
        self.configured = configured
        self.fail_models = set(fail_models)
        self.calls = []

    async def complete(self, messages, model, temperature, max_tokens, options=None):
        self.calls.append({"model": model, "temperature": temperature, "options": options})
        if model in self.fail_models:
            raise ProviderCallError(f"{self.name} rejected {model}: HTTP 400", retryable=False, status_code=400)
        return ProviderResponse(text='{"ok": true}', model=model, input_tokens=12, output_tokens=5)


def build(**providers):
    router = ModelRouter(settings=Settings(), config=load_router_config(BASE_DIR / "models.yaml"))
    defaults = {name: Provider(name, configured=False) for name in ("openai", "deepseek", "openrouter", "anthropic")}
    defaults.update(providers)
    router._providers = defaults
    return router, defaults


@pytest.mark.asyncio
async def test_only_configured_providers_are_called():
    router, providers = build(anthropic=Provider("anthropic"))
    results = await smoke(router, profile="premium")
    assert providers["openai"].calls == []
    assert results and {r["provider"] for r in results if r["status"] == "ok"} == {"anthropic"}
    skipped = [r for r in results if r["status"] == "skipped"]
    assert {r["provider"] for r in skipped} == {"openai"}  # listed, but not called: no key


@pytest.mark.asyncio
async def test_every_distinct_model_in_the_profile_is_tried_with_its_options():
    router, providers = build(anthropic=Provider("anthropic"))
    await smoke(router, profile="premium")
    sent = {call["model"]: call["options"] for call in providers["anthropic"].calls}
    assert set(sent) == {"claude-haiku-4-5", "claude-sonnet-5-5"}
    assert sent["claude-haiku-4-5"] is None  # plain model: no options
    assert sent["claude-sonnet-5-5"]["omit_temperature"] is True


@pytest.mark.asyncio
async def test_a_failing_model_is_reported_with_its_error_and_does_not_stop_the_rest():
    router, providers = build(anthropic=Provider("anthropic", fail_models={"claude-sonnet-5-5"}))
    results = await smoke(router, profile="premium")
    failed = [r for r in results if r["status"] == "error"]
    assert failed and all(r["model"] == "claude-sonnet-5-5" for r in failed)
    assert "HTTP 400" in failed[0]["detail"]
    assert any(r["status"] == "ok" and r["model"] == "claude-haiku-4-5" for r in results)


@pytest.mark.asyncio
async def test_the_economy_profile_is_the_default_and_covers_deepseek():
    router, providers = build(deepseek=Provider("deepseek"))
    results = await smoke(router)
    assert {r["profile"] for r in results} == {"economy"}
    assert any(r["provider"] == "deepseek" and r["status"] == "ok" for r in results)


@pytest.mark.asyncio
async def test_disabled_providers_are_not_called():
    router, providers = build(deepseek=Provider("deepseek"))
    router.settings.llm_disabled_providers = "deepseek"
    results = await smoke(router)
    assert providers["deepseek"].calls == []
    assert any(r["provider"] == "deepseek" and r["status"] == "skipped" for r in results)


@pytest.mark.asyncio
async def test_render_summarises_and_flags_failures():
    router, _ = build(anthropic=Provider("anthropic", fail_models={"claude-sonnet-5-5"}))
    text, ok = render(await smoke(router, profile="premium"))
    assert ok is False
    assert "claude-sonnet-5-5" in text and "error" in text.lower()
    router2, _ = build(anthropic=Provider("anthropic"))
    text2, ok2 = render(await smoke(router2, profile="premium"))
    assert ok2 is True
