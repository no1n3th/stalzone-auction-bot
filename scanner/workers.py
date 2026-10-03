"""Background workers: config reload, history sync, meta/season monitors, retention, OOM."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import os
from collections.abc import Set as ABSet
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from analytics import meta as meta_engine
from analytics.meta import MetaThresholds
from api.client import (
    ItemRequestError,
    TokenInvalidError,
    cgroup_memory_limit_bytes,
    cgroup_memory_usage_bytes,
)
from config.settings import reveal_secret
from database.repositories.history import HistoryRepository
from database.repositories.misc import MIN_OUTBOX_AGE_SEC
from database.repositories.misc import (
    DLQRepository,
    MetaStateRepository,
    NotificationsRepository,
    OutboxRepository,
    SentLotsRepository,
)
from models.events import NotificationEvent
from scanner.providers import HistoryMetaProvider
from utils.helpers import parse_api_time

log = structlog.get_logger(__name__)

OOM_CRITICAL_MB = 20.0


def _beat(container: Any, name: str) -> None:
    hb = getattr(container, "heartbeat", None)
    if hb is not None:
        hb.beat(name)


def flag_on(container: Any, name: str, default: bool = True) -> bool:
    """P1-10: feature flags set in the admin UI reach the runtime through
    `container.runtime_flags` (refreshed by config_reload_worker). A flag
    absent from the table falls back to `default` — the UI cannot brick the
    bot by deleting rows."""
    return bool(getattr(container, "runtime_flags", {}).get(name, default))


async def _reload_runtime_flags(container: Any) -> None:
    """Load admin feature flags into the container and apply side effects.

    P1-10: the flags UI used to be write-only. Side effects keep the loop
    bodies cheap — workers read memory, this function touches the DB once
    per config_reload tick (30 s)."""
    from database.repositories.misc import FeatureFlagsRepository

    db = getattr(container, "db", None)
    if db is None:  # тестовые стаб-контейнеры без БД — флаги не применяем
        return
    async with db.session() as session:
        flags = await FeatureFlagsRepository(session).all()
    prev = getattr(container, "runtime_flags", {})
    container.runtime_flags = dict(flags)
    if prev != container.runtime_flags:
        log.info("feature_flags_applied", flags=container.runtime_flags)
    chart_service = getattr(container, "chart_service", None)
    if chart_service is not None:
        chart_service.enabled = bool(
            container.runtime_flags.get("charts_enabled", container.settings.chart_enabled)
        )
    notifier = getattr(container, "notifier", None)
    if notifier is not None:
        notifier.paused = not bool(container.runtime_flags.get("discord_enabled", True))


def classify_oom_zone(headroom_mb: float, watchdog_mb: float) -> str:
    """green -> yellow (<watchdog) -> red (<watchdog/2) -> critical (<20MB)."""
    if headroom_mb < OOM_CRITICAL_MB:
        return "critical"
    if headroom_mb < watchdog_mb / 2:
        return "red"
    if headroom_mb < watchdog_mb:
        return "yellow"
    return "green"


async def interruptible_sleep(seconds: float, stop: asyncio.Event) -> None:
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def config_reload_worker(container: Any, stop: asyncio.Event) -> None:
    """Hot reload of artifacts.yaml / seasons.yaml on mtime change."""
    while not stop.is_set():
        _beat(container, "config_reload")
        try:
            if container.artifacts.maybe_reload():
                log.info("artifacts_config_reloaded", count=len(container.artifacts))
                container.scheduler.rebuild()
            if container.seasons_registry.maybe_reload():
                log.info("seasons_config_reloaded")
            await _reload_runtime_flags(container)  # P1-10: admin flags -> runtime
        except Exception as exc:
            log.error("config_reload_failed", error=str(exc))
        await interruptible_sleep(
            getattr(getattr(container, "settings", None), "config_reload_interval_sec", 30.0), stop
        )


def _sold_at_ts(value: object) -> float | None:
    """P0-4: ISO-8601 ('2026-09-29T12:45:15Z'), unix s/ms — None on garbage."""
    dt = parse_api_time(value)
    return dt.timestamp() if dt is not None else None


def _parse_sold_at(value: object) -> datetime | None:
    return parse_api_time(value)


async def history_sync_worker(container: Any, stop: asyncio.Event) -> None:
    """Periodically pull sales history for tracked items into the ring buffer."""
    settings = container.settings
    while not stop.is_set():
        _beat(container, "history_sync")
        if not flag_on(container, "history_sync_enabled"):
            await interruptible_sleep(60.0, stop)
            continue
        if getattr(container, "history_sync_paused", False):
            # red OOM zone: history sync is paused to relieve memory pressure
            await interruptible_sleep(60.0, stop)
            continue
        for item_id in container.artifacts.ids():
            if stop.is_set():
                break
            try:
                # AUDIT D1-watermark: drop sales already synced for this item
                # (the dedup index still guards the insert; this saves parsing
                # and insert volume on every 15-minute cycle).
                async with container.db.session() as session:
                    wm_raw = await MetaStateRepository(session).get(f"history:wm:{item_id}")
                watermark = float(wm_raw) if wm_raw else 0.0
                # P0-4.5: paginate — rare (quality, upgrade) groups never reached
                # MIN_SAMPLE_SIZE when only the last 50 rows were synced.
                raw: list[dict[str, Any]] = []
                offset = 0
                page_limit = 200
                while True:
                    page = await container.client.get_history(
                        item_id, limit=page_limit, offset=offset
                    )
                    if not page:
                        break
                    raw.extend(page)
                    oldest = min(
                        (t for t in (_sold_at_ts(e.get("time")) for e in page) if t),
                        default=None,
                    )
                    if len(page) < page_limit or (
                        watermark and oldest is not None and oldest <= watermark
                    ):
                        break
                    offset += page_limit
                # P0-4: '>=' — same-second sales are guarded by the unique
                # dedup index, the watermark filter must not silently drop them.
                if watermark:
                    raw = [
                        e
                        for e in raw
                        if (ts := _sold_at_ts(e.get("time"))) is not None and ts >= watermark
                    ]
                rows = []
                for entry in raw:
                    extra = entry.get("additional") or {}
                    rows.append(
                        {
                            "item_id": item_id,
                            "quality": int(extra.get("qlt", entry.get("quality", 0)) or 0),
                            "upgrade": int(extra.get("ptn", entry.get("upgrade", 0)) or 0),
                            "price": float(entry.get("price", 0) or 0),
                            "amount": int(entry.get("amount", 1) or 1),
                            "source": "history",
                            # AUDIT D1: real sale time — was ignored, so every
                            # time window was computed from download time.
                            "sold_at": _parse_sold_at(entry.get("time")),
                        }
                    )
                if rows:
                    async with container.db.session() as session:
                        repo = HistoryRepository(
                            session,
                            rows_per_item=settings.history_rows_per_item,
                            cheapest_only=settings.history_cheapest_only,
                        )
                        inserted = await repo.bulk_insert(rows)
                    if inserted:
                        log.info("history_rows", item_id=item_id, inserted=inserted)
                    container.metrics.inc("history_rows", inserted)
                    newest = max(
                        (t for t in (_sold_at_ts(e.get("time")) for e in raw) if t), default=None
                    )
                    if newest is not None and newest > watermark:
                        async with container.db.session() as session:
                            await MetaStateRepository(session).set(
                                f"history:wm:{item_id}", f"{newest}"
                            )
            except ItemRequestError as exc:
                # AUDIT D5: permanent 4xx — one warning per item, not per cycle
                log.warning("history_sync_quarantined", item_id=item_id, error=str(exc))
                continue
            except TokenInvalidError:
                # auth backoff is active inside the client — one error per cycle, not per item
                log.warning("history_sync_auth_failed", hint="проверьте CLIENT_ID/CLIENT_SECRET")
                break
            except Exception as exc:
                log.warning("history_sync_failed", item_id=item_id, error=str(exc))
            await interruptible_sleep(0.3, stop)
        await interruptible_sleep(settings.history_sync_interval_sec, stop)


class _ConfigSeasonStateStore:
    """SeasonStateStore over the meta_state table (shared k/v storage)."""

    def __init__(self, db: Any) -> None:
        self.db = db

    async def get(self, key: str) -> str | None:
        from database.repositories.misc import MetaStateRepository

        async with self.db.session() as session:
            return await MetaStateRepository(session).get(key)

    async def set(self, key: str, value: str) -> None:
        from database.repositories.misc import MetaStateRepository

        async with self.db.session() as session:
            await MetaStateRepository(session).set(key, value)

    async def delete(self, key: str) -> None:
        from database.repositories.misc import MetaStateRepository

        async with self.db.session() as session:
            await MetaStateRepository(session).delete(key)

    async def keys_with_prefix(self, prefix: str) -> ABSet[str]:
        from database.repositories.misc import MetaStateRepository

        async with self.db.session() as session:
            return set(await MetaStateRepository(session).invalidated_keys(prefix))


async def _publish_events(container: Any, events: list[NotificationEvent]) -> None:
    from api.discord import EVENT_PRIORITY

    for event in events:
        # human-readable titles everywhere: feed, websocket, discord
        if event.item_id:
            name = container.item_names.get(event.item_id)
            if name:
                event.title = event.title.replace(event.item_id, name)
        await container.bus.publish(event)
        if container.notifier is not None:
            embed = container.notifier.build_event_embed(
                event,
                item_name=container.item_names.get(event.item_id, event.item_id),
                item_names=container.item_names,
            )
            await container.notifier.enqueue(
                event.lot_signature or f"{event.event_type.value}:{event.item_id}:{event.title}",
                embed,
                event.detected_at,
                priority=EVENT_PRIORITY.get(event.event_type, 4),
            )


async def meta_monitor_worker(container: Any, stop: asyncio.Event) -> None:
    settings = container.settings
    thresholds = MetaThresholds(  # getattr-дефолты: тесты передают частичный стаб-settings
        dump_drop_pct=getattr(settings, "dump_drop_pct", 20.0),
        dump_window_hours=getattr(settings, "dump_window_hours", 24),
        meta_exit_min_lots=getattr(settings, "meta_exit_min_lots", 10),
        meta_exit_below_avg_pct=getattr(settings, "meta_exit_below_avg_pct", 25.0),
        stabilization_hours=getattr(settings, "stabilization_hours", 48),
        stabilization_max_spread_pct=getattr(settings, "stabilization_max_spread_pct", 15.0),
        meta_enter_window_hours=getattr(settings, "meta_enter_window_hours", 6),
        meta_enter_surge_pct=getattr(settings, "meta_enter_surge_pct", 50.0),
        fast_adapt_weight=getattr(settings, "fast_adapt_weight", 3.0),
    )
    provider = HistoryMetaProvider(
        container.db,
        getattr(getattr(container, "scanner", None), "market_lots", {}),
        rows_per_item=getattr(settings, "history_rows_per_item", 50),
    )
    store = _ConfigSeasonStateStore(container.db)
    while not stop.is_set():
        _beat(container, "meta_monitor")
        if not flag_on(container, "meta_monitor_enabled"):
            await interruptible_sleep(60.0, stop)
            continue
        for art in container.artifacts.all():
            if stop.is_set():
                break
            seasonal = container.seasons.is_seasonal(art.item_id)
            for upgrade in art.upgrades:
                for quality in range(art.effective_min_quality, 6):
                    try:
                        if seasonal:
                            # dump on seasonal item -> separate 'seasonal drop' type
                            dump = await meta_engine.check_dump(
                                provider, store, thresholds, art.item_id, quality, upgrade
                            )
                            if dump is not None:
                                event = container.seasons.seasonal_drop_event(
                                    art.item_id,
                                    quality,
                                    upgrade,
                                    dump.fields.get("drop_pct", 0.0),
                                )
                                if event is not None:
                                    container.metrics.inc("seasonal_events")
                                    await _publish_events(container, [event])
                            await meta_engine.check_stabilization(
                                provider, store, thresholds, art.item_id, quality, upgrade
                            )
                            enter = await meta_engine.check_meta_enter(
                                provider, store, thresholds, art.item_id, quality, upgrade
                            )
                            if enter is not None:
                                container.metrics.inc("meta_events")
                                await _publish_events(container, [enter])
                        else:
                            events = await meta_engine.evaluate_group(
                                provider,
                                store,
                                thresholds,
                                art.item_id,
                                quality,
                                upgrade,
                                suppress_meta=False,
                            )
                            if events:
                                container.metrics.inc("meta_events", len(events))
                                await _publish_events(container, events)
                    except Exception as exc:
                        log.warning(
                            "meta_check_failed",
                            item_id=art.item_id,
                            quality=quality,
                            upgrade=upgrade,
                            error=str(exc),
                        )
        await interruptible_sleep(settings.meta_check_interval_sec, stop)


async def season_monitor_worker(container: Any, stop: asyncio.Event) -> None:
    store = _ConfigSeasonStateStore(container.db)
    while not stop.is_set():
        _beat(container, "season_monitor")
        if not flag_on(container, "season_monitor_enabled"):
            await interruptible_sleep(60.0, stop)
            continue
        try:
            events = await container.seasons.check_transitions(store)
            if events:
                container.metrics.inc("seasonal_events", len(events))
                await _publish_events(container, events)
        except Exception as exc:
            log.warning("season_monitor_failed", error=str(exc))
        await interruptible_sleep(container.settings.season_check_interval_sec, stop)


async def retention_worker(container: Any, stop: asyncio.Event) -> None:
    """Daily rollup + prune raw history, notifications, sent_lots, DLQ, sessions."""
    settings = container.settings
    while not stop.is_set():
        _beat(container, "retention")
        try:
            async with container.db.session() as session:
                repo = HistoryRepository(
                    session,
                    rows_per_item=settings.history_rows_per_item,
                    cheapest_only=settings.history_cheapest_only,
                )
                rolled = await repo.rollup_daily()
                pruned = await repo.prune_older_than(settings.history_retention_days)
                notif_pruned = await NotificationsRepository(session).prune_older_than(14)
                sent_pruned = await SentLotsRepository(session).cleanup(older_than_hours=48)
                dlq_pruned = await DLQRepository(session).prune_done(older_than_days=7)
                # P0-2.7: the outbox table must not grow forever
                outbox_pruned = await OutboxRepository(session).prune(sent_older_than_days=3)
            session_store = getattr(container, "session_store", None)
            sessions_pruned = await session_store.gc() if session_store is not None else 0
            log.info(
                "retention_done",
                rolled=rolled,
                pruned=pruned,
                notifications=notif_pruned,
                sent_lots=sent_pruned,
                dlq=dlq_pruned,
                outbox=outbox_pruned,
                sessions=sessions_pruned,
            )
        except Exception as exc:
            log.warning("retention_failed", error=str(exc))
        await interruptible_sleep(3600.0, stop)


async def oom_monitor_worker(container: Any, stop: asyncio.Event) -> None:
    """Watchdog with degradation zones by memory headroom.

    yellow  (< OOM_WATCHDOG_MB): charts off, illiquid intervals x2, gc
    red     (< watchdog/2):      caches reset, history_sync paused
    critical(< 20MB):            alert, flush state, os._exit(1) — docker revives
    """
    settings = container.settings
    zone = "green"
    while not stop.is_set():
        _beat(container, "oom_monitor")
        try:
            limit = cgroup_memory_limit_bytes()
            usage = cgroup_memory_usage_bytes()
            if limit and usage:
                headroom_mb = (limit - usage) / (1024 * 1024)
                container.metrics.set_gauge("memory_headroom_mb", headroom_mb)
                new_zone = classify_oom_zone(headroom_mb, settings.oom_watchdog_mb)
                if new_zone != zone:
                    zone = new_zone
                    await _apply_oom_zone(container, zone, headroom_mb, limit)
        except Exception as exc:
            log.debug("oom_check_failed", error=str(exc))
        await interruptible_sleep(settings.oom_check_interval_sec, stop)


async def _apply_oom_zone(container: Any, zone: str, headroom_mb: float, limit: int) -> None:
    ctx = dict(zone=zone, headroom_mb=round(headroom_mb, 1), limit_mb=round(limit / (1024**2), 1))
    scheduler = getattr(container, "scheduler", None)
    chart_service = getattr(container, "chart_service", None)
    alerts = getattr(container, "alerts", None)
    if zone == "green":
        if scheduler is not None:
            scheduler.load_factor = 1.0
        if chart_service is not None:
            chart_service.enabled = getattr(container.settings, "chart_enabled", True)
        container.history_sync_paused = False
        log.info("oom_zone_green", **ctx)
        return
    log.error("oom_zone_" + zone, **ctx)
    gc.collect()
    if zone in ("yellow", "red", "critical"):
        if chart_service is not None:
            chart_service.enabled = False
        if scheduler is not None:
            scheduler.load_factor = 2.0
            with contextlib.suppress(Exception):
                scheduler.rebuild()
    if zone in ("red", "critical"):
        cache = getattr(container, "chart_cache", None)
        if cache is not None:
            with contextlib.suppress(Exception):
                cache.clear()
        scanner = getattr(container, "scanner", None)
        if scanner is not None:
            scanner.market_lots.clear()
        container.history_sync_paused = True
    if zone == "critical":
        if alerts is not None:
            with contextlib.suppress(Exception):
                await alerts.notify(
                    "oom_critical",
                    f"Критическая память: headroom {headroom_mb:.0f}MB — процесс перезапускается",
                )
        db = getattr(container, "db", None)
        if db is not None:
            # flush committed state before the hard exit; docker restarts us
            with contextlib.suppress(Exception):
                await asyncio.wait_for(db.close(), timeout=2.0)
        exit_fn = getattr(container, "oom_exit", os._exit)
        exit_fn(1)


async def alerting_worker(container: Any, stop: asyncio.Event) -> None:
    """Drains the AlertManager queue to the ops webhook with per-kind cooldown."""
    alerts = getattr(container, "alerts", None)
    url = reveal_secret(getattr(container.settings, "alerting_webhook_url", ""))
    session = getattr(container, "session", None)
    if alerts is None or not url or session is None or alerts.queue is None:
        while not stop.is_set():
            await interruptible_sleep(60.0, stop)
        return
    while not stop.is_set():
        _beat(container, "alerting")
        try:
            kind, text = await asyncio.wait_for(alerts.queue.get(), timeout=1.0)
        except TimeoutError:
            continue
        if not alerts.allowed(kind):
            continue
        try:
            async with session.post(url, json={"content": f"🚨 **{kind}**: {text}"}) as resp:
                if resp.status < 400:
                    alerts.mark_sent(kind)
                    log.info("alert_sent", kind=kind)
                else:
                    log.warning("alert_webhook_rejected", kind=kind, status=resp.status)
        except Exception as exc:
            log.warning("alert_send_failed", kind=kind, error=str(exc))


async def items_db_worker(container: Any, stop: asyncio.Event) -> None:
    """Daily refresh of the item names cache from stalcraft-database."""
    settings = container.settings
    path = settings.resolve_path(settings.items_db_path)
    while not stop.is_set():
        _beat(container, "items_db")
        try:
            count = await container.client.sync_items_db(path)
            from api.client import load_items_db_cache, load_items_icons

            container.item_names = load_items_db_cache(path)
            container.item_icons = load_items_icons(path)
            if container.notifier is not None:
                container.notifier.icons = container.item_icons
            log.info("items_db_refreshed", count=count)
        except Exception as exc:
            log.warning("items_db_refresh_failed", error=str(exc))
        await interruptible_sleep(settings.items_db_refresh_hours * 3600, stop)


MAX_DLQ_ATTEMPTS = 10  # AUDIT R6: give up after this many failed deliveries
DLQ_MAX_BACKOFF_SEC = 3600.0  # AUDIT R6: cap the linear backoff at 1 hour


async def dlq_replay_worker(container: Any, stop: asyncio.Event) -> None:
    while not stop.is_set():
        _beat(container, "dlq_replay")
        try:
            if container.notifier is not None:
                async with container.db.session() as session:
                    repo = DLQRepository(session)
                    due = await repo.due(limit=10)
                for row in due:
                    # AUDIT R6: per-row isolation — a poisoned payload used to
                    # escape the loop and block every later row forever.
                    try:
                        payload = json.loads(row.payload)
                    except (TypeError, ValueError) as exc:
                        log.error("dlq_poisoned_payload_dropped", row_id=row.id, error=str(exc))
                        async with container.db.session() as session:
                            await DLQRepository(session).mark_done(row.id)
                        continue
                    try:
                        sent, _failed = await container.notifier.replay_dlq([payload])
                    except Exception as exc:
                        log.warning("dlq_replay_row_failed", row_id=row.id, error=str(exc))
                        sent = False
                    async with container.db.session() as session:
                        repo = DLQRepository(session)
                        if sent:
                            await repo.mark_done(row.id)
                        elif row.attempts + 1 >= MAX_DLQ_ATTEMPTS:
                            # AUDIT R6: bounded retries — give up and mark dead
                            # instead of growing the queue forever.
                            log.error("dlq_dead_letter", row_id=row.id, attempts=row.attempts + 1)
                            await repo.mark_done(row.id)
                        else:
                            await repo.mark_retry(
                                row.id,
                                backoff_sec=min(60.0 * (row.attempts + 1), DLQ_MAX_BACKOFF_SEC),
                            )
        except Exception as exc:
            log.warning("dlq_replay_failed", error=str(exc))
        await interruptible_sleep(120.0, stop)


OUTBOX_SWEEP_INTERVAL_SEC = 60.0


async def outbox_sweep_worker(container: Any, stop: asyncio.Event) -> None:
    """P0-2: re-enqueue persisted-but-unsent notifications (restart/OOM safety).

    Fixes vs the AUDIT R4 version: awaited enqueue (was a discarded coroutine),
    persist=False replay (no duplicate rows), stable priority from the durable
    payload (was read from a key embeds do not have), heartbeat beats (gap=0
    made /health/ready answer 503 forever) and an interruptible sleep."""
    _beat(container, "outbox_sweep")
    while not stop.is_set():
        try:
            async with container.db.session() as session:
                rows = await OutboxRepository(session).pending(
                    limit=50, min_age_sec=MIN_OUTBOX_AGE_SEC
                )
            for row in rows:
                try:
                    payload = json.loads(row.payload)
                except (TypeError, ValueError) as exc:
                    log.error("outbox_poisoned_dropped", row_id=row.id, error=str(exc))
                    async with container.db.session() as session:
                        await OutboxRepository(session).mark_sent(row.id)
                    continue
                notifier = container.notifier
                if notifier is not None:
                    await notifier.enqueue(
                        payload["lot_sig"],
                        payload["embed"],
                        datetime.fromisoformat(payload["detected_at"]),
                        None,  # charts are re-rendered by the chart worker
                        priority=payload.get("priority", 50),
                        persist=False,  # P0-2.2: the outbox row already exists
                        outbox_id=row.id,
                    )
                async with container.db.session() as session:
                    await OutboxRepository(session).mark_attempt(
                        row.id, datetime.now(UTC) + timedelta(minutes=5)
                    )
        except Exception as exc:
            log.warning("outbox_sweep_failed", error=str(exc))
        _beat(container, "outbox_sweep")
        await interruptible_sleep(OUTBOX_SWEEP_INTERVAL_SEC, stop)
