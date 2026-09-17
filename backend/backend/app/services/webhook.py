import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _sign(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


async def deliver(url: str | None, event: str, data: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    target = url or settings.webhook_url
    if not target:
        return {"delivered": False, "reason": "no_webhook_url"}

    body = {
        "event": event,
        "sent_at": time.time(),
        "data": data,
    }
    payload_bytes = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Interview-Event": event}
    if settings.webhook_secret:
        headers["X-Interview-Signature"] = _sign(payload_bytes, settings.webhook_secret)

    last_error = ""
    async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds) as client:
        for attempt in range(1, settings.webhook_max_attempts + 1):
            try:
                response = await client.post(target, content=payload_bytes, headers=headers)
                if response.status_code < 300:
                    logger.info("webhook_delivered event=%s status=%s", event, response.status_code)
                    return {"delivered": True, "status": response.status_code, "attempts": attempt}
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code < 500 and response.status_code != 429:
                    logger.warning("webhook_rejected event=%s error=%s", event, last_error)
                    return {"delivered": False, "reason": last_error, "attempts": attempt}
            except httpx.HTTPError as exc:
                last_error = str(exc)
            await asyncio.sleep(min(2 ** attempt, 8))

    logger.error("webhook_failed event=%s error=%s", event, last_error)
    return {"delivered": False, "reason": last_error, "attempts": settings.webhook_max_attempts}


def fire_and_forget(url: str | None, event: str, data: dict[str, Any]) -> None:
    asyncio.create_task(deliver(url, event, data))
