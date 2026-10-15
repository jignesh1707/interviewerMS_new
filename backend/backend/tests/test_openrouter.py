from pathlib import Path

import pytest

from app.config import Settings
from app.llm.providers.base import LLMMessage
from app.llm.providers.openai_compatible import OpenAICompatibleClient
from app.llm.router import ModelRouter, load_router_config
from app.llm.safety import bound_provider_call, is_approved_endpoint
from tests.test_router import FakeProvider

MODELS_YAML = Path(__file__).resolve().parents[1] / "models.yaml"
DEEPSEEK_KEY = "ds-test-key-1111111111"
OPENROUTER_KEY = "or-test-key-2222222222"


def _messages():
    return [LLMMessage(role="user", content="hi")]


def _router(**overrides):
    values = {
        "deepseek_api_key": DEEPSEEK_KEY,
        "openrouter_api_key": OPENROUTER_KEY,
        "provider_cooldown_seconds": 60.0,
        "llm_max_attempts_per_provider": 1,
        **overrides,
    }
    settings = Settings(**values)
    return ModelRouter(settings=settings, config=load_router_config(MODELS_YAML))


def test_yaml_runs_every_economy_tier_on_gpt_6_luna_through_openrouter():
    config = load_router_config(MODELS_YAML)
    for tier in ("cheap", "standard", "premium"):
        first = config.candidates(tier)[0]
        assert (first.provider, first.model) == ("openrouter", "openai/gpt-6-luna")
        assert (first.input_price, first.output_price) == (0.10, 0.50)
        assert all(c.provider != "deepseek" for c in config.candidates(tier))


def test_each_task_resolves_to_the_intended_tier_and_model():
    config = load_router_config(MODELS_YAML)
    expected = {
        "resume_summary": ("cheap", "openai/gpt-6-luna"),
        "jd_metadata": ("cheap", "openai/gpt-6-luna"),
        "followup_generation": ("cheap", "openai/gpt-6-luna"),
        "answer_coaching": ("cheap", "openai/gpt-6-luna"),
        "question_generation": ("standard", "openai/gpt-6-luna"),
        "tips_generation": ("standard", "openai/gpt-6-luna"),
        "answer_analysis": ("standard", "openai/gpt-6-luna"),
        "final_scoring": ("premium", "openai/gpt-6-luna"),
        "report_narrative": ("premium", "openai/gpt-6-luna"),
    }
    assert set(config.tasks) == set(expected)
    for task, (tier, model) in expected.items():
        assert config.tier_for_task(task) == tier
        assert config.candidates(tier)[0].model == model


def test_openrouter_endpoint_is_allowlisted_per_provider():
    assert is_approved_endpoint("https://openrouter.ai/api/v1", "openrouter")
    assert not is_approved_endpoint("https://openrouter.ai/api/v1", "deepseek")
    assert not is_approved_endpoint("https://evil.example/api/v1", "openrouter")
    assert not is_approved_endpoint("http://openrouter.ai/api/v1", "openrouter")


def test_only_configured_providers_are_used():
    router = _router()
    assert router.provider_configured("deepseek") is True
    assert router.provider_configured("openrouter") is True
    assert router.provider_configured("openai") is False
    assert router.provider_configured("anthropic") is False
    ready, skipped = router._plan("standard", None)
    assert [c.provider for c in ready] == ["openrouter"]
    assert {item["provider"] for item in skipped} == {"openai", "anthropic"}


def test_openrouter_unconfigured_without_key_or_with_bad_endpoint():
    assert _router(openrouter_api_key="").provider_configured("openrouter") is False
    assert _router(openrouter_base_url="https://evil.example/v1").provider_configured("openrouter") is False


def test_openrouter_can_be_disabled_by_policy():
    router = _router(llm_disabled_providers="openrouter", openai_api_key="sk-proj-test-12345678")
    ready, _ = router._plan("standard", None)
    assert [c.provider for c in ready] == ["openai"]
    assert router.status()["providers"]["openrouter"]["disabled_by_policy"] is True


def test_openrouter_key_is_registered_for_log_redaction():
    assert OPENROUTER_KEY in _router()._secret_values()


async def test_falls_back_from_gpt_6_luna_on_openrouter_to_openai_direct():
    router = _router()
    openrouter = FakeProvider("openrouter", fail=True)
    openai = FakeProvider("openai")
    router._providers = {
        "deepseek": FakeProvider("deepseek"),
        "openrouter": openrouter,
        "openai": openai,
        "anthropic": FakeProvider("anthropic"),
    }
    router.provider_configured = lambda name: name in {"openrouter", "openai"}
    result = await router.complete("question_generation", _messages())
    assert result.provider == "openai"
    assert result.model == "gpt-4o-mini"
    assert openrouter.calls == 1 and openai.calls == 1
    assert router.health("openrouter").consecutive_failures == 1


async def test_deepseek_is_never_tried_by_the_shipped_economy_setup():
    router = _router()
    deepseek = FakeProvider("deepseek")
    router._providers = {
        "deepseek": deepseek,
        "openrouter": FakeProvider("openrouter", fail=True),
        "openai": FakeProvider("openai", fail=True),
        "anthropic": FakeProvider("anthropic", fail=True),
    }
    router.provider_configured = lambda name: True
    with pytest.raises(Exception):
        await router.complete("question_generation", _messages())
    assert deepseek.calls == 0


class _Response:
    status_code = 200

    def json(self):
        return {
            "model": "openai/gpt-6-luna",
            "choices": [{"message": {"content": " {\"ok\": true} "}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
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


async def test_openrouter_request_shape_and_credentials(monkeypatch):
    monkeypatch.setattr("app.llm.providers.openai_compatible.httpx.AsyncClient", _Client)
    router = _router()
    client = router._providers["openrouter"]
    with bound_provider_call(task="question_generation", provider="openrouter", model="openai/gpt-6-luna"):
        response = await client.complete(_messages(), "openai/gpt-6-luna", 0.4, 256)
    sent = _Client.sent
    assert sent["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert sent["json"]["model"] == "openai/gpt-6-luna"
    assert sent["json"]["provider"] == {"data_collection": "deny"}
    assert sent["headers"]["Authorization"] == f"Bearer {OPENROUTER_KEY}"
    assert DEEPSEEK_KEY not in str(sent)
    assert response.text == '{"ok": true}' and response.input_tokens == 7


async def test_deepseek_request_has_no_openrouter_preferences(monkeypatch):
    monkeypatch.setattr("app.llm.providers.openai_compatible.httpx.AsyncClient", _Client)
    client = _router()._providers["deepseek"]
    with bound_provider_call(task="question_generation", provider="deepseek", model="deepseek-flash"):
        await client.complete(_messages(), "deepseek-flash", 0.4, 256)
    assert "provider" not in _Client.sent["json"]
    assert _Client.sent["headers"]["Authorization"] == f"Bearer {DEEPSEEK_KEY}"
    assert OPENROUTER_KEY not in str(_Client.sent)


@pytest.mark.parametrize(
    "setting, expected",
    [("deny", {"provider": {"data_collection": "deny"}}), ("allow", {"provider": {"data_collection": "allow"}}), ("", {})],
)
def test_data_collection_preference_configurable(setting, expected):
    assert _router(openrouter_data_collection=setting)._providers["openrouter"].extra_body == expected


def test_client_without_extra_body_defaults_to_empty():
    assert OpenAICompatibleClient("deepseek", "https://api.deepseek.com", "k", 5).extra_body == {}


def test_every_luna_entry_runs_at_low_reasoning_effort():
    config = load_router_config(MODELS_YAML)
    for tier in ("cheap", "standard", "premium"):
        first = config.candidates(tier)[0]
        assert first.options == {"body": {"reasoning_effort": "low"}}


async def test_model_options_body_reaches_the_request_but_cannot_replace_core_fields(monkeypatch):
    monkeypatch.setattr("app.llm.providers.openai_compatible.httpx.AsyncClient", _Client)
    client = _router()._providers["openrouter"]
    options = {"body": {"reasoning_effort": "low", "model": "evil/other", "stream": True, "messages": []}}
    with bound_provider_call(task="question_generation", provider="openrouter", model="openai/gpt-6-luna"):
        await client.complete(_messages(), "openai/gpt-6-luna", 0.4, 256, options=options)
    sent = _Client.sent["json"]
    assert sent["reasoning_effort"] == "low"
    assert sent["model"] == "openai/gpt-6-luna" and sent["stream"] is False and sent["messages"]


async def test_no_options_leaves_the_request_unchanged(monkeypatch):
    monkeypatch.setattr("app.llm.providers.openai_compatible.httpx.AsyncClient", _Client)
    client = _router()._providers["openrouter"]
    with bound_provider_call(task="question_generation", provider="openrouter", model="openai/gpt-6-luna"):
        await client.complete(_messages(), "openai/gpt-6-luna", 0.4, 256)
    assert "reasoning_effort" not in _Client.sent["json"]
