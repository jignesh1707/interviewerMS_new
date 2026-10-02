"""Per-plan model profiles: Economy and Premium are served by different provider lists, and Premium student
data can never reach DeepSeek, even if models.yaml is misconfigured."""

import pytest

from app.config import BASE_DIR, Settings
from app.core.errors import AllProvidersFailedError, PolicyDeniedError, ValidationAppError
from app.core.plans import PlansConfig, load_plans, validate_against_router
from app.llm.providers.base import LLMMessage, ProviderResponse
from app.llm.providers.anthropic import AnthropicClient
from app.llm.router import ModelCandidate, ModelRouter, RouterConfig, load_router_config
from app.llm.safety import bound_provider_call

MODELS_YAML = BASE_DIR / "models.yaml"
US_ONLY = {"anthropic", "openai"}


class FakeProvider:
    def __init__(self, name: str, *, supports_options: bool = False) -> None:
        self.name = name
        self.configured = True
        self.calls: list[dict] = []
        self.supports_options = supports_options

    async def complete(self, messages, model, temperature, max_tokens, options=None):
        self.calls.append({"model": model, "options": options})
        return ProviderResponse(text=f"ok:{self.name}", model=model, input_tokens=100, output_tokens=50)


def messages():
    return [LLMMessage(role="user", content="hi")]


def router_from(config: RouterConfig) -> tuple[ModelRouter, dict[str, FakeProvider]]:
    router = ModelRouter(settings=Settings(llm_max_attempts_per_provider=1), config=config)
    providers = {name: FakeProvider(name) for name in ("openai", "deepseek", "openrouter", "anthropic")}
    router._providers = providers
    return router, providers


# ----------------------------------------------------------------------------- shipped models.yaml


def test_shipped_yaml_has_an_economy_default_and_a_premium_profile():
    config = load_router_config(MODELS_YAML)
    assert config.default_profile == "economy"
    assert "premium" in config.profiles
    assert set(config.profile_names()) == {"economy", "premium"}


def test_premium_profile_only_lists_us_providers_and_leads_with_anthropic():
    config = load_router_config(MODELS_YAML)
    for tier in ("cheap", "standard", "premium"):
        candidates = config.candidates(tier, profile="premium")
        assert {c.provider for c in candidates} <= US_ONLY, tier
        assert candidates[0].provider == "anthropic", tier
    assert config.providers_in("premium") <= US_ONLY


def test_economy_profile_is_the_existing_deepseek_first_setup():
    config = load_router_config(MODELS_YAML)
    for tier in ("cheap", "standard", "premium"):
        assert config.candidates(tier, profile="economy") == config.candidates(tier)
        assert config.candidates(tier, profile=None)[0].provider == "deepseek"
    assert "deepseek" in config.providers_in("economy")


def test_an_unknown_profile_is_an_error_and_never_falls_back_to_economy():
    config = load_router_config(MODELS_YAML)
    with pytest.raises(ValidationAppError):
        config.candidates("standard", profile="platinum")


# ----------------------------------------------------------------------------- routing


@pytest.mark.asyncio
async def test_each_profile_is_served_by_its_own_first_choice():
    router, providers = router_from(load_router_config(MODELS_YAML))
    economy = await router.complete("answer_analysis", messages(), profile="economy")
    premium = await router.complete("answer_analysis", messages(), profile="premium")
    default = await router.complete("answer_analysis", messages())
    assert economy.provider == "deepseek" and default.provider == "deepseek"
    assert premium.provider == "anthropic"
    assert premium.profile == "premium" and economy.profile == "economy"
    assert len(providers["deepseek"].calls) == 2 and len(providers["anthropic"].calls) == 1


def _misconfigured() -> RouterConfig:
    """A premium profile where someone mistakenly put DeepSeek first."""
    leaky = [
        ModelCandidate(provider="deepseek", model="deepseek-flash", priority=0),
        ModelCandidate(provider="anthropic", model="claude-sonnet-5-5", priority=1),
    ]
    return RouterConfig(
        tiers={"standard": leaky},
        profiles={"premium": {"standard": leaky}},
        tasks={"answer_analysis": "standard"},
        default_tier="standard",
    )


@pytest.mark.asyncio
async def test_the_allow_list_keeps_deepseek_out_even_when_the_yaml_is_wrong():
    router, providers = router_from(_misconfigured())
    result = await router.complete(
        "answer_analysis", messages(), profile="premium", authorized_providers=("anthropic", "openai")
    )
    assert result.provider == "anthropic"
    assert providers["deepseek"].calls == []  # not even attempted


@pytest.mark.asyncio
async def test_if_no_allowed_provider_is_available_nothing_falls_through_to_deepseek():
    router, providers = router_from(_misconfigured())
    providers["anthropic"].configured = False
    with pytest.raises((PolicyDeniedError, AllProvidersFailedError)):
        await router.complete(
            "answer_analysis", messages(), profile="premium", authorized_providers=("anthropic", "openai")
        )
    assert providers["deepseek"].calls == []


# ----------------------------------------------------------------------------- per-model options


@pytest.mark.asyncio
async def test_candidate_options_reach_the_provider_and_plain_candidates_get_none():
    options = {"omit_temperature": True, "body": {"output_config": {"effort": "low"}}}
    config = RouterConfig(
        tiers={
            "standard": [
                ModelCandidate(provider="anthropic", model="claude-sonnet-5-5", priority=0, options=options),
                ModelCandidate(provider="openai", model="gpt-4o-mini", priority=1),
            ]
        },
        tasks={"answer_analysis": "standard"},
        default_tier="standard",
    )
    router, providers = router_from(config)
    await router.complete("answer_analysis", messages())
    assert providers["anthropic"].calls[0]["options"] == options

    providers["anthropic"].configured = False
    await router.complete("answer_analysis", messages())
    assert providers["openai"].calls[0]["options"] is None  # no options configured: provider is called as before


# ----------------------------------------------------------------------------- Anthropic request shape


class _Response:
    status_code = 200

    def json(self):
        return {
            "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": ' {"ok": true} '}],
            "usage": {"input_tokens": 11, "output_tokens": 4},
        }


class _Client:
    sent: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json, headers):
        _Client.sent = {"url": url, "json": json, "headers": headers}
        return _Response()


def _anthropic() -> AnthropicClient:
    return AnthropicClient("anthropic", "https://api.anthropic.com", "sk-ant-test", 5.0)


async def _call(options):
    with bound_provider_call(task="answer_analysis", provider="anthropic", model="claude-sonnet-5-5"):
        return await _anthropic().complete(messages(), "claude-sonnet-5-5", 0.4, 256, options=options)


@pytest.mark.asyncio
async def test_anthropic_sends_temperature_by_default(monkeypatch):
    monkeypatch.setattr("app.llm.providers.anthropic.httpx.AsyncClient", _Client)
    response = await _call(None)
    assert _Client.sent["json"]["temperature"] == 0.4
    assert "thinking" not in _Client.sent["json"]
    assert response.text == '{"ok": true}' and response.input_tokens == 11


@pytest.mark.asyncio
async def test_anthropic_options_can_drop_temperature_and_add_thinking_and_effort(monkeypatch):
    """Newer Claude models reject a non-default temperature and think by default; the options switch both."""
    monkeypatch.setattr("app.llm.providers.anthropic.httpx.AsyncClient", _Client)
    await _call(
        {"omit_temperature": True, "body": {"thinking": {"type": "between_tools"}, "output_config": {"effort": "low"}}}
    )
    sent = _Client.sent["json"]
    assert "temperature" not in sent
    assert sent["thinking"] == {"type": "between_tools"}
    assert sent["output_config"] == {"effort": "low"}
    assert sent["model"] == "claude-sonnet-5-5" and sent["max_tokens"] == 256


@pytest.mark.asyncio
async def test_options_cannot_override_the_model_or_the_messages(monkeypatch):
    monkeypatch.setattr("app.llm.providers.anthropic.httpx.AsyncClient", _Client)
    await _call({"body": {"model": "something-else", "messages": [], "max_tokens": 1, "system": "x"}})
    sent = _Client.sent["json"]
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["messages"] and sent["max_tokens"] == 256
    assert "system" not in sent


# ----------------------------------------------------------------------------- plans must match models.yaml


def test_the_shipped_plans_and_models_agree():
    plans = load_plans(BASE_DIR / "plans.yaml")
    validate_against_router(plans, load_router_config(MODELS_YAML))  # does not raise


def test_a_plan_naming_a_missing_profile_is_rejected():
    plans = PlansConfig.model_validate(
        {
            "default_plan": "gold",
            "profiles": {15: {"question_count": 5, "max_followups": 1}},
            "plans": {
                "gold": {
                    "pack_minutes": 10, "pack_days": 30, "durations": [15], "default_duration": 15,
                    "llm_profile": "gold-models",
                }
            },
        }
    )
    with pytest.raises(ValueError, match="gold-models"):
        validate_against_router(plans, load_router_config(MODELS_YAML))


def test_a_plan_whose_profile_uses_a_provider_it_forbids_is_rejected():
    """Premium promises US providers only; a models.yaml that lists DeepSeek for it must fail at startup."""
    config = load_router_config(MODELS_YAML)
    config.profiles["premium"]["standard"].insert(0, ModelCandidate(provider="deepseek", model="deepseek-flash"))
    plans = load_plans(BASE_DIR / "plans.yaml")
    with pytest.raises(ValueError, match="deepseek"):
        validate_against_router(plans, config)
