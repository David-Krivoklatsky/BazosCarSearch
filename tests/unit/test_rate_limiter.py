"""Rate limiter behaviour tests."""

from __future__ import annotations

import asyncio
import time

from bazcar.core.utils import AsyncRateLimiter


def test_min_interval_enforced() -> None:
    async def run() -> float:
        limiter = AsyncRateLimiter(min_interval_ms=60, jitter_ms=0)
        start = time.monotonic()
        await limiter.wait()
        await limiter.wait()
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    assert elapsed >= 0.05  # allow small scheduling slack, must respect ~60ms


def test_zero_interval_is_fast() -> None:
    async def run() -> float:
        limiter = AsyncRateLimiter(min_interval_ms=0, jitter_ms=0)
        start = time.monotonic()
        for _ in range(10):
            await limiter.wait()
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    assert elapsed < 0.05
