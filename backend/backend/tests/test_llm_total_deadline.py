"""A provider that keeps a request open must be cut off after the configured timeout, not minutes later.

httpx's timeout applies to each network operation, so a provider that keeps the connection alive with small writes
(OpenRouter does while a busy upstream thinks) was never cut off: staging logged GPT-6 Luna calls of 60 to 435 seconds
against a 40 second timeout. Both clients now put a deadline on the whole call.
"""

import asyncio
import time

import pytest

from app.llm.providers.anthropic import AnthropicClient
from app.llm.providers.base import LLMMessage, ProviderCallError
from app.llm.providers.openai_compatible import OpenAICompatibleClient
from app.llm.safety import bound_provider_call


class _HangingClient:
    """An httpx.AsyncClient stand-in whose request never finishes within the test."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json, headers):
        await asyncio.sleep(30)


def _messages():
    return [LLMMessage(role="user", content="hi")]


async def test_openai_compatible_call_is_cut_off_at_the_deadline(monkeypatch):
    monkeypatch.setattr("app.llm.providers.openai_compatible.httpx.AsyncClient", _HangingClient)
    client = OpenAICompatibleClient("openrouter", "https://openrouter.ai/api/v1", "key-1234567890", 0.2)
    started = time.monotonic()
    with bound_provider_call(task="question_generation", provider="openrouter", model="openai/gpt-6-luna"):
        with pytest.raises(ProviderCallError) as failure:
            await client.complete(_messages(), "openai/gpt-6-luna", 0.4, 64)
    assert time.monotonic() - started < 3
    assert "timed out" in failure.value.message
    assert failure.value.retryable is True  # so the router falls back to the next provider


async def test_anthropic_call_is_cut_off_at_the_deadline(monkeypatch):
    monkeypatch.setattr("app.llm.providers.anthropic.httpx.AsyncClient", _HangingClient)
    client = AnthropicClient("anthropic", "https://api.anthropic.com", "key-1234567890", 0.2)
    started = time.monotonic()
    with bound_provider_call(task="question_generation", provider="anthropic", model="claude-sonnet-5-5"):
        with pytest.raises(ProviderCallError) as failure:
            await client.complete(_messages(), "claude-sonnet-5-5", 0.4, 64)
    assert time.monotonic() - started < 3
    assert "timed out" in failure.value.message
    assert failure.value.retryable is True
