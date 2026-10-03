"""Dependency injection container: wires DB, registries, client, scanner, web."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import aiohttp
import structlog
import uvicorn

from analytics.analytics import AnalyticsEngine
from analytics.seasonality import SeasonEngine
from api.client import StalcraftClient, load_items_db_cache
from api.discord import DiscordNotifier
from config.artifacts import ArtifactRegistry
from config.seasons import SeasonRegistry
from config.settings import Settings, reveal_secret
from database.engine import Database
from database.repositories.misc import DLQRepository
from deals.engine import DealsEngine
from metrics.metrics import MetricsCollector
from notify.feed import NotificationBus
from scanner import workers
from scanner.chart_worker import ChartService
from scanner.engine import AuctionScanner
from scanner.providers import HistoryPreseasonAvgProvider
from scanner.scheduler import WeightedScheduler, scheduler_loop
from utils.alerting import AlertManager
from utils.chart import ChartCache
from utils.circuit_breaker import CircuitBreaker, CircuitState
from utils.rate_limiter import TokenBucket
from utils.supervisor import HeartbeatRegistry, Supervisor
from web.app import create_app

log = structlog.get_logger(__name__)


class AppContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.database_url)
        self.metrics = MetricsCollector(enabled=settings.metrics_enabled)
        self.stop_event = asyncio.Event()
        self.heartbeat = HeartbeatRegistry()
        # P3-2: no alerting webhook -> no queue at all, so producers never
        # pile up "alert_queue_full" warnings with zero consumers.
        self.alerts = AlertManager(
            cooldown_sec=settings.alert_cooldown_sec,
            enabled=bool(reveal_secret(settings.alerting_webhook_url)),
        )
        self.supervisor = Supervisor(metrics=self.metrics, alert=self.alerts.notify)
        self.session: aiohttp.ClientSession | None = None
        self.client: StalcraftClient | None = None
        self.notifier: DiscordNotifier | None = None
        self.chart_service: ChartService | None = None
        self.chart_cache: ChartCache | None = None
        self.history_sync_paused = False
        self.bus: NotificationBus | None = None
        self.scanner: AuctionScanner | None = None
        self.scheduler: WeightedScheduler | None = None
        self.analytics: AnalyticsEngine | None = None
        self.deals_engine: DealsEngine | None = None
        self.seasons: SeasonEngine | None = None
        self.artifacts: ArtifactRegistry | None = None
        self.seasons_registry: SeasonRegistry | None = None
        self.rate_limiter: TokenBucket | None = None
        self.item_names: dict[str, str] = {}
        self.item_icons: dict[str, str] = {}  # P0-1: raw icon paths from the listing
        self.runtime_flags: dict[str, bool] = {}  # P1-10: admin feature flags
        self.web_task: asyncio.Task[None] | None = None
        self.worker_tasks: list[asyncio.Task[None]] = []
        # P1-11: declared in __init__, not created late in start()
        self.session_store: Any = None
        self.server: uvicorn.Server | None = None

    async def start(self, with_workers: bool = True, with_web: bool = True) -> None:
        s = self.settings
        await self.db.connect()
        # AUDIT R3: owned here (not only in web/app.py state) so background
        # workers (e.g. session GC) can reach the store.
        from utils.sessions import SessionStore

        self.session_store = SessionStore(self.db, ttl_hours=self.settings.session_ttl_hours)

        self.artifacts = ArtifactRegistry(s.resolve_path(s.artifacts_config_path))
        self.seasons_registry = SeasonRegistry(s.resolve_path(s.seasons_config_path))
        self.seasons = SeasonEngine(
            self.seasons_registry,
            prewarn_days=s.season_prewarn_days,
            seasonal_window_days=s.seasonal_history_window_days,
            dip_buy_pct=s.season_dip_buy_pct,
            fast_adapt_weight=s.fast_adapt_weight,
        )
        self.item_names = load_items_db_cache(s.resolve_path(s.items_db_path))
        # P0-1: fallback layer — names from artifacts.yaml always work, even
        # when the listing is unavailable or the cache is empty.
        self.item_names = {
            **self.item_names,
            **{i: a.name for i in self.artifacts.ids() if (a := self.artifacts.get(i))},
        }
        from api.client import load_items_icons

        self.item_icons = load_items_icons(s.resolve_path(s.items_db_path))

        timeout = aiohttp.ClientTimeout(total=s.api_timeout_sec)
        self.session = aiohttp.ClientSession(timeout=timeout)
        self.rate_limiter = TokenBucket(
            capacity=s.rate_limit_capacity,
            refill_rate=s.rate_limit_capacity / s.rate_limit_window_sec,
        )

        def _breaker_alert(state: CircuitState) -> None:
            if state == CircuitState.OPEN:
                with contextlib.suppress(RuntimeError):
                    asyncio.get_running_loop().create_task(
                        self.alerts.notify(
                            "breaker_open",
                            "Circuit breaker открыт: Stalcraft API недоступен",
                        )
                    )

        breaker = CircuitBreaker(
            "stalcraft_api",
            failure_threshold=5,
            recovery_timeout=30.0,
            on_state_change=_breaker_alert,
        )
        self.client = StalcraftClient(
            s, self.session, self.rate_limiter, breaker, on_alert=self.alerts.notify
        )

        self.bus = NotificationBus(self.db, ws_queue_size=s.ws_feed_queue_size)

        async def dlq_factory(payload: dict[str, Any]) -> None:
            async with self.db.session() as session:
                await DLQRepository(session).add("discord", payload)

        if s.discord_webhooks:
            self.notifier = DiscordNotifier(
                s, self.session, dlq_factory=dlq_factory, metrics=getattr(self, "metrics", None)
            )
            # AUDIT R4-outbox: wire durable-log hooks so every notification is
            # persisted at enqueue and closed after confirmed delivery.
            from database.repositories.misc import OutboxRepository

            async def _outbox_add(payload_json: str) -> int:
                async with self.db.session() as session:
                    return await OutboxRepository(session).add(payload_json)

            async def _outbox_done(row_id: int) -> None:
                async with self.db.session() as session:
                    await OutboxRepository(session).mark_sent(row_id)

            self.notifier._outbox_add = _outbox_add
            self.notifier._outbox_done = _outbox_done
            self.notifier.icons = self.item_icons  # P0-1: real icon structure
            self.chart_cache = ChartCache(
                s.resolve_path(s.chart_dir), max_files=s.chart_cache_max_files
            )
            self.notifier.chart_dir = s.resolve_path(s.chart_dir)
            self.chart_service = ChartService(
                s, self.db, self.notifier, self.chart_cache, metrics=self.metrics
            )

        if self.db.session_factory is not None:
            self.analytics = AnalyticsEngine(self.db.session_factory)
        self.deals_engine = DealsEngine(self.db, fee=s.fee)

        self.scheduler = WeightedScheduler(
            self.artifacts,
            base_interval=s.liquid_interval_sec,
            illiquid_mult=s.illiquid_interval_multiplier,
            illiquid_default_weight=s.illiquid_default_weight,
        )
        preseason = HistoryPreseasonAvgProvider(self.db, rows_per_item=s.history_rows_per_item)
        self.scanner = AuctionScanner(
            settings=s,
            db=self.db,
            client=self.client,
            registry=self.artifacts,
            seasons=self.seasons,
            bus=self.bus,
            notifier=self.notifier,
            metrics=self.metrics,
            preseason_provider=preseason,
            chart_service=self.chart_service,
            scheduler=self.scheduler,  # P0-3
        )

        if with_web:
            app = create_app(self)
            config = uvicorn.Config(
                app,
                host=s.web_host,
                port=s.web_port,
                log_level=s.log_level.lower(),
                access_log=False,
                server_header=False,
                timeout_keep_alive=10,
                limit_concurrency=64,
                ws_max_size=1 << 20,
                # P2: honor X-Forwarded-For from the configured proxies so
                # throttles key on the real client IP, not the proxy's.
                proxy_headers=True,
                forwarded_allow_ips=s.forwarded_allow_ips,
            )
            self.server = uvicorn.Server(config)
            self.web_task = asyncio.create_task(self.server.serve(), name="uvicorn")

        if with_workers:
            self._start_workers()

    def _start_workers(self) -> None:
        s = self.settings
        assert self.scheduler is not None and self.rate_limiter is not None
        assert self.scanner is not None

        async def budget_available() -> bool:
            # free estimate only — the token is spent inside the API client,
            # so a deferred item never burns budget twice
            assert self.rate_limiter is not None
            ok = self.rate_limiter.available >= 1.0
            if not ok:
                self.metrics.inc("scheduler_deferred")
            return ok

        hb = self.heartbeat

        def sup(
            name: str, factory: Any, gap: float, hb_name: str | None = None
        ) -> asyncio.Task[None]:
            coro = self.supervisor.supervise(
                name,
                factory,
                self.stop_event,
                heartbeat=hb,
                heartbeat_gap_sec=gap,
                heartbeat_name=hb_name,
            )
            return asyncio.create_task(coro, name=name)

        tasks = [
            sup(
                "scheduler",
                lambda: scheduler_loop(
                    self.scheduler,
                    budget_available,
                    self.scanner.scan_item,
                    batch_size=s.scan_batch_size,
                    stop_event=self.stop_event,
                    concurrency=s.scan_concurrency,
                    heartbeat=hb,
                    is_enabled=lambda: workers.flag_on(self, "scanner_enabled", True),
                ),
                max(s.liquid_interval_sec * 3, 90.0),
                hb_name="scan",
            ),
            sup(
                "config_reload",
                lambda: workers.config_reload_worker(self, self.stop_event),
                max(s.config_reload_interval_sec * 3, 90.0),
            ),
            sup(
                "history_sync",
                lambda: workers.history_sync_worker(self, self.stop_event),
                max(s.history_sync_interval_sec * 3, 90.0),
            ),
            sup(
                "meta_monitor",
                lambda: workers.meta_monitor_worker(self, self.stop_event),
                max(s.meta_check_interval_sec * 3, 90.0),
            ),
            sup(
                "season_monitor",
                lambda: workers.season_monitor_worker(self, self.stop_event),
                max(s.season_check_interval_sec * 3, 90.0),
            ),
            sup("retention", lambda: workers.retention_worker(self, self.stop_event), 10800.0),
            sup(
                "oom_monitor",
                lambda: workers.oom_monitor_worker(self, self.stop_event),
                max(s.oom_check_interval_sec * 3, 90.0),
            ),
            sup(
                "items_db",
                lambda: workers.items_db_worker(self, self.stop_event),
                max(s.items_db_refresh_hours * 3600 * 3, 90.0),
            ),
            sup("dlq_replay", lambda: workers.dlq_replay_worker(self, self.stop_event), 360.0),
            sup(
                "outbox_sweep",
                lambda: workers.outbox_sweep_worker(self, self.stop_event),
                max(workers.OUTBOX_SWEEP_INTERVAL_SEC * 3, 90.0),
                hb_name="outbox_sweep",
            ),
        ]
        if self.notifier is not None:
            tasks.append(
                sup(
                    "discord_sender",
                    lambda: self.notifier.run_sender(self.stop_event, heartbeat=hb),
                    90.0,
                )
            )
        if self.chart_service is not None:
            tasks.append(
                sup(
                    "chart_worker",
                    lambda: self.chart_service.run(self.stop_event, heartbeat=hb),
                    90.0,
                )
            )
        if reveal_secret(s.alerting_webhook_url):
            tasks.append(
                sup("alerting", lambda: workers.alerting_worker(self, self.stop_event), 90.0)
            )
        self.worker_tasks = tasks

    async def stop(self) -> None:
        # AUDIT R4: graceful stop — workers see stop_event and get a short
        # window to finish their iteration / drain (Discord queue drain),
        # then remaining tasks are cancelled. Cancelling immediately made
        # the drain paths in run_sender / ChartService unreachable.
        self.stop_event.set()
        if self.server is not None:
            self.server.should_exit = True  # P1-11: graceful web shutdown
        pending = [t for t in [*self.worker_tasks, self.web_task] if t is not None]
        if not pending:
            # ресурсы закрываем даже без задач: иначе aiohttp-сессия
            # остаётся незакрытой ("Unclosed client session" в тестах)
            if self.session is not None:
                await self.session.close()
                self.session = None
            await self.db.close()
            log.info("container_stopped")
            return
        done, still = await asyncio.wait(pending, timeout=10.0)
        for task in still:
            task.cancel()
        for task in still:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                log.warning("worker_error_on_shutdown", error=str(exc))
        if self.session is not None:
            await self.session.close()
        await self.db.close()
        log.info("container_stopped")
