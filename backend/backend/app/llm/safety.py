from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import PolicyDeniedError, SafetyViolationError
from app.llm.providers.base import LLMMessage, ProviderResponse

__all__ = [
    "APPROVED_MODEL_HOSTS",
    "MAX_RESPONSE_CHARS",
    "ModelRequest",
    "PolicyDeniedError",
    "SafetyViolationError",
    "assert_request_safe",
    "bound_provider_call",
    "contains_forbidden_secret",
    "is_approved_endpoint",
    "redact_secrets",
    "require_boundary_permit",
    "validate_provider_response",
    "validate_structured_output",
]

APPROVED_MODEL_HOSTS = frozenset(
    {
        "api.openai.com",
        "api.deepseek.com",
        "api.anthropic.com",
    }
)

PROVIDER_APPROVED_HOSTS: dict[str, frozenset[str]] = {
    "openai": frozenset({"api.openai.com"}),
    "deepseek": frozenset({"api.deepseek.com"}),
    "anthropic": frozenset({"api.anthropic.com"}),
}

MAX_RESPONSE_CHARS = 32768
MAX_MESSAGE_CHARS = 24000
MAX_REQUEST_CHARS = 48000

_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:sk[-_](?:live|test|ant|proj)[-_][A-Za-z0-9_-]{8,}"
    r"|sk-ant-[A-Za-z0-9_-]{8,}"
    r"|whsec_[A-Za-z0-9_-]{8,}"
    r"|rk_live_[A-Za-z0-9_-]{8,})"
)
_BEARER_RE = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+")
_X_API_KEY_RE = re.compile(r"(?i)(x-api-key\s*:\s*)\S+")
_GENERIC_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+=/]{8,}")

_permit: ContextVar["_BoundaryPermit | None"] = ContextVar("model_safety_permit", default=None)

_TASK_SCHEMAS: dict[str, dict[str, Any]] = {
    "question_generation": {"questions": list},
    "answer_analysis": {"scores": dict},
    "followup_generation": {"followup": str},
    "final_scoring": {"overall_score": (int, float)},
    "tips_generation": {"quick_wins": list},
    "report_narrative": {"narrative": str},
    "resume_summary": {"headline": str},
    "answer_coaching": {"tip": str},
    "jd_metadata": {},
}


class _BoundaryPermit:
    __slots__ = ("task", "provider", "model")

    def __init__(self, task: str, provider: str, model: str) -> None:
        self.task = task
        self.provider = provider
        self.model = model


class ModelMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=80)
    messages: list[ModelMessage] = Field(min_length=1)
    authorized_providers: tuple[str, ...] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    expect_json: bool = False

    @field_validator("messages", mode="before")
    @classmethod
    def _coerce_messages(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        coerced: list[Any] = []
        for item in value:
            if isinstance(item, LLMMessage):
                coerced.append({"role": item.role, "content": item.content})
            elif isinstance(item, ModelMessage):
                coerced.append(item)
            else:
                coerced.append(item)
        return coerced

    @field_validator("authorized_providers", mode="before")
    @classmethod
    def _coerce_providers(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            raise ValueError("authorized_providers must be a sequence of provider names")
        return tuple(str(item) for item in value)

    def llm_messages(self) -> list[LLMMessage]:
        return [LLMMessage(role=item.role, content=item.content) for item in self.messages]

    def request_text(self) -> str:
        return "\n".join(item.content for item in self.messages)


def require_boundary_permit() -> None:
    if _permit.get() is None:
        raise SafetyViolationError("provider adapter invoked without safety boundary permit")


@contextmanager
def bound_provider_call(*, task: str, provider: str, model: str) -> Iterator[None]:
    token = _permit.set(_BoundaryPermit(task, provider, model))
    try:
        yield
    finally:
        _permit.reset(token)


def is_approved_endpoint(url: str, provider: str | None = None) -> bool:
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if host not in APPROVED_MODEL_HOSTS:
        return False
    if provider:
        allowed = PROVIDER_APPROVED_HOSTS.get(provider)
        if allowed is not None and host not in allowed:
            return False
    return True


def redact_secrets(text: str, extra_values: tuple[str, ...] | list[str] | None = None) -> str:
    if text is None:
        return ""
    value = str(text)
    value = _BEARER_RE.sub(r"\1***", value)
    value = _X_API_KEY_RE.sub(r"\1***", value)
    value = _GENERIC_BEARER_RE.sub("Bearer ***", value)
    value = _SECRET_VALUE_RE.sub("***", value)
    extras = [item for item in (extra_values or ()) if item and len(item) >= 8]
    extras.sort(key=len, reverse=True)
    for secret in extras:
        if secret in value:
            value = value.replace(secret, "***")
    return value


def contains_forbidden_secret(text: str, extra_values: tuple[str, ...] | list[str] | None = None) -> bool:
    if not text:
        return False
    if _SECRET_VALUE_RE.search(text):
        return True
    if _BEARER_RE.search(text) or _GENERIC_BEARER_RE.search(text):
        return True
    for secret in extra_values or ():
        if secret and len(secret) >= 8 and secret in text:
            return True
    return False


def assert_request_safe(request: ModelRequest, extra_values: tuple[str, ...] | list[str] | None = None) -> None:
    blob = request.request_text()
    if len(blob) > MAX_REQUEST_CHARS:
        raise SafetyViolationError("model request exceeds size limit")
    if contains_forbidden_secret(blob, extra_values):
        raise SafetyViolationError("model request contains credential or secret material")
    lowered = blob.lower()
    if "\x00" in blob:
        raise SafetyViolationError("model request contains binary material")
    if request.task.startswith("_") or "/" in request.task or "\\" in request.task:
        raise PolicyDeniedError("invalid task identifier")
    _ = lowered


def validate_provider_response(
    response: Any,
    extra_values: tuple[str, ...] | list[str] | None = None,
) -> ProviderResponse:
    if not isinstance(response, ProviderResponse):
        raise SafetyViolationError("adapter returned an invalid response type")
    if not isinstance(response.text, str):
        raise SafetyViolationError("adapter returned non-text content")
    if not response.text.strip():
        raise SafetyViolationError("adapter returned empty content")
    if len(response.text) > MAX_RESPONSE_CHARS:
        raise SafetyViolationError("adapter returned oversized content")
    if not isinstance(response.model, str) or not response.model.strip():
        raise SafetyViolationError("adapter returned an invalid model id")
    if contains_forbidden_secret(response.text, extra_values):
        raise SafetyViolationError("adapter returned secret material")
    response.raw = {}
    return response


def validate_structured_output(task: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SafetyViolationError("structured output must be a JSON object")
    spec = _TASK_SCHEMAS.get(str(task))
    if spec:
        for key, expected in spec.items():
            if key not in payload:
                raise SafetyViolationError(f"structured output missing field '{key}'")
            if not isinstance(payload[key], expected):
                raise SafetyViolationError(f"structured output field '{key}' has an invalid type")
    encoded = str(payload)
    if len(encoded) > MAX_RESPONSE_CHARS:
        raise SafetyViolationError("structured output exceeds size limit")
    return payload


def known_task(task: str, configured_tasks: dict[str, str]) -> bool:
    return bool(task) and task in configured_tasks
