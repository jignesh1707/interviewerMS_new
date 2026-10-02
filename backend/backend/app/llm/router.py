import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings, get_settings
from app.core.errors import AllProvidersFailedError, PolicyDeniedError, ProviderError, SafetyViolationError, ValidationAppError
from app.core.logging import get_logger, install_secret_filter
from app.llm.providers.anthropic import AnthropicClient
from app.llm.providers.base import LLMMessage, ProviderCallError, ProviderResponse
from app.llm.providers.openai_compatible import OpenAICompatibleClient
from app.llm.safety import (
    ModelRequest,
    assert_request_safe,
    bound_provider_call,
    known_task,
    redact_secrets,
    validate_provider_response,
    validate_structured_output,
)

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
        self.last_error = redact_secrets(message)[:500]
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
        install_secret_filter(self._secret_values())

    def _secret_values(self) -> tuple[str, ...]:
        s = self.settings
        values = [
            s.openai_api_key,
            s.deepseek_api_key,
            s.anthropic_api_key,
            s.webhook_secret,
            *s.api_key_set,
        ]
        return tuple(item for item in values if item)

    def _build_providers(self) -> None:
        s = self.settings
        self._providers = {
            "openai": OpenAICompatibleClient("openai", s.openai_base_url, s.openai_api_key, s.llm_timeout_seconds),
            "deepseek": OpenAICompatibleClient("deepseek", s.deepseek_base_url, s.deepseek_api_key, s.llm_timeout_seconds),
            "anthropic": AnthropicClient("anthropic", s.anthropic_base_url, s.anthropic_api_key, s.llm_timeout_seconds),
        }

    def provider_configured(self, name: str) -> bool:
        if name in self.settings.disabled_provider_set:
            return False
        provider = self._providers.get(name)
        return bool(provider and provider.configured)

    def health(self, name: str) -> ProviderHealth:
        return self._health.setdefault(name, ProviderHealth())

    def _plan(
        self,
        tier: str,
        authorized: frozenset[str] | None,
    ) -> tuple[list[ModelCandidate], list[dict[str, Any]]]:
        now = time.monotonic()
        ready: list[ModelCandidate] = []
        skipped: list[dict[str, Any]] = []
        for candidate in self.config.candidates(tier):
            if authorized is not None and candidate.provider not in authorized:
                skipped.append(
                    {"provider": candidate.provider, "model": candidate.model, "reason": "not_authorized"}
                )
                continue
            if not self.provider_configured(candidate.provider):
                skipped.append({"provider": candidate.provider, "model": candidate.model, "reason": "not_configured"})
                continue
            if not self.health(candidate.provider).is_available(now):
                skipped.append({"provider": candidate.provider, "model": candidate.model, "reason": "circuit_open"})
                continue
            ready.append(candidate)
        return ready, skipped

    def _authorized_set(self, request: ModelRequest) -> frozenset[str] | None:
        if request.authorized_providers is None:
            return None
        return frozenset(request.authorized_providers)

    async def complete(
        self,
        task: str,
        messages: list[LLMMessage],
        *,
        tier: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        authorized_providers: tuple[str, ...] | None = None,
    ) -> LLMResult:
        try:
            request = ModelRequest(
                task=task,
                messages=messages,
                authorized_providers=authorized_providers,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_seconds=self.settings.llm_timeout_seconds,
                expect_json=False,
            )
        except ValidationError as exc:
            raise PolicyDeniedError("malformed model request", details={"errors": exc.errors()}) from exc
        return await self._complete_validated(request, tier=tier)

    async def complete_request(self, request: ModelRequest) -> LLMResult:
        if not known_task(request.task, self.config.tasks):
            raise PolicyDeniedError(
                f"unknown or unauthorized task '{request.task}'",
                details={"task": request.task},
            )
        return await self._complete_validated(request)

    async def _complete_validated(self, request: ModelRequest, *, tier: str | None = None) -> LLMResult:
        assert_request_safe(request, self._secret_values())
        if not known_task(request.task, self.config.tasks):
            raise PolicyDeniedError(
                f"unknown or unauthorized task '{request.task}'",
                details={"task": request.task},
            )
        resolved_tier = tier or self.config.tier_for_task(request.task)
        authorized = self._authorized_set(request)
        ready, skipped = self._plan(resolved_tier, authorized)
        attempts: list[dict[str, Any]] = [dict(skip, stage="plan") for skip in skipped]

        if not ready:
            denied = bool(skipped) and all(item.get("reason") == "not_authorized" for item in skipped)
            error_cls = PolicyDeniedError if denied or authorized == frozenset() else AllProvidersFailedError
            raise error_cls(
                f"no configured/available provider for tier '{resolved_tier}'",
                details={"task": request.task, "tier": resolved_tier, "attempts": attempts},
            )

        last_error: Exception | None = None
        for candidate in ready:
            provider = self._providers[candidate.provider]
            started = time.monotonic()
            for attempt in range(1, self.settings.llm_max_attempts_per_provider + 1):
                try:
                    with bound_provider_call(task=request.task, provider=candidate.provider, model=candidate.model):
                        response = await provider.complete(
                            request.llm_messages(),
                            model=candidate.model,
                            temperature=self.settings.llm_temperature if request.temperature is None else request.temperature,
                            max_tokens=request.max_tokens or self.settings.llm_max_output_tokens,
                        )
                    response = validate_provider_response(response, self._secret_values())
                except ProviderCallError as exc:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    safe_error = redact_secrets(exc.message, self._secret_values())
                    attempts.append(
                        {
                            "provider": candidate.provider,
                            "model": candidate.model,
                            "attempt": attempt,
                            "status": "error",
                            "error": safe_error,
                            "retryable": exc.retryable,
                            "latency_ms": latency_ms,
                        }
                    )
                    last_error = exc
                    logger.warning(
                        "provider_call_failed provider=%s model=%s task=%s attempt=%s retryable=%s error=%s",
                        candidate.provider,
                        candidate.model,
                        request.task,
                        attempt,
                        exc.retryable,
                        safe_error,
                    )
                    if exc.retryable and attempt < self.settings.llm_max_attempts_per_provider:
                        continue
                    break
                except (SafetyViolationError, PolicyDeniedError) as exc:
                    latency_ms = int((time.monotonic() - started) * 1000)
                    safe_error = redact_secrets(str(exc), self._secret_values())
                    attempts.append(
                        {
                            "provider": candidate.provider,
                            "model": candidate.model,
                            "attempt": attempt,
                            "status": "error",
                            "error": safe_error,
                            "retryable": False,
                            "latency_ms": latency_ms,
                        }
                    )
                    last_error = exc
                    logger.warning(
                        "provider_call_rejected provider=%s task=%s error=%s",
                        candidate.provider,
                        request.task,
                        safe_error,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    safe_error = redact_secrets(str(exc), self._secret_values())
                    attempts.append(
                        {
                            "provider": candidate.provider,
                            "model": candidate.model,
                            "attempt": attempt,
                            "status": "error",
                            "error": safe_error,
                            "retryable": True,
                            "latency_ms": int((time.monotonic() - started) * 1000),
                        }
                    )
                    last_error = exc
                    logger.exception("provider_call_unexpected provider=%s task=%s", candidate.provider, request.task)
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
                        request.task,
                        resolved_tier,
                        latency_ms,
                        cost,
                    )
                    return LLMResult(
                        text=response.text,
                        provider=candidate.provider,
                        model=response.model,
                        tier=resolved_tier,
                        task=request.task,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens,
                        estimated_cost_usd=cost,
                        latency_ms=latency_ms,
                        attempts=attempts,
                    )

            auth_error = isinstance(last_error, ProviderCallError) and last_error.auth_error
            async with self._lock:
                self.health(candidate.provider).record_failure(
                    redact_secrets(str(last_error), self._secret_values()),
                    self.settings.provider_cooldown_seconds,
                    time.monotonic(),
                    auth_error,
                )

        raise AllProvidersFailedError(
            f"all providers failed for task '{request.task}'",
            details={
                "task": request.task,
                "tier": resolved_tier,
                "attempts": attempts,
                "last_error": redact_secrets(str(last_error), self._secret_values()),
            },
        )

    async def complete_json(
        self,
        task: str,
        messages: list[LLMMessage],
        *,
        tier: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        authorized_providers: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, Any], LLMResult]:
        result = await self.complete(
            task,
            messages,
            tier=tier,
            temperature=temperature,
            max_tokens=max_tokens,
            authorized_providers=authorized_providers,
        )
        try:
            payload = extract_json(result.text)
            return validate_structured_output(task, payload), result
        except ValueError as exc:
            raise ProviderError(
                f"model '{result.provider}/{result.model}' returned unparseable JSON",
                details={"task": task, "raw_preview": redact_secrets(result.text[:500], self._secret_values())},
            ) from exc
        except SafetyViolationError as exc:
            raise ProviderError(
                f"model '{result.provider}/{result.model}' returned invalid structured output",
                details={"task": task},
            ) from exc

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        providers = {}
        for name, provider in self._providers.items():
            state = self.health(name)
            providers[name] = {
                "configured": self.provider_configured(name),
                "disabled_by_policy": name in self.settings.disabled_provider_set,
                "available": self.provider_configured(name) and state.is_available(now),
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
