"""Providers bridging repositories to the I/O-free analytics engines."""

from __future__ import annotations

from collections.abc import Set as ABSet
from datetime import datetime
from typing import Any

from database.engine import Database
from database.repositories.history import HistoryRepository
from database.repositories.misc import MetaStateRepository
from models.lot import Lot
from scanner.pricing import LIQUID_EARNINGS_BY_QUALITY_PCT


class HistoryMetaProvider:
    """MetaDataProvider over auction_history + in-memory market lots."""

    def __init__(
        self,
        db: Database,
        market_lots: dict[str, list[Lot]],
        rows_per_item: int = 50,
    ) -> None:
        self.db = db
        self.market_lots = market_lots
        self.rows_per_item = rows_per_item

    async def window_avg(
        self, item_id: str, quality: int, upgrade: int, hours: int, offset_hours: int = 0
    ) -> float | None:
        async with self.db.session() as session:
            repo = HistoryRepository(session, rows_per_item=self.rows_per_item)
            return await repo.window_avg(item_id, quality, upgrade, hours, offset_hours)

    async def window_count(
        self, item_id: str, quality: int, upgrade: int, hours: int, offset_hours: int = 0
    ) -> int:
        async with self.db.session() as session:
            repo = HistoryRepository(session, rows_per_item=self.rows_per_item)
            return await repo.window_count(item_id, quality, upgrade, hours, offset_hours)

    async def spread_pct(
        self,
        item_id: str,
        quality: int,
        upgrade: int,
        hours: int,
        since: datetime | None = None,
    ) -> float | None:
        async with self.db.session() as session:
            repo = HistoryRepository(session, rows_per_item=self.rows_per_item)
            return await repo.window_spread_pct(item_id, quality, upgrade, hours, since=since)

    def cheap_lots_count(
        self, item_id: str, quality: int, upgrade: int, avg: float, below_pct: float
    ) -> int:
        """Active lots cheaper than avg*(1-below_pct/100) — from the in-memory snapshot."""
        threshold = avg * (1.0 - below_pct / 100.0)
        return sum(
            1
            for lot in self.market_lots.get(item_id, [])
            if lot.quality == quality and lot.upgrade == upgrade and 0 < lot.price < threshold
        )


class DbMetaStateStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, key: str) -> str | None:
        async with self.db.session() as session:
            return await MetaStateRepository(session).get(key)

    async def set(self, key: str, value: str) -> None:
        async with self.db.session() as session:
            await MetaStateRepository(session).set(key, value)

    async def delete(self, key: str) -> None:
        async with self.db.session() as session:
            await MetaStateRepository(session).delete(key)

    async def keys_with_prefix(self, prefix: str) -> ABSet[str]:
        async with self.db.session() as session:
            return set(await MetaStateRepository(session).invalidated_keys(prefix))


class HistoryPreseasonAvgProvider:
    """Avg price before the current season started (for dip-buy signals)."""

    def __init__(self, db: Database, rows_per_item: int = 50) -> None:
        self.db = db
        self.rows_per_item = rows_per_item

    async def avg_before(
        self, item_id: str, quality: int, upgrade: int, hours_back: int, season_hours: int
    ) -> float | None:
        async with self.db.session() as session:
            repo = HistoryRepository(session, rows_per_item=self.rows_per_item)
            return await repo.window_avg(
                item_id, quality, upgrade, hours=hours_back, offset_hours=season_hours
            )


def thresholds_from_settings(settings: Any) -> dict[int, float]:
    try:
        raw = settings.liquid_earnings_by_quality
        return {int(k): float(v) for k, v in dict(raw).items()}
    except Exception:
        return dict(LIQUID_EARNINGS_BY_QUALITY_PCT)
