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


def test_yaml_keeps_cheap_standard_on_flash_and_premium_on_pro():
    config = load_router_config(MODELS_YAML)
    expected = {
        "cheap": ("deepseek-flash", "deepseek/deepseek-v4.1-flash"),
        "standard": ("deepseek-flash", "deepseek/deepseek-v4.1-flash"),
        "premium": ("deepseek-v4-pro", "deepseek/deepseek-v4-pro-0813"),
    }
    for tier, (direct, via_openrouter) in expected.items():
        first, second = config.candidates(tier)[:2]
        assert (first.provider, first.model) == ("deepseek", direct)
        assert (second.provider, second.model) == ("openrouter", via_openrouter)


def test_each_task_resolves_to_the_intended_tier_and_model():
    config = load_router_config(MODELS_YAML)
    expected = {
        "resume_summary": ("cheap", "deepseek-flash"),
        "jd_metadata": ("cheap", "deepseek-flash"),
        "followup_generation": ("cheap", "deepseek-flash"),
        "answer_coaching": ("cheap", "deepseek-flash"),
        "question_generation": ("standard", "deepseek-flash"),
        "tips_generation": ("standard", "deepseek-flash"),
        "answer_analysis": ("standard", "deepseek-flash"),
        "final_scoring": ("premium", "deepseek-v4-pro"),
        "report_narrative": ("premium", "deepseek-v4-pro"),
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
    assert [c.provider for c in ready] == ["deepseek", "openrouter"]
    assert {item["provider"] for item in skipped} == {"openai", "anthropic"}


def test_openrouter_unconfigured_without_key_or_with_bad_endpoint():
    assert _router(openrouter_api_key="").provider_configured("openrouter") is False
    assert _router(openrouter_base_url="https://evil.example/v1").provider_configured("openrouter") is False


def test_openrouter_can_be_disabled_by_policy():
    router = _router(llm_disabled_providers="openrouter")
    ready, _ = router._plan("standard", None)
    assert [c.provider for c in ready] == ["deepseek"]
    assert router.status()["providers"]["openrouter"]["disabled_by_policy"] is True


def test_openrouter_key_is_registered_for_log_redaction():
    assert OPENROUTER_KEY in _router()._secret_values()


async def test_falls_back_from_deepseek_to_openrouter_with_openrouter_model_id():
    router = _router()
    deepseek = FakeProvider("deepseek", fail=True)
    openrouter = FakeProvider("openrouter")
    router._providers = {
        "deepseek": deepseek,
        "openrouter": openrouter,
        "openai": FakeProvider("openai"),
        "anthropic": FakeProvider("anthropic"),
    }
    router.settings.openai_api_key = ""  # keep OpenAI/Anthropic out of the plan
    router.provider_configured = lambda name: name in {"deepseek", "openrouter"}
    result = await router.complete("question_generation", _messages())
    assert result.provider == "openrouter"
    assert result.model == "deepseek/deepseek-v4.1-flash"
    assert deepseek.calls == 1 and openrouter.calls == 1
    assert router.health("deepseek").consecutive_failures == 1


class _Response:
    status_code = 200

    def json(self):
        return {
            "model": "deepseek/deepseek-v4.1-flash",
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
    with bound_provider_call(task="question_generation", provider="openrouter", model="deepseek/deepseek-v4.1-flash"):
        response = await client.complete(_messages(), "deepseek/deepseek-v4.1-flash", 0.4, 256)
    sent = _Client.sent
    assert sent["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert sent["json"]["model"] == "deepseek/deepseek-v4.1-flash"
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
