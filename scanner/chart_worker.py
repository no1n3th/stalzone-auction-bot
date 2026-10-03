"""Background chart rendering: keeps the lot→Discord fast path under p95 1.5s.

Profitable lots are enqueued to Discord immediately, without an image.
This service renders the price chart off the hot path (semaphore-limited,
day-keyed LRU cache on disk) and then PATCHes the already-sent Discord
message via ``DiscordNotifier.attach_chart``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from api.discord import DiscordNotifier
from config.settings import Settings
from database.engine import Database
from database.repositories.history import HistoryRepository
from metrics.metrics import MetricsCollector
from models.lot import Lot
from utils.chart import ChartCache, render_price_chart

log = structlog.get_logger(__name__)


class ChartService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        notifier: DiscordNotifier,
        cache: ChartCache,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.notifier = notifier
        self.cache = cache
        self.metrics = metrics
        self.queue: asyncio.Queue[tuple[Lot, str]] = asyncio.Queue(
            maxsize=max(1, settings.chart_queue_size)
        )
        self._sem = asyncio.Semaphore(max(1, settings.chart_render_workers))
        self._tasks: set[asyncio.Task[None]] = set()
        self.enabled = settings.chart_enabled  # OOM yellow zone flips this off

    def enqueue(self, lot: Lot, chart_key: str) -> bool:
        """Non-blocking submit; drops (and counts) when disabled or full."""
        if not self.enabled:
            return False
        try:
            self.queue.put_nowait((lot, chart_key))
            return True
        except asyncio.QueueFull:
            if self.metrics is not None:
                self.metrics.inc("chart_queue_dropped")
            log.warning("chart_queue_full", item_id=lot.item_id)
            return False

    async def run(self, stop_event: asyncio.Event, heartbeat: Any = None) -> None:
        """Worker loop: consumes jobs until stopped, then drains in-flight renders."""
        while not stop_event.is_set():
            if heartbeat is not None:
                heartbeat.beat("chart_worker")
            try:
                job = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            task = asyncio.create_task(self._process(*job))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _process(self, lot: Lot, chart_key: str) -> None:
        async with self._sem:
            try:
                png = await self.cache.get_async(chart_key)
                if png is None:
                    png = await self._render(lot)
                    if png is not None:
                        await self.cache.put_async(chart_key, png)
            except Exception as exc:
                if self.metrics is not None:
                    self.metrics.inc("chart_render_failed")
                log.warning("chart_render_failed", item_id=lot.item_id, error=str(exc))
                return
        if png is None:
            return
        try:
            await self.notifier.attach_chart(lot.signature, png)
            if self.metrics is not None:
                self.metrics.inc("charts_attached")
        except Exception as exc:
            if self.metrics is not None:
                self.metrics.inc("chart_attach_failed")
            log.warning("chart_attach_failed", item_id=lot.item_id, error=str(exc))

    async def _render(self, lot: Lot) -> bytes | None:
        async with self.db.session() as session:
            history = HistoryRepository(session, rows_per_item=self.settings.history_rows_per_item)
            daily = await history.daily_avg(lot.item_id, lot.quality, lot.upgrade, days=30)
        # P1-5: the day-keyed cache is shared by every lot of the group - the
        # lot-price marker of the first lot leaked into the others' charts.
        # The price already sits in the Discord embed; do not bake it into PNG.
        return await asyncio.to_thread(
            render_price_chart,
            f"{lot.item_id} q{lot.quality} +{lot.upgrade}",
            daily,
            None,
        )
