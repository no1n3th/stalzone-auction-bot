"""Login throttling: sliding window per key (ip:username and global ip:).

Keys are evicted lazily when the table grows past a cap, so an attacker
spraying random usernames cannot grow memory without bound.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class LoginThrottle:
    def __init__(
        self,
        max_attempts: int = 10,
        window_sec: float = 300.0,
        max_keys: int = 10_000,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self.max_keys = max_keys
        self._clock = clock or time.monotonic  # test hook (test_web_auth TestThrottleUnit)
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._failures.get(key)
        if hits is None:
            return deque()
        while hits and now - hits[0] >= self.window_sec:
            hits.popleft()
        if not hits:
            self._failures.pop(key, None)
        return hits

    def retry_after(self, key: str) -> float:
        now = self._clock()
        hits = self._prune(key, now)
        if len(hits) < self.max_attempts:
            return 0.0
        return max(0.0, self.window_sec - (now - hits[0]))

    def fail(self, key: str) -> None:
        now = self._clock()
        hits = self._failures.setdefault(key, deque())
        hits.append(now)
        # lazy eviction of stale keys when the table grows too large
        if len(self._failures) > self.max_keys:
            for stale in [k for k in self._failures if not self._prune(k, now)]:
                self._failures.pop(stale, None)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
