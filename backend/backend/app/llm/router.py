import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.core.errors import AllProvidersFailedError, ProviderError, ValidationAppError
from app.core.logging import get_logger
from app.llm.providers.anthropic import AnthropicClient
from app.llm.providers.base import LLMMessage, ProviderCallError, ProviderResponse
from app.llm.providers.openai_compatible import OpenAICompatibleClient

logger = get_logger(__name__)


OUTPUT_COST_WEIGHT = 0.6


class ModelCandidate(BaseModel):
    provider: str
    model: str
    input_price: float = 0.0
    output_price: float = 0.0
    priority: int | None = None

    def cost_score(self) -> float:
        """Expected relative cost. Output tokens dominate this workload, so they are weighted higher."""
        return (
            self.input_price * (1.0 - OUTPUT_COST_WEIGHT)
            + self.output_price * OUTPUT_COST_WEIGHT
        )

    def sort_key(self, yaml_index: int = 0) -> tuple[int, float]:
        if self.priority is not None:
            return (0, float(self.priority))
        return (1, float(yaml_index))


class RouterConfig(BaseModel):
    tiers: dict[str, list[ModelCandidate]] = Field(default_factory=dict)
    tasks: dict[str, str] = Field(default_factory=dict)
    default_tier: str = "standard"

    def tier_for_task(self, task: str) -> str:
        return self.tasks.get(task, self.default_tier)

    def candidates(self, tier: str) -> list[ModelCandidate]:
        if tier not in self.tiers:
            raise ValidationAppError(f"unknown model tier '{tier}'", details={"tier": tier})
        return [
            candidate
            for _, candidate in sorted(
                enumerate(self.tiers[tier]),
                key=lambda item: item[1].sort_key(item[0]),
            )
        ]


@dataclass
class ProviderHealth:
    consecutive_failures: int = 0
    disabled_until: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0

    def is_available(self, now: float) -> bool:
        return now >= self.disabled_until

    def record_failure(self, message: str, cooldown: float, now: float, auth_error: bool = False) -> None:
        self.consecutive_failures += 1
        self.last_error = message[:500]
        self.last_error_at = now
        effective_cooldown = cooldown * max(1, min(self.consecutive_failures, 5))
        if auth_error:
            effective_cooldown = max(cooldown, 900.0)
        self.disabled_until = now + effective_cooldown

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.disabled_until = 0.0


@dataclass
class UsageTotals:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    by_model: dict[str, float] = field(default_factory=dict)

    def add(self, response: ProviderResponse, candidate: ModelCandidate) -> float:
        cost = (
            response.input_tokens / 1_000_000 * candidate.input_price
            + response.output_tokens / 1_000_000 * candidate.output_price
        )
        self.calls += 1
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self.estimated_cost_usd += cost
        key = f"{candidate.provider}/{response.model}"
        self.by_model[key] = self.by_model.get(key, 0.0) + cost
        return cost


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    tier: str
    task: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    latency_ms: int
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def fallback_used(self) -> bool:
        return len(self.attempts) > 1


class ModelRouter:
    def __init__(self, settings: Settings | None = None, config: RouterConfig | None = None) -> None:
        self.settings = settings or get_settings()
        self.config = config or load_router_config(self.settings.models_config_path)
        self.usage = UsageTotals()
        self._health: dict[str, ProviderHealth] = {}
        self._lock = asyncio.Lock()
        self._providers: dict[str, Any] = {}
        self._build_providers()

    def _build_providers(self) -> None:
        s = self.settings
        self._providers = {
            "openai": OpenAICompatibleClient("openai", s.openai_base_url, s.openai_api_key, s.llm_timeout_seconds),
            "deepseek": OpenAICompatibleClient("deepseek", s.deepseek_base_url, s.deepseek_api_key, s.llm_timeout_seconds),
            "anthropic": AnthropicClient("anthropic", s.anthropic_base_url, s.anthropic_api_key, s.llm_timeout_seconds),
        }

    def provider_configured(self, name: str) -> bool:
        provider = self._providers.get(name)
        return bool(provider and provider.configured)

    def health(self, name: str) -> ProviderHealth:
        return self._health.setdefault(name, ProviderHealth())

    def _plan(self, tier: str) -> tuple[list[ModelCandidate], list[dict[str, Any]]]:
        now = time.monotonic()
        ready: list[ModelCandidate] = []
        skipped: list[dict[str, Any]] = []
        for candidate in self.config.candidates(tier):
            if not self.provider_configured(candidate.provider):
                skipped.append({"provider": candidate.provider, "model": candidate.model, "reason": "not_configured"})
                continue
            if not self.health(candidate.provider).is_available(now):
                skipped.append({"provider": candidate.provider, "model": candidate.model, "reason": "circuit_open"})
                continue
            ready.append(candidate)
        return ready, skipped

    async def complete(
        self,
        task: str,
        messages: list[LLMMessage],
        *,
        tier: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        resolved_tier = tier or self.config.tier_for_task(task)
        ready, skipped = self._plan(resolved_tier)
        attempts: list[dict[str, Any]] = [dict(skip, stage="plan") for skip in skipped]

        if not ready:
            raise AllProvidersFailedError(
                f"no configured/available provider for tier '{resolved_tier}'",
                details={"task": task, "tier": resolved_tier, "attempts": attempts},
            )

        last_error: Exception | None = None
        for candidate in ready:
            provider = self._providers[candidate.provider]
            started = time.monotonic()
            for attempt in range(1, self.settings.llm_max_attempts_per_provider + 1):
                try:
                    response = await provider.complete(
                        messages,
                        model=candidate.model,
                        temperature=self.settings.llm_temperature if temperature is None else temperature,
                        max_tokens=max_tokens or self.settings.llm_max_output_tokens,
                    )
                except ProviderCallError as exc:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    attempts.append(
                        {
                            "provider": candidate.provider,
                            "model": candidate.model,
                            "attempt": attempt,
                            "status": "error",
                            "error": exc.message,
                            "retryable": exc.retryable,
                            "latency_ms": latency_ms,
                        }
                    )
                    last_error = exc
                    logger.warning(
                        "provider_call_failed provider=%s model=%s task=%s attempt=%s retryable=%s error=%s",
                        candidate.provider,
                        candidate.model,
                        task,
                        attempt,
                        exc.retryable,
                        exc.message,
                    )
                    if exc.retryable and attempt < self.settings.llm_max_attempts_per_provider:
                        continue
                    break
                except Exception as exc:  # noqa: BLE001
                    attempts.append(
                        {
                            "provider": candidate.provider,
                            "model": candidate.model,
                            "attempt": attempt,
                            "status": "error",
                            "error": str(exc),
                            "retryable": True,
                            "latency_ms": int((time.monotonic() - started) * 1000),
                        }
                    )
                    last_error = exc
                    logger.exception("provider_call_unexpected provider=%s task=%s", candidate.provider, task)
                    break
                else:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    async with self._lock:
                        self.health(candidate.provider).record_success()
                        cost = self.usage.add(response, candidate)
                    attempts.append(
                        {
                            "provider": candidate.provider,
                            "model": candidate.model,
                            "attempt": attempt,
                            "status": "ok",
                            "latency_ms": latency_ms,
                        }
                    )
                    logger.info(
                        "provider_call_ok provider=%s model=%s task=%s tier=%s latency_ms=%s cost_usd=%.6f",
                        candidate.provider,
                        response.model,
                        task,
                        resolved_tier,
                        latency_ms,
                        cost,
                    )
                    return LLMResult(
                        text=response.text,
                        provider=candidate.provider,
                        model=response.model,
                        tier=resolved_tier,
                        task=task,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        estimated_cost_usd=cost,
                        latency_ms=latency_ms,
                        attempts=attempts,
                    )

            auth_error = isinstance(last_error, ProviderCallError) and last_error.auth_error
            async with self._lock:
                self.health(candidate.provider).record_failure(
                    str(last_error), self.settings.provider_cooldown_seconds, time.monotonic(), auth_error
                )

        raise AllProvidersFailedError(
            f"all providers failed for task '{task}'",
            details={"task": task, "tier": resolved_tier, "attempts": attempts, "last_error": str(last_error)},
        )

    async def complete_json(
        self,
        task: str,
        messages: list[LLMMessage],
        *,
        tier: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[dict[str, Any], LLMResult]:
        result = await self.complete(
            task, messages, tier=tier, temperature=temperature, max_tokens=max_tokens
        )
        try:
            return extract_json(result.text), result
        except ValueError as exc:
            raise ProviderError(
                f"model '{result.provider}/{result.model}' returned unparseable JSON",
                details={"task": task, "raw_preview": result.text[:500]},
            ) from exc

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        providers = {}
        for name, provider in self._providers.items():
            state = self.health(name)
            providers[name] = {
                "configured": provider.configured,
                "available": provider.configured and state.is_available(now),
                "consecutive_failures": state.consecutive_failures,
                "disabled_for_seconds": max(0.0, round(state.disabled_until - now, 1)),
                "last_error": state.last_error,
            }
        return {
            "providers": providers,
            "tiers": {
                name: [
                    {
                        **candidate.model_dump(),
                        "cost_score": round(candidate.cost_score(), 4),
                        "resolved_order": position,
                    }
                    for position, candidate in enumerate(self.config.candidates(name))
                ]
                for name in self.config.tiers
            },
            "tasks": self.config.tasks,
            "usage": {
                "calls": self.usage.calls,
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "estimated_cost_usd": round(self.usage.estimated_cost_usd, 6),
                "by_model": {key: round(value, 6) for key, value in self.usage.by_model.items()},
            },
        }


def load_router_config(path: Path) -> RouterConfig:
    if not path.exists():
        raise ValidationAppError(f"models config not found at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return RouterConfig(**data)


def extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1]
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start_candidates = [index for index in (cleaned.find("{"), cleaned.find("[")) if index != -1]
        if not start_candidates:
            raise ValueError("no JSON object found in response")
        start = min(start_candidates)
        end = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if end <= start:
            raise ValueError("no JSON object found in response")
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router
