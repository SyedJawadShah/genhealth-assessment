"""In-process fixed-window rate limiting.

State lives in memory, so limits apply per process and reset on restart. That fits a single
container; several replicas would need a shared store.
"""

import hashlib
import math
import threading
import time
from collections.abc import Callable

from fastapi import HTTPException

PRUNE_INTERVAL_SECONDS = 60


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # sha256(key) -> (window start, count, window length). Hashing bounds the memory per
        # key whatever the client sends (usernames, IPs).
        self._windows: dict[str, tuple[float, int, float]] = {}
        self._last_prune = clock()

    def hit(self, key: str, limit: int, window_seconds: float) -> None:
        """Count one request for `key`; raise 429 with Retry-After once `limit` is passed."""
        now = self._clock()
        key = hashlib.sha256(key.encode()).hexdigest()
        with self._lock:
            if now - self._last_prune >= PRUNE_INTERVAL_SECONDS:
                self._prune(now)
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

    def _prune(self, now: float) -> None:
        # Drop expired windows once a minute so memory tracks active clients only.
        self._last_prune = now
        expired = [k for k, (start, _, length) in self._windows.items() if now - start >= length]
        for key in expired:
            del self._windows[key]
