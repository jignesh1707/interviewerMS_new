import secrets

from fastapi import Header

from app.config import get_settings
from app.core.errors import AuthError


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> str:
    settings = get_settings()
    presented = x_api_key
    if not presented and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    if not presented:
        raise AuthError("missing API key; send X-API-Key header")
    valid = settings.api_key_set
    if not any(secrets.compare_digest(presented, key) for key in valid):
        raise AuthError("invalid API key")
    return presented
