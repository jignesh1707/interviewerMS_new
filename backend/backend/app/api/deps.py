import secrets

from fastapi import Header

from app.config import get_settings
from app.core.errors import AuthError


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> str:
    """Authenticate the caller and return its tenant id."""
    settings = get_settings()
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
        raise AuthError("invalid API key")
    return matched
