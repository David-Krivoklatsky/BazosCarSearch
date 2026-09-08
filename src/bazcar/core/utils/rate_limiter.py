"""Async rate limiter enforcing a minimum interval between requests.

A simple token-style limiter: a new request may start only after
``min_interval`` has elapsed since the previous one (plus optional random
jitter to look more like a human and avoid pattern detection).
"""

from __future__ import annotations

import asyncio
import random


class AsyncRateLimiter:
    def __init__(self, min_interval_ms: float, jitter_ms: float = 0.0) -> None:
        self.min_interval_s = min_interval_ms / 1000.0
        self.jitter_s = jitter_ms / 1000.0
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        """Block until this request slot is allowed, then reserve the next one."""
        async with self._lock:
            now = asyncio.get_running_loop().time()
            delay = self._next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
            jitter = random.uniform(0.0, self.jitter_s) if self.jitter_s else 0.0
            self._next_allowed = (
                asyncio.get_running_loop().time() + self.min_interval_s + jitter
            )
