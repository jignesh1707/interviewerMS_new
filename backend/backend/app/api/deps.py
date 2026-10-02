import secrets
from typing import Annotated

from fastapi import Depends, Header, Request

from app.config import get_settings
from app.core.errors import AuthError, RateLimitError
from app.core.ratelimit import budget, limiter


def _client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> str:
    """Authenticate the caller, apply the per-tenant rate limit, and return its tenant id."""
    settings = get_settings()
    client = _client_id(request)
    fail_key = f"authfail:{client}"
    if await limiter.is_blocked(fail_key, settings.auth_fail_limit_per_minute):
        raise RateLimitError(
            "too many failed authentication attempts", headers={"Retry-After": "60"}
        )
    presented = x_api_key
    if not presented and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented:
        raise AuthError("missing API key; send X-API-Key header")
    matched: str | None = None
    for key, tenant in settings.api_key_entries.items():
        if secrets.compare_digest(presented, key):
            matched = tenant
    if matched is None:
        await limiter.record(fail_key)
        raise AuthError("invalid API key")
    await limiter.check(f"tenant:{matched}", settings.rate_limit_per_minute)
    return matched


async def enforce_student_limits(tenant: str, external_ref: str | None) -> None:
    """Per-student rate limit and daily budget. Call it once the student is known (their external_ref).

    Keyed on tenant and student so two tenants can reuse the same id. Skipped when no student id was sent.
    """
    if not external_ref:
        return
    settings = get_settings()
    subject = f"student:{tenant}:{external_ref}"
    await limiter.check(subject, settings.student_rate_limit_per_minute)
    await budget.consume(subject, settings.student_daily_budget, scope="student")


async def expensive_call(tenant: Annotated[str, Depends(require_api_key)]) -> None:
    """Stricter limit plus daily budget for endpoints that run LLM, speech-to-text or TTS work."""
    settings = get_settings()
    await limiter.check(f"expensive:{tenant}", settings.rate_limit_expensive_per_minute)
    await budget.consume(tenant, settings.daily_expensive_budget)
