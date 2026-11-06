"""In-process fixed-window rate limiting.

State lives in memory, so limits apply per process and reset on restart. That fits a single
container; several replicas would need a shared store.
"""

import math
import threading
import time
from collections.abc import Callable

from fastapi import HTTPException


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (window start, count, window length)
        self._windows: dict[str, tuple[float, int, float]] = {}

    def hit(self, key: str, limit: int, window_seconds: float) -> None:
        """Count one request for `key`; raise 429 with Retry-After once `limit` is passed."""
        now = self._clock()
        with self._lock:
            started, count, _ = self._windows.get(key, (now, 0, window_seconds))
            if now - started >= window_seconds:
                started, count = now, 0
            if count >= limit:
                retry_after = math.ceil(window_seconds - (now - started))
                raise HTTPException(
                    status_code=429,
                    detail="rate_limited",
                    headers={"Retry-After": str(max(retry_after, 1))},
                )
            self._windows[key] = (started, count + 1, window_seconds)
            if len(self._windows) > 100_000:
                self._evict(now)

    def _evict(self, now: float) -> None:
        # Bound memory under key churn (e.g. many client IPs) by dropping expired windows.
        expired = [k for k, (start, _, length) in self._windows.items() if now - start >= length]
        for key in expired:
            del self._windows[key]
