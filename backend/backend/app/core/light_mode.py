"""LIGHT_MODE: a small, always-on instance that answers only cheap read requests.

Resumeto reads a student's minutes from many places (profile, voice page, booking, admin screens, refund and alert
jobs). Sent to the big speech machines, each of those reads can wake one. A light instance is a separate Fly app
(fly.light.toml) that serves those reads from the same database and never loads the speech model.

The guard is an ALLOWLIST, so a route added later is refused in light mode until someone adds it here on purpose.
It runs before routing and before the request body is read, so a speech upload sent to the wrong address is turned
away without being buffered.
"""

import json
import re

from app.config import get_settings
from app.core.errors import NotFoundError

LIGHT_MODE_CODE = "light_mode_route"

# (method, path) pairs served in light mode. Everything else, including every write, is refused.
_ALLOWED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern))
    for method, pattern in (
        ("GET", r"/"),
        ("GET", r"/api/v1/health"),
        ("GET", r"/api/v1/ready"),
        ("GET", r"/api/v1/ready/details"),
        ("GET", r"/api/v1/plans"),
        ("GET", r"/api/v1/packs/[^/]+"),  # a student's balance on every plan
    )
)

# Human-readable form for docs and tests.
ALLOWED_ROUTES: tuple[str, ...] = (
    "GET /",
    "GET /api/v1/health",
    "GET /api/v1/ready",
    "GET /api/v1/ready/details",
    "GET /api/v1/plans",
    "GET /api/v1/packs/{external_ref}",
)


class LightRouteRefused(NotFoundError):
    code = LIGHT_MODE_CODE


def is_allowed(method: str, path: str) -> bool:
    if method == "HEAD":
        method = "GET"
    path = path.rstrip("/") or "/"
    return any(method == allowed_method and pattern.fullmatch(path) for allowed_method, pattern in _ALLOWED)


class LightModeMiddleware:
    """Refuse every request outside the allowlist while ``settings.light_mode`` is on. Pass-through when it is off."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not get_settings().light_mode:
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        # CORS preflights carry no data and are answered by the CORS middleware.
        if method == "OPTIONS" or is_allowed(method, scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        error = LightRouteRefused(
            "this route is not served by the light instance; use the normal Interviewer address",
            details={"method": method, "path": scope.get("path", "")},
        )
        body = json.dumps(error.to_dict()).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": error.status_code,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})
