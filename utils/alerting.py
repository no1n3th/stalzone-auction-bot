"""Ops alerting: dedup-by-kind queue drained by the alerting worker."""

from __future__ import annotations

import asyncio
import time

import structlog

log = structlog.get_logger(__name__)


class AlertManager:
    """Collects ops alerts (401, breaker open, worker death, OOM-critical).

    Producers call :meth:`notify` (non-blocking). ``alerting_worker`` drains
    the queue and POSTs to the alerting webhook, honoring a per-kind cooldown
    so a flapping subsystem cannot spam the ops channel.

    P3-2: `enabled=False` (no alerting webhook configured) means no queue is
    created at all — producers drop silently instead of filling the queue to
    maxsize and logging `alert_queue_full` against a consumer that does not
    exist.
    """

    def __init__(
        self, cooldown_sec: float = 300.0, queue_size: int = 100, enabled: bool = True
    ) -> None:
        self.cooldown_sec = cooldown_sec
        self.enabled = enabled
        self.queue: asyncio.Queue[tuple[str, str]] | None = (
            asyncio.Queue(maxsize=queue_size) if enabled else None
        )
        self._last_sent: dict[str, float] = {}

    async def notify(self, kind: str, text: str) -> None:
        if not self.enabled or self.queue is None:
            return
        try:
            self.queue.put_nowait((kind, text))
        except asyncio.QueueFull:
            log.warning("alert_queue_full", kind=kind)

    def allowed(self, kind: str) -> bool:
        last = self._last_sent.get(kind)
        return last is None or (time.monotonic() - last) >= self.cooldown_sec

    def mark_sent(self, kind: str) -> None:
        self._last_sent[kind] = time.monotonic()
