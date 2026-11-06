import pytest
from fastapi import HTTPException

from app.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limits_per_key_and_resets_after_the_window() -> None:
    clock = Clock()
    limiter = RateLimiter(clock)

    limiter.hit("a", 2, 60)
    limiter.hit("a", 2, 60)
    limiter.hit("b", 2, 60)
    with pytest.raises(HTTPException) as info:
        limiter.hit("a", 2, 60)
    assert info.value.status_code == 429
    assert info.value.headers == {"Retry-After": "60"}

    clock.now += 45
    with pytest.raises(HTTPException) as info:
        limiter.hit("a", 2, 60)
    assert info.value.headers == {"Retry-After": "15"}

    clock.now += 15
    limiter.hit("a", 2, 60)


def test_expired_windows_are_evicted_when_many_keys_exist() -> None:
    clock = Clock()
    limiter = RateLimiter(clock)
    for i in range(100_001):
        limiter.hit(f"k{i}", 1, 1)
    clock.now += 2

    limiter.hit("fresh", 1, 1)

    assert len(limiter._windows) == 1
