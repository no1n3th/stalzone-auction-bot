"""Auction scanner: per-item scan pipeline from API lots to notifications."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from analytics.seasonality import SeasonEngine
from api.client import ItemRequestError, StalcraftClient, TokenInvalidError
from api.discord import EVENT_PRIORITY, DiscordNotifier
from config.artifacts import ArtifactRegistry
from config.settings import Settings
from database.engine import Database
from database.repositories.history import HistoryRepository
from database.repositories.misc import (
    MarketRepository,
    MetaStateRepository,
    SentLotsRepository,
)
from metrics.metrics import MetricsCollector
from models.events import EventType, NotificationEvent
from models.lot import Lot
from notify.feed import NotificationBus
from scanner.filters import ScanContext, default_chain
from utils.chart import ChartCache
from utils.helpers import parse_api_time, safe_str

_QUARANTINE_DELAY_SEC = 3600.0  # AUDIT D5: 4xx-quarantined items retry hourly

if TYPE_CHECKING:
    from scanner.chart_worker import ChartService
    from scanner.scheduler import WeightedScheduler

log = structlog.get_logger(__name__)


class AuctionScanner:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        client: StalcraftClient,
        registry: ArtifactRegistry,
        seasons: SeasonEngine,
        bus: NotificationBus,
        notifier: DiscordNotifier | None,
        metrics: MetricsCollector,
        preseason_provider: Any = None,
        chart_service: ChartService | None = None,
        scheduler: WeightedScheduler | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        self.registry = registry
        self.seasons = seasons
        self.bus = bus
        self.notifier = notifier
        self.metrics = metrics
        self.preseason_provider = preseason_provider
        self.chart_service = chart_service
        self.scheduler = scheduler  # P0-3: explicit injection (was getattr-hack dead code)
        self.filters = default_chain()
        self.market_lots: dict[str, list[Lot]] = {}  # shared with meta worker

    async def scan_item(self, item_id: str) -> None:
        art = self.registry.get(item_id)
        if art is None:
            return
        try:
            raw_lots = await self.client.get_lots(item_id, limit=self.settings.scan_limit)
        except ItemRequestError as exc:
            # AUDIT D5: permanent 4xx — quarantine the item so one bad item_id
            # doesn't burn retries and breaker budget on every scan cycle.
            self.metrics.inc("lots_fetch_failed")
            log.warning("lots_fetch_quarantined", item_id=item_id, error=str(exc))
            # AUDIT D5: quarantine — retry the item hourly instead of every cycle.
            if self.scheduler is not None:
                self.scheduler.defer(item_id, _QUARANTINE_DELAY_SEC)
            return
        except TokenInvalidError:
            # auth backoff inside the client; one debug line, no warning spam
            self.metrics.inc("lots_fetch_failed")
            log.debug("lots_fetch_auth_backoff", item_id=item_id)
            return
        except Exception as exc:
            self.metrics.inc("lots_fetch_failed")
            log.warning("lots_fetch_failed", item_id=item_id, error=str(exc))
            return
        # INFO: по этой строке видно, что сканер реально ходит в API
        log.info("lots_fetched", item_id=item_id, count=len(raw_lots))
        self.metrics.inc("lots_fetched", len(raw_lots))

        lots: list[Lot] = []
        for raw in raw_lots:
            try:
                lots.append(Lot.from_api(item_id, raw))
            except (ValueError, TypeError, KeyError) as exc:
                log.debug("lot_parse_skipped", item_id=item_id, error=str(exc))
        self.market_lots[item_id] = lots

        # group market minimums per (quality, upgrade); P1-4: expired lots are
        # not a real "wall" — a stale cheap lot used to suppress good signals.
        market_min: dict[tuple[int, int], float] = {}
        for lot in lots:
            key = (lot.quality, lot.upgrade)
            if lot.price <= 0 or lot.is_expired:
                continue
            if key not in market_min or lot.price < market_min[key]:
                market_min[key] = lot.price

        window_days = self.seasons.history_window_days(item_id)
        adapt_weight = self.seasons.adapt_weight(item_id)
        seasonal = self.seasons.is_seasonal(item_id)

        async with self.db.session() as session:
            history = HistoryRepository(
                session,
                rows_per_item=self.settings.history_rows_per_item,
                cheapest_only=self.settings.history_cheapest_only,
            )
            await MarketRepository(session).replace_for_item(
                item_id,
                [
                    {
                        "quality": q,
                        "upgrade": u,
                        "min_price": p,
                        "lots_count": sum(
                            1 for lot_ in lots if (lot_.quality, lot_.upgrade) == (q, u)
                        ),
                    }
                    for (q, u), p in market_min.items()
                ],
            )
            avg_cache = await history.get_price_cache(
                item_id, window_days=window_days, fast_adapt_weight=adapt_weight
            )
            # one GROUP BY for all (quality, upgrade) sample sizes instead of N queries
            samples = await history.sample_sizes(item_id)
            meta_repo = MetaStateRepository(session)
            invalid_keys = await meta_repo.invalidated_keys()
            sent_repo = SentLotsRepository(session, cooldown_min=self.settings.notify_cooldown_min)
            # one batched IN-query instead of a per-lot is_sent round-trip
            sent_sigs = await sent_repo.filter_sent({lot.signature for lot in lots})

        invalidated: set[tuple[int, int]] = set()
        for state_key in invalid_keys:
            try:
                _, iid, q, u = state_key.split(":")
                if iid == item_id:
                    invalidated.add((int(q), int(u)))
            except ValueError:
                continue
        if seasonal:
            invalidated.clear()  # seasonal mode uses its own rules

        ctx = ScanContext(
            artifact=art,
            fee=self.settings.fee,
            fee_pct=self.settings.fee_pct,
            earnings_by_quality=self.settings.liquid_earnings_by_quality,
            avg_cache=avg_cache,
            samples=samples,
            market_min=market_min,
            invalidated=invalidated,
            min_sample_size=self.settings.min_sample_size,
            threshold_mode=self.settings.threshold_mode,
            mid_low_mult=self.settings.mid_low_multiplier,
            mid_high_mult=self.settings.mid_high_multiplier,
            mid_top_mult=self.settings.mid_top_multiplier,
            sent_checker=lambda sig: sig in sent_sigs,
        )

        for lot in lots:
            try:  # P1-3: one bad lot must not abort the rest of the scan
                if all(f.check(lot, ctx) for f in self.filters):
                    await self._on_profitable(lot, ctx, is_mid=lot.upgrade not in art.upgrades)
            except Exception as exc:
                self.metrics.inc("lot_notify_failed")
                log.warning("lot_processing_failed", item_id=item_id, error=safe_str(exc))
        try:
            await self._check_dip_buy(item_id, lots)
        except Exception as exc:
            log.warning("dip_buy_check_failed", item_id=item_id, error=safe_str(exc))

    async def _on_profitable(self, lot: Lot, ctx: ScanContext, is_mid: bool) -> None:
        # P1-2: mid +10..+14 are priced against +15 (as SampleSizeFilter does),
        # not against +0 — the message must show the base the rule used.
        base_upgrade = lot.upgrade if not is_mid else (15 if lot.upgrade >= 10 else 0)
        avg = ctx.avg(lot.quality, base_upgrade)
        market_min = ctx.market_min.get((lot.quality, lot.upgrade))
        others = sorted(
            lot_.price
            for lot_ in self.market_lots.get(lot.item_id, [])
            if (lot_.quality, lot_.upgrade) == (lot.quality, lot.upgrade)
            and lot_.price > 0
            and not lot_.is_expired
        )
        next_price = others[1] if len(others) > 1 else None

        event_type = EventType.MID_LOT if is_mid else EventType.LOT
        event = NotificationEvent(
            event_type=event_type,
            item_id=lot.item_id,
            title=f"{ctx.artifact.name} +{lot.upgrade} по {lot.price:.0f}",
            fields={
                "quality": lot.quality,
                "upgrade": lot.upgrade,
                "price": lot.price,
                "avg": avg,
                "market_min": market_min,
            },
            lot_signature=lot.signature,
        )
        self.metrics.inc("mid_level_hits" if is_mid else "lots_profitable")
        await self.bus.publish(event)

        if self.notifier is not None:
            # fast path: embed goes out WITHOUT the chart (p95 target < 1.5s);
            # the chart worker PATCHes the image into the message later
            embed = self.notifier.build_embed(
                lot,
                item_name=ctx.artifact.name,
                avg_price=avg,
                next_price=next_price,
                market_min=market_min,
                title=f"{'Промежуточная заточка' if is_mid else 'Выгодный лот'}: "
                f"{ctx.artifact.name} +{lot.upgrade}",
            )
            chart_key: str | None = None
            if self.settings.chart_enabled and self.chart_service is not None:
                chart_key = ChartCache.make_key(lot.item_id, lot.quality, lot.upgrade)
            await self.notifier.enqueue(
                lot.signature,
                embed,
                event.detected_at,
                priority=EVENT_PRIORITY[event_type],
                chart_key=chart_key,
            )
            if chart_key is not None and self.chart_service is not None:
                self.chart_service.enqueue(lot, chart_key)
        # P1-3: the lot counts as "sent" only after the event actually left the
        # process — a failure above must not consume the dedup signature.
        try:
            async with self.db.session() as session:
                await SentLotsRepository(session).mark_sent(lot.signature, lot.item_id, lot.price)
        except Exception as exc:
            self.metrics.inc("lot_notify_failed")
            log.warning("sent_mark_failed", lot_sig=lot.signature, error=safe_str(exc))

    # P1-1: dip-buy alert state - fire once on crossing, refire only on a
    # further >=5 p.p. dip or after a 6-hour cooldown; reset when the price
    # climbs back above the dip line.
    _DIP_REARM_PCT = 5.0
    _DIP_COOLDOWN_SEC = 6 * 3600

    async def _dip_state(self, key: str) -> str | None:
        from database.repositories.misc import MetaStateRepository

        async with self.db.session() as session:
            return await MetaStateRepository(session).get(key)

    async def _dip_set(self, key: str, value: str) -> None:
        from database.repositories.misc import MetaStateRepository

        async with self.db.session() as session:
            await MetaStateRepository(session).set(key, value)

    async def _dip_delete(self, key: str) -> None:
        from database.repositories.misc import MetaStateRepository

        async with self.db.session() as session:
            await MetaStateRepository(session).delete(key)

    async def _check_dip_buy(self, item_id: str, lots: list[Lot]) -> None:
        if self.preseason_provider is None or not self.seasons.is_seasonal(item_id):
            return
        cheapest: dict[tuple[int, int], float] = {}
        for lot in lots:
            key = (lot.quality, lot.upgrade)
            if lot.price > 0 and (key not in cheapest or lot.price < cheapest[key]):
                cheapest[key] = lot.price
        for (quality, upgrade), price in cheapest.items():
            event = await self.seasons.check_dip_buy(
                self.preseason_provider, item_id, quality, upgrade, price
            )
            state_key = f"alert:dip:{item_id}:{quality}:{upgrade}"
            if event is None:
                await self._dip_delete(state_key)  # back above the line -> rearm
                continue
            dip_pct = (1.0 - price / float(event.fields["preseason_avg"])) * 100.0
            raw = await self._dip_state(state_key)
            fire = raw is None
            if raw is not None:
                pct_s, _, ts_s = raw.partition("|")
                try:
                    last_pct = float(pct_s)
                except ValueError:
                    last_pct = 0.0
                fired_at = parse_api_time(ts_s) if ts_s else None
                cooldown_done = (
                    fired_at is not None
                    and (datetime.now(UTC) - fired_at).total_seconds() >= self._DIP_COOLDOWN_SEC
                )
                fire = dip_pct >= last_pct + self._DIP_REARM_PCT or cooldown_done
            if not fire:
                continue
            await self._dip_set(
                state_key, f"{round(dip_pct, 1)}|{datetime.now(UTC).isoformat()}"
            )
            self.metrics.inc("seasonal_events")
            await self.bus.publish(event)
            if self.notifier is not None:
                await self.notifier.enqueue(
                    event.lot_signature or f"dip:{item_id}:{quality}:{upgrade}",
                    self.notifier.build_event_embed(event),
                    event.detected_at,
                    priority=EVENT_PRIORITY[event.event_type],
                )
