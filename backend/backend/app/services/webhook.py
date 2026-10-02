import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import httpx

from app.config import get_settings
from app.core.errors import ValidationAppError
from app.core.logging import get_logger
from app.core.url_safety import validate_callback_url

logger = get_logger(__name__)


def _sign(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


async def deliver(url: str | None, event: str, data: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    target = url or settings.webhook_url
    if not target:
        return {"delivered": False, "reason": "no_webhook_url"}
    try:
        # Re-validate at send time: DNS may have changed since the interview was created.
        await asyncio.to_thread(validate_callback_url, target)
    except ValidationAppError as exc:
        logger.warning("webhook_blocked event=%s reason=%s", event, exc.message)
        return {"delivered": False, "reason": f"blocked: {exc.message}", "attempts": 0}

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
    async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds, follow_redirects=False) as client:
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


_background: set[asyncio.Task] = set()


def fire_and_forget(url: str | None, event: str, data: dict[str, Any]) -> None:
    task = asyncio.create_task(deliver(url, event, data))
    _background.add(task)
    task.add_done_callback(_background.discard)
