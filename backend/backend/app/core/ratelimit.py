"""Rate limiting and a per-tenant daily budget.

State lives in Redis (for example Upstash) when ``REDIS_URL`` is set, so every machine shares one view.
If Redis is unset or unreachable the limits fall back to per-process memory: protection degrades to
"per machine" instead of failing requests.
"""

import asyncio
import math
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from typing import Any

from app.config import get_settings
from app.core.errors import RateLimitError
from app.core.logging import get_logger

logger = get_logger(__name__)


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window: float = 60.0) -> None:
        """Record a hit for key, raising RateLimitError once more than `limit` hits fall in `window`."""
        if limit <= 0:
            return
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= window:
                hits.popleft()
            if len(hits) >= limit:
                retry = max(1, math.ceil(window - (now - hits[0])))
                raise RateLimitError(
                    "rate limit exceeded; slow down",
                    details={"retry_after_seconds": retry},
                    headers={"Retry-After": str(retry)},
                )
            hits.append(now)

    def peek_blocked(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= window:
                hits.popleft()
            return len(hits) >= limit

    def record(self, key: str) -> None:
        with self._lock:
            self._hits[key].append(time.monotonic())

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class DailyBudget:
    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def consume(self, tenant: str, budget: int) -> None:
        if budget <= 0:
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            self._counts = {k: v for k, v in self._counts.items() if k[1] == today}
            used = self._counts.get((tenant, today), 0)
            if used >= budget:
                raise RateLimitError(
                    "daily usage budget exhausted for this API key",
                    details={"budget": budget},
                    headers={"Retry-After": "3600"},
                )
            self._counts[(tenant, today)] = used + 1

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


# ---------------------------------------------------------------------------- Redis

_HIT_SCRIPT = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return {c, redis.call('TTL', KEYS[1])}
"""

_clients: dict[int, Any] = {}
_scripts: dict[int, Any] = {}
_last_warning = 0.0


def _make_client(url: str) -> Any:
    import redis.asyncio as redis_asyncio

    return redis_asyncio.from_url(
        url,
        decode_responses=True,
        socket_timeout=1.0,
        socket_connect_timeout=1.0,
        health_check_interval=30,
    )


def _redis() -> tuple[Any, Any] | None:
    """Return (client, hit script) for the running loop, or None when Redis is not configured."""
    url = get_settings().redis_url
    if not url:
        return None
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _clients:
        client = _make_client(url)
        _clients[loop_id] = client
        _scripts[loop_id] = client.register_script(_HIT_SCRIPT)
    return _clients[loop_id], _scripts[loop_id]


def _warn_once(message: str, exc: Exception) -> None:
    global _last_warning
    now = time.monotonic()
    if now - _last_warning > 30:
        _last_warning = now
        logger.warning("%s; falling back to per-process limits (%s)", message, type(exc).__name__)


def _key(*parts: str) -> str:
    return ":".join([get_settings().redis_key_prefix, *parts])


async def redis_status() -> str:
    """'disabled', 'ok' or 'unavailable' for the readiness details."""
    pair = _redis()
    if pair is None:
        return "disabled"
    try:
        await pair[0].ping()
        return "ok"
    except Exception:  # noqa: BLE001
        return "unavailable"


class SharedLimiter:
    """Fixed-window counters in Redis (one command per check), sliding window in memory as a fallback."""

    def __init__(self) -> None:
        self.memory = SlidingWindowLimiter()

    async def check(self, key: str, limit: int, window: float = 60.0) -> None:
        if limit <= 0:
            return
        pair = _redis()
        if pair is not None:
            try:
                window_id = int(time.time() // window)
                count, ttl = await pair[1](keys=[_key("rl", key, str(window_id))], args=[int(window) + 1])
                if int(count) > limit:
                    retry = max(1, int(ttl))
                    raise RateLimitError(
                        "rate limit exceeded; slow down",
                        details={"retry_after_seconds": retry},
                        headers={"Retry-After": str(retry)},
                    )
                return
            except RateLimitError:
                raise
            except Exception as exc:  # noqa: BLE001
                _warn_once("redis rate limit unavailable", exc)
        self.memory.check(key, limit, window)

    async def is_blocked(self, key: str, limit: int, window: float = 60.0) -> bool:
        pair = _redis()
        if pair is not None:
            try:
                window_id = int(time.time() // window)
                current = await pair[0].get(_key("rl", key, str(window_id)))
                return int(current or 0) >= limit
            except Exception as exc:  # noqa: BLE001
                _warn_once("redis rate limit unavailable", exc)
        return self.memory.peek_blocked(key, limit, window)

    async def record(self, key: str, window: float = 60.0) -> None:
        pair = _redis()
        if pair is not None:
            try:
                window_id = int(time.time() // window)
                await pair[1](keys=[_key("rl", key, str(window_id))], args=[int(window) + 1])
                return
            except Exception as exc:  # noqa: BLE001
                _warn_once("redis rate limit unavailable", exc)
        self.memory.record(key)

    def reset(self) -> None:
        self.memory.reset()


class SharedBudget:
    """Per-tenant daily counter, shared through Redis when available."""

    def __init__(self) -> None:
        self.memory = DailyBudget()

    async def consume(self, tenant: str, budget: int) -> None:
        if budget <= 0:
            return
        pair = _redis()
        if pair is not None:
            try:
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                count, _ = await pair[1](keys=[_key("budget", tenant, today)], args=[172_800])
                if int(count) > budget:
                    raise RateLimitError(
                        "daily usage budget exhausted for this API key",
                        details={"budget": budget},
                        headers={"Retry-After": "3600"},
                    )
                return
            except RateLimitError:
                raise
            except Exception as exc:  # noqa: BLE001
                _warn_once("redis budget unavailable", exc)
        self.memory.consume(tenant, budget)

    def reset(self) -> None:
        self.memory.reset()


limiter = SharedLimiter()
budget = SharedBudget()
