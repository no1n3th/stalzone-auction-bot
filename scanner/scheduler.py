"""Weighted scan scheduler with rate-limit budget awareness."""

from __future__ import annotations

import asyncio
import heapq
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from config.artifacts import ArtifactConfig, ArtifactRegistry

log = structlog.get_logger(__name__)


@dataclass(order=True)
class _ScheduledItem:
    due_at: float
    item_id: str = field(compare=False)


class WeightedScheduler:
    """Liquid items scan every `base_interval`; illiquid ones are slower:

    interval = base * illiquid_mult * (threshold/30)  (30% -> x1, 50% -> x1.67, 200% -> x6.67)
    illiquid without threshold -> base * illiquid_default_weight (x6.67).
    """

    def __init__(
        self,
        registry: ArtifactRegistry,
        base_interval: float = 60.0,
        illiquid_mult: float = 10.0,
        illiquid_default_weight: float = 6.67,
    ) -> None:
        self.registry = registry
        self.base_interval = base_interval
        self.illiquid_mult = illiquid_mult
        self.illiquid_default_weight = illiquid_default_weight
        self.load_factor = 1.0  # OOM yellow zone raises this to slow illiquid scans
        self._heap: list[_ScheduledItem] = []
        self._deferred: set[str] = set()
        self.rebuild()

    def interval_for(self, artifact: ArtifactConfig) -> float:
        if artifact.category == "liquid":
            return self.base_interval
        if artifact.profit_threshold_pct:
            weight = artifact.profit_threshold_pct / 30.0
        else:
            weight = self.illiquid_default_weight
        return self.base_interval * self.illiquid_mult * weight * self.load_factor

    def rebuild(self) -> None:
        # AUDIT D4: preserve due timers of existing items (rebuild is called on
        # every artifacts.yaml reload / OOM zone change and used to reset all
        # timers, delaying every scan by a full interval); a cold start spreads
        # first scans with jitter so 42 liquid items don't fire in one second.
        now = time.monotonic()
        previous: dict[str, float] = {}
        for it in self._heap:
            previous[it.item_id] = min(previous.get(it.item_id, it.due_at), it.due_at)
        cold_start = not previous
        self._heap = []
        for art in self.registry.all():
            if art.item_id in previous:
                due = previous[art.item_id]
            elif cold_start:
                due = now + self.interval_for(art) * random.uniform(0.15, 1.0)
            else:
                due = now + self.interval_for(art)
            heapq.heappush(self._heap, _ScheduledItem(due, art.item_id))
        self._deferred.clear()

    def defer(self, item_id: str, delay: float) -> None:
        # AUDIT D4: one heap entry per item — the old version pushed an extra
        # entry on every defer (5 defers -> 6 entries), scanning the item more
        # often than its interval and burning API budget in a feedback loop.
        self._deferred.add(item_id)
        self._heap = [it for it in self._heap if it.item_id != item_id]
        heapq.heapify(self._heap)
        heapq.heappush(self._heap, _ScheduledItem(time.monotonic() + delay, item_id))

    def due_ids(self, limit: int = 8) -> list[str]:
        now = time.monotonic()
        out: list[str] = []
        skipped: list[_ScheduledItem] = []
        while self._heap and len(out) < limit:
            item = heapq.heappop(self._heap)
            if item.due_at > now:
                skipped.append(item)
                break
            art = self.registry.get(item.item_id)
            if art is not None:
                out.append(item.item_id)
                skipped.append(_ScheduledItem(now + self.interval_for(art), item.item_id))
        for item in skipped:
            heapq.heappush(self._heap, item)
        return out

    def next_due_in(self) -> float:
        if not self._heap:
            return self.base_interval
        return max(0.0, self._heap[0].due_at - time.monotonic())

    @property
    def deferred_count(self) -> int:
        return len(self._deferred)


async def scheduler_loop(
    scheduler: WeightedScheduler,
    acquire_budget: Callable[[], Awaitable[bool]],
    scan_item: Callable[[str], Awaitable[None]],
    batch_size: int = 8,
    stop_event: asyncio.Event | None = None,
    *,
    concurrency: int = 4,
    heartbeat: Any = None,
    is_enabled: Any = None,
) -> None:
    """`is_enabled` (P1-10): when the admin flag `scanner_enabled` is off the
    loop idles instead of consuming the API budget."""
    """Main scan loop: never violates the API budget — defers items when short.

    Budget checks stay sequential (cheap, non-consuming), the scans themselves
    run in parallel under `concurrency` semaphore.
    """
    stop = stop_event or asyncio.Event()
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run(item_id: str) -> None:
        async with sem:
            if stop.is_set():
                return
            try:
                await scan_item(item_id)
            except Exception as exc:
                log.error("scan_item_failed", item_id=item_id, error=str(exc))

    scan_count = 0
    while not stop.is_set():
        if heartbeat is not None:
            heartbeat.beat("scan")  # пульс в /health/ready
            scan_count += 1
            if scan_count % 300 == 0:  # ~раз в 30 с: сканер жив
                log.info("scanner_alive", scans=scan_count)
        if is_enabled is not None and not is_enabled():
            await asyncio.sleep(5.0)
            continue
        ids = scheduler.due_ids(limit=batch_size)
        if not ids:
            await asyncio.sleep(min(scheduler.next_due_in(), 5.0))
            continue
        ready: list[str] = []
        for item_id in ids:
            if stop.is_set():
                break
            if not await acquire_budget():
                # not enough API budget -> push the item back, never violate the limit
                scheduler.defer(item_id, delay=5.0)
                log.info("scheduler_deferred", item_id=item_id)
                continue
            scheduler._deferred.discard(item_id)
            ready.append(item_id)
        if ready:
            await asyncio.gather(*(_run(item_id) for item_id in ready))
        await asyncio.sleep(0.1)
