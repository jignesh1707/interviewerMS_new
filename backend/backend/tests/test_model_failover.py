"""Economy runs two models at one provider (GPT-6 Luna, then gpt-4.1-nano, both at OpenAI): a failing model must not
take its sibling down with it, while a rejected key still stops every model of that provider."""

import pytest

from app.config import BASE_DIR, Settings
from app.llm.providers.base import LLMMessage, ProviderCallError, ProviderResponse
from app.llm.router import ModelRouter, load_router_config


class Provider:
    def __init__(self, name, *, fail_models=(), auth_error=False):
        self.name = name
        self.configured = True
        self.fail_models = set(fail_models)
        self.auth_error = auth_error
        self.calls = []

    async def complete(self, messages, model, temperature, max_tokens, options=None):
        self.calls.append(model)
        if model in self.fail_models:
            raise ProviderCallError(f"{self.name} {model} failed", retryable=False, auth_error=self.auth_error)
        return ProviderResponse(text='{"ok": true}', model=model, input_tokens=10, output_tokens=5)


def build(openai, anthropic):
    settings = Settings(llm_max_attempts_per_provider=1, provider_cooldown_seconds=60.0)
    router = ModelRouter(settings=settings, config=load_router_config(BASE_DIR / "models.yaml"))
    router._providers = {"openai": openai, "anthropic": anthropic}
    return router


def messages():
    return [LLMMessage(role="user", content="hi")]


@pytest.mark.asyncio
async def test_a_failing_gpt_6_luna_hands_over_to_gpt_4_1_nano_and_stays_out_of_the_way():
    openai = Provider("openai", fail_models={"gpt-6-luna"})
    anthropic = Provider("anthropic")
    router = build(openai, anthropic)

    first = await router.complete("question_generation", messages())
    second = await router.complete("question_generation", messages())

    assert (first.provider, first.model) == ("openai", "gpt-4.1-nano")
    # Luna is in its own cooldown: the next call goes straight to nano, not back to Luna and not to Anthropic.
    assert (second.provider, second.model) == ("openai", "gpt-4.1-nano")
    assert openai.calls == ["gpt-6-luna", "gpt-4.1-nano", "gpt-4.1-nano"]
    assert anthropic.calls == []


@pytest.mark.asyncio
async def test_a_rejected_openai_key_skips_both_openai_models_until_the_cooldown_ends():
    openai = Provider("openai", fail_models={"gpt-6-luna", "gpt-4.1-nano"}, auth_error=True)
    anthropic = Provider("anthropic")
    router = build(openai, anthropic)

    first = await router.complete("question_generation", messages())
    second = await router.complete("question_generation", messages())

    assert first.provider == "anthropic" and second.provider == "anthropic"
    assert openai.calls == ["gpt-6-luna"]  # the key failed once; nano is not tried with the same key
    ready, skipped = router._plan("standard", None)
    assert [c.provider for c in ready] == ["anthropic"]
    assert {(s["model"], s["reason"]) for s in skipped} == {
        ("gpt-6-luna", "circuit_open"),
        ("gpt-4.1-nano", "circuit_open"),
    }


@pytest.mark.asyncio
async def test_openai_is_reported_available_while_one_of_its_models_works():
    openai = Provider("openai", fail_models={"gpt-6-luna"})
    router = build(openai, Provider("anthropic"))
    await router.complete("question_generation", messages())
    status = router.status()["providers"]["openai"]
    assert status["available"] is True
    assert status["consecutive_failures"] == 0  # nano's success after Luna's failure
