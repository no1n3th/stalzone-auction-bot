"""Async token bucket rate limiter for the Stalcraft API."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping


class TokenBucket:
    """
    Token bucket: capacity=400, refill_rate=400/60 by default.
    acquire() waits when empty (never skips). Server hints from
    x-ratelimit-remaining / x-ratelimit-reset (unix ms) are honored.
    """

    def __init__(self, capacity: int = 400, refill_rate: float = 400.0 / 60.0) -> None:
        self.capacity = capacity
        self.refill_rate = refill_rate
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._reset_at: float | None = None  # monotonic ts of server budget reset
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        if self._reset_at is not None and now >= self._reset_at:
            self._tokens = float(self.capacity)
            self._reset_at = None
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(float(self.capacity), self._tokens + elapsed * self.refill_rate)
            self._updated = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """Wait until `tokens` are available and consume them."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.refill_rate if self.refill_rate > 0 else 1.0
                if self._reset_at is not None:
                    wait = max(wait, self._reset_at - time.monotonic())
            await asyncio.sleep(min(max(wait, 0.01), 5.0))

    async def update_from_headers(self, headers: Mapping[str, str]) -> None:
        """Sync local budget with server-side rate-limit headers."""
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        async with self._lock:
            try:
                if remaining is not None:
                    self._tokens = min(self._tokens, float(int(remaining)))
                    self._updated = time.monotonic()
            except (TypeError, ValueError):
                pass
            try:
                if reset:
                    # reset is unix epoch in milliseconds (production probe)
                    reset_sec = int(reset) / 1000.0
                    self._reset_at = time.monotonic() + max(0.0, reset_sec - time.time())
            except (TypeError, ValueError):
                pass

    async def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking acquire: consume tokens if available, else return False."""
        async with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens
