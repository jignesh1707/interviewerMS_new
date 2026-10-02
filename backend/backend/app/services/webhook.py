import asyncio
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import get_settings
from app.core.errors import ValidationAppError
from app.core.logging import get_logger
from app.core.url_safety import validate_callback_url
from app.services.storage import get_async_store

logger = get_logger(__name__)


def _sign(payload_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


async def _prepare(
    url: str | None, event: str, data: dict[str, Any], delivery_id: str | None = None
) -> tuple[str, bytes, dict[str, str]] | dict[str, Any]:
    """Validate the target and build the signed request, or return a final result when nothing can be sent."""
    settings = get_settings()
    target = url or settings.webhook_url
    if not target:
        return {"delivered": False, "reason": "no_webhook_url", "retryable": False}
    try:
        # Re-validate at send time: DNS may have changed since the interview was created.
        await asyncio.to_thread(validate_callback_url, target)
    except ValidationAppError as exc:
        logger.warning("webhook_blocked event=%s reason=%s", event, exc.message)
        return {"delivered": False, "reason": f"blocked: {exc.message}", "attempts": 0, "retryable": False}

    body = {
        "event": event,
        "sent_at": time.time(),
        "data": data,
    }
    payload_bytes = json.dumps(body, separators=(",", ":"), default=str).encode("utf-8")
    if not settings.webhook_secret:
        if not settings.is_development:
            logger.error("webhook_blocked event=%s reason=no_webhook_secret", event)
            # Retryable: an operator can add the secret and queued deliveries then go out.
            return {
                "delivered": False,
                "reason": "WEBHOOK_SECRET is not configured",
                "attempts": 0,
                "retryable": True,
            }
        logger.warning("webhook_unsigned event=%s (development only)", event)
    timestamp = str(int(body["sent_at"]))
    headers = {
        "Content-Type": "application/json",
        "X-Interview-Event": event,
        "X-Interview-Delivery": delivery_id or uuid.uuid4().hex,
        "X-Interview-Timestamp": timestamp,
    }
    if settings.webhook_secret:
        # v1 signs the body only (kept for existing receivers); v2 also binds the timestamp so a
        # captured request cannot be replayed after the receiver's freshness window.
        headers["X-Interview-Signature"] = _sign(payload_bytes, settings.webhook_secret)
        headers["X-Interview-Signature-V2"] = _sign(
            timestamp.encode("ascii") + b"." + payload_bytes, settings.webhook_secret
        )
    return target, payload_bytes, headers


async def deliver(url: str | None, event: str, data: dict[str, Any]) -> dict[str, Any]:
    """Send now, retrying a few times inside this call. Prefer ``enqueue`` for anything that must not be lost."""
    settings = get_settings()
    prepared = await _prepare(url, event, data)
    if isinstance(prepared, dict):
        prepared.pop("retryable", None)
        return prepared
    target, payload_bytes, headers = prepared

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


async def send_once(url: str | None, event: str, data: dict[str, Any], delivery_id: str) -> dict[str, Any]:
    """One attempt. Returns ``delivered`` and, when it failed, whether a later retry could succeed."""
    settings = get_settings()
    prepared = await _prepare(url, event, data, delivery_id)
    if isinstance(prepared, dict):
        return prepared
    target, payload_bytes, headers = prepared
    try:
        async with httpx.AsyncClient(timeout=settings.webhook_timeout_seconds, follow_redirects=False) as client:
            response = await client.post(target, content=payload_bytes, headers=headers)
    except httpx.HTTPError as exc:
        return {"delivered": False, "reason": str(exc) or exc.__class__.__name__, "retryable": True}
    if response.status_code < 300:
        return {"delivered": True, "status": response.status_code}
    reason = f"HTTP {response.status_code}: {response.text[:200]}"
    retryable = response.status_code >= 500 or response.status_code == 429
    return {"delivered": False, "reason": reason, "retryable": retryable}


# ---------------------------------------------------------------------------------------------------------------
# Outbox: webhooks are stored first and sent by a background loop with exponential backoff, so a restart, a deploy
# or a receiver outage delays them instead of losing them. Delivery is at least once: a receiver should treat
# X-Interview-Delivery (stable across retries of the same row) as an idempotency key.

BACKOFF_SECONDS = (30, 120, 600, 3600, 21600, 43200)
OUTBOX_BATCH = 20
_wake: asyncio.Event | None = None


def _retry_delay(attempts: int) -> int:
    return BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]


def _iso(moment: datetime) -> str:
    return moment.isoformat()


async def enqueue(url: str | None, event: str, data: dict[str, Any], interview_id: str | None = None) -> bool:
    """Queue a webhook. Returns False when there is nowhere to send it."""
    target = url or get_settings().webhook_url
    if not target:
        return False
    await get_async_store().outbox_add(interview_id or data.get("interview_id"), target, event, data)
    if _wake is not None:
        _wake.set()
    return True


async def drain_once(limit: int = OUTBOX_BATCH) -> int:
    """Send what is due. Returns how many rows this call took responsibility for."""
    settings = get_settings()
    store = get_async_store()
    now = datetime.now(timezone.utc)
    rows = await store.outbox_due(_iso(now), limit)
    lock_until = _iso(now + timedelta(seconds=settings.webhook_timeout_seconds + 30))
    handled = 0
    for row in rows:
        if not await store.outbox_claim(row["id"], _iso(now), lock_until):
            continue  # another machine took it
        handled += 1
        attempts = row["attempts"] + 1
        result = await send_once(row["url"], row["event"], row["payload"], f"ob-{row['id']}")
        if result["delivered"]:
            await store.outbox_mark_delivered(row["id"], attempts)
            logger.info("webhook_delivered event=%s id=%s attempts=%d", row["event"], row["id"], attempts)
            continue
        reason = result.get("reason", "unknown")
        if not result.get("retryable") or attempts >= settings.webhook_outbox_max_attempts:
            await store.outbox_mark_dead(row["id"], attempts, reason)
            logger.error(
                "webhook_dead event=%s id=%s attempts=%d reason=%s", row["event"], row["id"], attempts, reason
            )
            continue
        next_attempt = datetime.now(timezone.utc) + timedelta(seconds=_retry_delay(attempts))
        await store.outbox_mark_retry(row["id"], attempts, _iso(next_attempt), reason)
        logger.warning(
            "webhook_retry event=%s id=%s attempts=%d reason=%s", row["event"], row["id"], attempts, reason
        )
    return handled


async def outbox_loop() -> None:
    global _wake
    settings = get_settings()
    _wake = asyncio.Event()
    last_prune = 0.0
    while True:
        handled = 0
        try:
            handled = await drain_once()
            if time.monotonic() - last_prune > 3600:
                cutoff = datetime.now(timezone.utc) - timedelta(days=settings.webhook_outbox_keep_days)
                await get_async_store().outbox_prune(_iso(cutoff))
                last_prune = time.monotonic()
        except Exception:  # noqa: BLE001
            logger.exception("webhook_outbox_failed")
        if handled >= OUTBOX_BATCH:
            continue
        try:
            await asyncio.wait_for(_wake.wait(), timeout=max(1, settings.webhook_outbox_poll_seconds))
        except asyncio.TimeoutError:
            pass
        _wake.clear()
