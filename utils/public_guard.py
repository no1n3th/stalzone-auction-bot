"""P2: sliding-window per-IP limiter + short TTL cache for public GETs.

Covers /api/market, /api/daily-prices, /api/forecast — the unauthenticated
endpoints that hit the DB on every request.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from fastapi import HTTPException, Request, status


class PublicEndpointGuard:
    """In-memory guard: 30 requests/min per IP, 45-second response cache."""

    def __init__(self, window_sec: float = 60.0, max_requests: int = 30, cache_sec: float = 45.0):
        self.window_sec = window_sec
        self.max_requests = max_requests
        self.cache_sec = cache_sec
        self._hits: dict[str, deque[float]] = {}
        self._cache: dict[str, tuple[float, Any]] = {}

    async def limit(self, request: Request) -> None:
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        dq = self._hits.setdefault(ip, deque())
        while dq and now - dq[0] >= self.window_sec:
            dq.popleft()
        if len(dq) >= self.max_requests:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit")
        dq.append(now)

    def cached(self, key: str) -> Any | None:
        ent = self._cache.get(key)
        if ent and time.monotonic() - ent[0] < self.cache_sec:
            return ent[1]
        return None

    def store(self, key: str, value: Any) -> Any:
        self._cache[key] = (time.monotonic(), value)
        return value
