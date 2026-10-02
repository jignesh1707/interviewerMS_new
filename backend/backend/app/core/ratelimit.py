"""In-process sliding-window rate limiting and a per-tenant daily budget."""

import math
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from app.core.errors import RateLimitError


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


limiter = SlidingWindowLimiter()
budget = DailyBudget()
