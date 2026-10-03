"""Worker supervision: liveness heartbeats for /health/ready + restarts with backoff.

Every long-running worker is wrapped by :meth:`Supervisor.supervise`:
on crash the worker is restarted with exponential backoff (1s → 60s), each
restart bumps the ``worker_restarts{name}`` metric, and after
``ALERT_AFTER_RESTARTS`` consecutive crashes an ops alert is emitted.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

import structlog

log = structlog.get_logger(__name__)

# re-export: canonical home is scanner.workers (OOM zone thresholds shared by
# the OOM worker and tests — keep one implementation)
from scanner.workers import classify_oom_zone  # noqa: E402,F401

ALERT_AFTER_RESTARTS = 5
BACKOFF_START_SEC = 1.0
BACKOFF_MAX_SEC = 60.0
STABLE_RUN_SEC = 300.0  # AUDIT R5: stable run before streak reset

# heartbeats older than interval × HEARTBEAT_STALE_FACTOR count as dead
HEARTBEAT_STALE_FACTOR = 3.0


class HeartbeatRegistry:
    """Per-worker monotonic timestamps consumed by the readiness probe."""

    def __init__(self) -> None:
        self._beats: dict[str, float] = {}
        self._max_gap: dict[str, float] = {}

    def register(self, name: str, max_gap_sec: float) -> None:
        self._max_gap[name] = max_gap_sec
        self._beats.setdefault(name, time.monotonic())

    def beat(self, name: str) -> None:
        self._beats[name] = time.monotonic()

    def age(self, name: str) -> float | None:
        """Seconds since the last beat; None if the worker never reported."""
        ts = self._beats.get(name)
        return None if ts is None else time.monotonic() - ts

    def stale(self) -> list[str]:
        """Registered workers whose last beat is older than their max gap."""
        now = time.monotonic()
        return [
            name for name, gap in self._max_gap.items() if now - self._beats.get(name, 0.0) > gap
        ]


class Supervisor:
    """Restarts crashed workers with exponential backoff."""

    def __init__(
        self,
        metrics: object | None = None,
        alert: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.metrics = metrics
        self.alert = alert

    async def supervise(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        stop_event: asyncio.Event,
        heartbeat: HeartbeatRegistry | None = None,
        heartbeat_gap_sec: float = 90.0,
        heartbeat_name: str | None = None,
    ) -> None:
        """Run `factory()` forever; restart on failure until `stop_event` is set."""
        if heartbeat is not None:
            heartbeat.register(heartbeat_name or name, heartbeat_gap_sec)
        backoff = BACKOFF_START_SEC
        streak = 0
        while not stop_event.is_set():
            t0 = time.monotonic()
            try:
                await factory()
                if stop_event.is_set():
                    break
                # clean return while we should still be running == implicit crash
                raise RuntimeError("worker returned unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # AUDIT R5: a worker alive >= STABLE_RUN_SEC that then crashes
                # starts a new incident — reset counters, don't let a rare
                # failure accumulate backoff forever.
                if time.monotonic() - t0 >= STABLE_RUN_SEC:
                    streak = 0
                    backoff = BACKOFF_START_SEC
                streak += 1
                if self.metrics is not None:
                    self.metrics.inc_labeled("worker_restarts", {"name": name})  # type: ignore[attr-defined]
                log.warning(
                    "worker_restart",
                    worker=name,
                    streak=streak,
                    backoff_sec=backoff,
                    error=str(exc),
                )
                if streak == ALERT_AFTER_RESTARTS and self.alert is not None:
                    try:
                        await self.alert(
                            "worker_death",
                            f"worker `{name}` crashed {streak} times in a row: {exc}",
                        )
                    except Exception:
                        log.warning("alert_send_failed", worker=name)
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                backoff = min(backoff * 2.0, BACKOFF_MAX_SEC)
            else:  # pragma: no cover - unreachable (raise above), kept for clarity
                streak = 0
                backoff = BACKOFF_START_SEC
