"""Seasonality engine: seasonal windows, fast adapt, dip-buy, transitions."""

from __future__ import annotations

from collections.abc import Set as ABSet
from datetime import UTC, date, datetime
from typing import Protocol

from config.seasons import SeasonConfig, SeasonRegistry
from models.events import EventType, NotificationEvent


class SeasonStateStore(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def keys_with_prefix(self, prefix: str) -> ABSet[str]: ...


class PreseasonAvgProvider(Protocol):
    async def avg_before(
        self, item_id: str, quality: int, upgrade: int, hours_back: int, season_hours: int
    ) -> float | None: ...


class SeasonEngine:
    def __init__(
        self,
        registry: SeasonRegistry,
        prewarn_days: int = 7,
        seasonal_window_days: int = 14,
        dip_buy_pct: float = 30.0,
        fast_adapt_weight: float = 3.0,
        default_window_days: int = 30,
    ) -> None:
        self.registry = registry
        self.prewarn_days = prewarn_days
        self.seasonal_window_days = seasonal_window_days
        self.dip_buy_pct = dip_buy_pct
        self.fast_adapt_weight = fast_adapt_weight
        self.default_window_days = default_window_days

    def seasons_for(self, item_id: str, day: date | None = None) -> list[SeasonConfig]:
        return self.registry.seasons_for(item_id, day or datetime.now(UTC).date())

    def is_seasonal(self, item_id: str, day: date | None = None) -> bool:
        return bool(self.seasons_for(item_id, day))

    def history_window_days(self, item_id: str, day: date | None = None) -> int:
        """Seasonal items use the shortened window."""
        if self.is_seasonal(item_id, day):
            return self.seasonal_window_days
        return self.default_window_days

    def adapt_weight(self, item_id: str, day: date | None = None) -> float:
        """Seasonal items get the fast-adapt weight x3."""
        if self.is_seasonal(item_id, day):
            return self.fast_adapt_weight
        return 1.0

    async def check_dip_buy(
        self,
        provider: PreseasonAvgProvider,
        item_id: str,
        quality: int,
        upgrade: int,
        current_price: float,
        day: date | None = None,
    ) -> NotificationEvent | None:
        """Price fell below DIP_BUY_PCT of the preseason average -> buy signal."""
        day = day or datetime.now(UTC).date()
        seasons = self.seasons_for(item_id, day)
        if not seasons:
            return None
        season = seasons[0]
        season_start = season.current_start(day)
        season_hours = max(
            1,
            int(
                (
                    datetime.combine(day, datetime.min.time(), tzinfo=UTC)
                    - datetime.combine(season_start, datetime.min.time(), tzinfo=UTC)
                ).total_seconds()
                // 3600
            ),
        )
        preseason_avg = await provider.avg_before(
            item_id, quality, upgrade, hours_back=24 * 30, season_hours=season_hours
        )
        if preseason_avg is None or preseason_avg <= 0:
            return None
        dip_line = preseason_avg * (1.0 - self.dip_buy_pct / 100.0)
        if current_price <= dip_line:
            return NotificationEvent(
                event_type=EventType.SEASON_DIP_BUY,
                item_id=item_id,
                title=f"Сезонная просадка (dip buy): {item_id} q{quality} +{upgrade}",
                fields={
                    "season": season.name,
                    "quality": quality,
                    "upgrade": upgrade,
                    "price": current_price,
                    "preseason_avg": round(preseason_avg, 2),
                    "dip_line": round(dip_line, 2),
                },
            )
        return None

    def seasonal_drop_event(
        self, item_id: str, quality: int, upgrade: int, drop_pct: float, day: date | None = None
    ) -> NotificationEvent | None:
        """Seasonal items: dump is re-emitted as a separate 'seasonal drop' type."""
        seasons = self.seasons_for(item_id, day)
        if not seasons:
            return None
        return NotificationEvent(
            event_type=EventType.SEASONAL_DROP,
            item_id=item_id,
            title=f"Сезонное падение: {item_id} q{quality} +{upgrade}",
            fields={
                "season": seasons[0].name,
                "quality": quality,
                "upgrade": upgrade,
                "drop_pct": round(drop_pct, 1),
            },
        )

    async def check_transitions(
        self, store: SeasonStateStore, day: date | None = None
    ) -> list[NotificationEvent]:
        """Season prewarn/start/end notifications, once per occurrence."""
        day = day or datetime.now(UTC).date()
        events: list[NotificationEvent] = []
        # P1-6: tag by the year the season STARTS, not the current date - a
        # winter season (Dec..Feb) used to refire its start event every January.

        for season in self.registry.upcoming(day, self.prewarn_days):
            key = (
                f"season:prewarn:{season.name}:{season.current_start(day).strftime('%Y')}"
            )
            if await store.get(key) is None:
                await store.set(key, day.isoformat())
                events.append(
                    NotificationEvent(
                        event_type=EventType.SEASON_PREWARN,
                        item_id="",
                        title=f"Скоро сезон: {season.name}",
                        fields={
                            "season": season.name,
                            "starts": season.start,
                            "items": list(season.item_ids),
                            "prewarn_days": self.prewarn_days,
                        },
                    )
                )

        active_names = {s.name for s in self.registry.active_at(day)}
        for season in self.registry.all():
            start_year = season.current_start(day).strftime("%Y")
            start_key = f"season:start:{season.name}:{start_year}"
            if season.name in active_names:
                if await store.get(start_key) is None:
                    await store.set(start_key, day.isoformat())
                    events.append(
                        NotificationEvent(
                            event_type=EventType.SEASON_START,
                            item_id="",
                            title=f"Сезон начался: {season.name}",
                            fields={
                                "season": season.name,
                                "items": list(season.item_ids),
                                "window_days": self.seasonal_window_days,
                                "fast_adapt_weight": self.fast_adapt_weight,
                            },
                        )
                    )
            else:
                # P1-6: a season may have started under an earlier year tag
                # (winter: Dec 2026 start, Mar 2027 end). Close EVERY open
                # start key instead of guessing the year from "today".
                started_years = sorted(
                    k.rsplit(":", 1)[1]
                    for k in await store.keys_with_prefix(f"season:start:{season.name}:")
                )
                for started_year in started_years:
                    end_key = f"season:end:{season.name}:{started_year}"
                    if await store.get(end_key) is None:
                        await store.set(end_key, day.isoformat())
                        events.append(
                            NotificationEvent(
                                event_type=EventType.SEASON_END,
                                item_id="",
                                title=f"Сезон завершён: {season.name}",
                                fields={
                                    "season": season.name,
                                    "items": list(season.item_ids),
                                    "note": "стандартные правила восстановлены",
                                },
                            )
                        )
        return events
