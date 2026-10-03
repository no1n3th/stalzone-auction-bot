"""Circuit breaker: CLOSED -> OPEN -> HALF_OPEN state machine (no external deps)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Awaitable, Callable
from enum import IntEnum
from typing import TypeVar

import structlog

logger = structlog.get_logger("stalzone.circuit")

T = TypeVar("T")


class CircuitState(IntEnum):
    CLOSED = 0
    HALF_OPEN = 1
    OPEN = 2


class CircuitOpenError(RuntimeError):
    """Raised when the circuit is open and calls are rejected."""


class CircuitBreaker:
    """
    failure_threshold failures within window_sec -> OPEN.
    After recovery_timeout -> HALF_OPEN (up to half_open_max_calls probes).
    half_open_max_calls successes -> CLOSED; any failure -> OPEN again.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        window_sec: float = 60.0,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
        on_state_change: Callable[[CircuitState], None] | None = None,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.window_sec = window_sec
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self._on_state_change = on_state_change
        self._state = CircuitState.CLOSED
        self._failures: deque[float] = deque()
        self._opened_at: float = 0.0
        self._half_open_in_flight = 0
        self._half_open_successes = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, coro_factory: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            self._maybe_transition_locked()
            if self._state == CircuitState.OPEN:
                raise CircuitOpenError(f"Circuit '{self.name}' is OPEN")
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_in_flight >= self.half_open_max_calls:
                    raise CircuitOpenError(f"Circuit '{self.name}' is HALF_OPEN and busy")
                self._half_open_in_flight += 1
        try:
            result = await coro_factory()
        except Exception:
            await self._on_failure()
            raise
        await self._on_success()
        return result

    async def reset(self) -> None:
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failures.clear()
            self._half_open_in_flight = 0
            self._half_open_successes = 0

    def _maybe_transition_locked(self) -> None:
        if (
            self._state == CircuitState.OPEN
            and (time.monotonic() - self._opened_at) >= self.recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            self._half_open_in_flight = 0
            self._half_open_successes = 0
            self._notify()

    async def _on_failure(self) -> None:
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                self._notify()
                return
            now = time.monotonic()
            self._failures.append(now)
            cutoff = now - self.window_sec
            while self._failures and self._failures[0] < cutoff:
                self._failures.popleft()
            if len(self._failures) >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = now
                self._notify()

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._half_open_successes += 1
                if self._half_open_successes >= self.half_open_max_calls:
                    self._state = CircuitState.CLOSED
                    self._failures.clear()
                    self._notify()

    def _notify(self) -> None:
        logger.info("Circuit state changed", circuit=self.name, state=self._state.name)
        if self._on_state_change is not None:
            with contextlib.suppress(Exception):
                self._on_state_change(self._state)

    # --- lightweight sync probes (single event loop; used by the API client) ---
    # Semantics mirror the async call() path: a HALF_OPEN failure re-opens the
    # circuit immediately, and recovery takes `half_open_max_calls` successes.
    def allow(self) -> bool:
        if self._state == CircuitState.OPEN:
            if (time.monotonic() - self._opened_at) >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_successes = 0
                self._notify()
                return True
            return False
        return True

    def on_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._half_open_successes += 1
            if self._half_open_successes >= self.half_open_max_calls:
                self._state = CircuitState.CLOSED
                self._failures.clear()
                self._notify()
            return
        if self._state != CircuitState.CLOSED:
            self._state = CircuitState.CLOSED
            self._failures.clear()
            self._notify()

    def on_failure(self) -> None:
        now = time.monotonic()
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._opened_at = now
            self._notify()
            return
        self._failures.append(now)
        cutoff = now - self.window_sec
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        if len(self._failures) >= self.failure_threshold and self._state != CircuitState.OPEN:
            self._state = CircuitState.OPEN
            self._opened_at = now
            self._notify()
