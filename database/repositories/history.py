"""Auction history repository: ring buffer (FIFO cap), sliding averages, rollups."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from typing import Any

import sqlalchemy as sa
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.orm import AuctionHistoryRow, DailyRollupRow

# P0-4: every time window runs on the SALE time (sold_at), falling back to the
# download time (created_at) only for legacy rows without a sale timestamp.
EFFECTIVE_AT = func.coalesce(AuctionHistoryRow.sold_at, AuctionHistoryRow.created_at)


class HistoryRepository:
    def __init__(
        self,
        session: AsyncSession,
        rows_per_item: int = 50,
        cheapest_only: bool = False,
    ) -> None:
        self.session = session
        self.rows_per_item = rows_per_item
        self.cheapest_only = cheapest_only

    async def add_sale(
        self,
        item_id: str,
        quality: int,
        upgrade: int,
        price: float,
        amount: int = 1,
        source: str = "history",
        created_at: datetime | None = None,
    ) -> AuctionHistoryRow:
        row = AuctionHistoryRow(
            item_id=item_id,
            quality=quality,
            upgrade=upgrade,
            price=price,
            amount=max(1, amount),
            source=source,
        )
        if created_at is not None:
            row.created_at = created_at
        self.session.add(row)
        await self.session.flush()
        await self._enforce_cap(item_id, quality, upgrade)
        return row

    async def _enforce_cap(self, item_id: str, quality: int, upgrade: int) -> None:
        """Ring buffer: keep at most N rows per (item, quality, upgrade).

        Single set-based DELETE (no COUNT + SELECT + DELETE chain).
        FIFO eviction by default; in cheapest_only mode keep the cheapest rows.
        """
        keep_order = (
            (AuctionHistoryRow.price.asc(), AuctionHistoryRow.id.asc())
            if self.cheapest_only
            else (EFFECTIVE_AT.desc(), AuctionHistoryRow.id.desc())
        )
        keep = (
            select(AuctionHistoryRow.id)
            .where(
                AuctionHistoryRow.item_id == item_id,
                AuctionHistoryRow.quality == quality,
                AuctionHistoryRow.upgrade == upgrade,
            )
            .order_by(*keep_order)
            .limit(self.rows_per_item)
        )
        await self.session.execute(
            delete(AuctionHistoryRow).where(
                AuctionHistoryRow.item_id == item_id,
                AuctionHistoryRow.quality == quality,
                AuctionHistoryRow.upgrade == upgrade,
                AuctionHistoryRow.id.not_in(keep),
            )
        )

    async def bulk_insert(self, rows: Sequence[dict[str, Any]]) -> int:
        """Mass insert + one set-based prune per touched group (no N+1)."""
        if not rows:
            return 0
        groups: set[tuple[str, int, int]] = set()
        values: list[dict[str, Any]] = []
        for r in rows:
            item_id = str(r["item_id"])
            quality = int(r.get("quality", 0))
            upgrade = int(r.get("upgrade", 0))
            rec: dict[str, Any] = {
                "item_id": item_id,
                "quality": quality,
                "upgrade": upgrade,
                "price": float(r["price"]),
                "amount": max(1, int(r.get("amount", 1))),
                "source": str(r.get("source", "history")),
                "sold_at": r.get("sold_at"),
            }
            if r.get("created_at") is not None:
                rec["created_at"] = r["created_at"]
            values.append(rec)
            groups.add((item_id, quality, upgrade))
        # AUDIT D1: idempotent re-sync — rows already present under the dedup
        # unique index (item, quality, upgrade, sold_at, price, amount) are
        # skipped instead of duplicated.
        bind = self.session.bind
        assert bind is not None  # repository always runs inside an open session
        ins = pg_insert if bind.dialect.name == "postgresql" else sqlite_insert
        result = await self.session.execute(
            ins(AuctionHistoryRow)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=["item_id", "quality", "upgrade", "sold_at", "price", "amount"]
            )
        )
        await self.session.flush()
        rc = result.rowcount  # type: ignore[attr-defined]
        inserted = rc if rc is not None else len(values)
        for item_id, quality, upgrade in groups:
            await self._enforce_cap(item_id, quality, upgrade)
        return inserted

    async def get_avg(
        self,
        item_id: str,
        quality: int,
        upgrade: int,
        window_days: int | None = None,
        fast_adapt_weight: float = 1.0,
    ) -> float | None:
        """Sliding average; recent third of the window weighs `fast_adapt_weight` x."""
        stmt = select(AuctionHistoryRow.price, AuctionHistoryRow.amount).where(
            AuctionHistoryRow.item_id == item_id,
            AuctionHistoryRow.quality == quality,
            AuctionHistoryRow.upgrade == upgrade,
        )
        if window_days:
            since = datetime.now(UTC) - timedelta(days=window_days)
            stmt = stmt.where(since <= EFFECTIVE_AT)
        # P0-4: deterministic order — rows[-recent_n:] used to be arbitrary
        stmt = stmt.order_by(EFFECTIVE_AT.asc(), AuctionHistoryRow.id.asc())
        rows = (await self.session.execute(stmt)).all()
        if not rows:
            return None
        prices = [float(p) for p, _ in rows]
        amounts = [max(1, int(a)) for _, a in rows]
        total = sum(amounts)
        base_avg = sum(p * a for p, a in zip(prices, amounts, strict=True)) / total
        if fast_adapt_weight <= 1.0 or len(rows) < 3:
            return base_avg
        recent_n = max(1, len(rows) // 3)
        recent = rows[-recent_n:]
        recent_avg = sum(float(p) for p, _ in recent) / len(recent)
        weight = fast_adapt_weight
        return (base_avg + recent_avg * (weight - 1.0)) / weight

    async def sample_size(self, item_id: str, quality: int, upgrade: int) -> int:
        return int(
            await self.session.scalar(
                select(func.count(AuctionHistoryRow.id)).where(
                    AuctionHistoryRow.item_id == item_id,
                    AuctionHistoryRow.quality == quality,
                    AuctionHistoryRow.upgrade == upgrade,
                )
            )
            or 0
        )

    async def sample_sizes(self, item_id: str) -> dict[tuple[int, int], int]:
        """All (quality, upgrade) sample counts for an item in one GROUP BY query."""
        stmt = (
            select(
                AuctionHistoryRow.quality,
                AuctionHistoryRow.upgrade,
                func.count(AuctionHistoryRow.id),
            )
            .where(AuctionHistoryRow.item_id == item_id)
            .group_by(AuctionHistoryRow.quality, AuctionHistoryRow.upgrade)
        )
        rows = (await self.session.execute(stmt)).all()
        return {(int(q), int(u)): int(c) for q, u, c in rows}

    async def avg_for_keys(
        self, keys: Collection[tuple[str, int, int]]
    ) -> dict[tuple[str, int, int], float]:
        """Weighted average for many (item, quality, upgrade) keys in one query."""
        unique = list(set(keys))
        if not unique:
            return {}
        stmt = (
            select(
                AuctionHistoryRow.item_id,
                AuctionHistoryRow.quality,
                AuctionHistoryRow.upgrade,
                func.sum(AuctionHistoryRow.price * AuctionHistoryRow.amount),
                func.sum(AuctionHistoryRow.amount),
            )
            .where(
                tuple_(
                    AuctionHistoryRow.item_id,
                    AuctionHistoryRow.quality,
                    AuctionHistoryRow.upgrade,
                ).in_(unique)
            )
            .group_by(
                AuctionHistoryRow.item_id, AuctionHistoryRow.quality, AuctionHistoryRow.upgrade
            )
        )
        rows = (await self.session.execute(stmt)).all()
        return {
            (str(i), int(q), int(u)): float(total) / float(amount)
            for i, q, u, total, amount in rows
            if amount
        }

    async def get_price_cache(
        self, item_id: str, window_days: int | None = None, fast_adapt_weight: float = 1.0
    ) -> dict[tuple[int, int], float]:
        """Avg prices for all (quality, upgrade) pairs of the item."""
        stmt = select(
            AuctionHistoryRow.quality,
            AuctionHistoryRow.upgrade,
            func.sum(AuctionHistoryRow.price * AuctionHistoryRow.amount),
            func.sum(AuctionHistoryRow.amount),
        ).where(AuctionHistoryRow.item_id == item_id)
        if window_days:
            since = datetime.now(UTC) - timedelta(days=window_days)
            stmt = stmt.where(since <= EFFECTIVE_AT)
        stmt = stmt.group_by(AuctionHistoryRow.quality, AuctionHistoryRow.upgrade)
        rows = (await self.session.execute(stmt)).all()
        cache: dict[tuple[int, int], float] = {}
        for quality, upgrade, total, amount in rows:
            if amount:
                cache[(int(quality), int(upgrade))] = float(total) / float(amount)
        return cache

    async def window_avg(
        self,
        item_id: str,
        quality: int,
        upgrade: int,
        hours: int,
        offset_hours: int = 0,
    ) -> float | None:
        """Avg over [now-offset-hours, now-offset] window."""
        now = datetime.now(UTC)
        end = now - timedelta(hours=offset_hours)
        start = end - timedelta(hours=hours)
        value = await self.session.scalar(
            select(func.avg(AuctionHistoryRow.price)).where(
                AuctionHistoryRow.item_id == item_id,
                AuctionHistoryRow.quality == quality,
                AuctionHistoryRow.upgrade == upgrade,
                start <= EFFECTIVE_AT,
                end > EFFECTIVE_AT,
            )
        )
        return float(value) if value is not None else None

    async def window_count(
        self, item_id: str, quality: int, upgrade: int, hours: int, offset_hours: int = 0
    ) -> int:
        now = datetime.now(UTC)
        end = now - timedelta(hours=offset_hours)
        start = end - timedelta(hours=hours)
        return int(
            await self.session.scalar(
                select(func.count(AuctionHistoryRow.id)).where(
                    AuctionHistoryRow.item_id == item_id,
                    AuctionHistoryRow.quality == quality,
                    AuctionHistoryRow.upgrade == upgrade,
                    start <= EFFECTIVE_AT,
                    end > EFFECTIVE_AT,
                )
            )
            or 0
        )

    async def window_spread_pct(
        self,
        item_id: str,
        quality: int,
        upgrade: int,
        hours: int,
        since: datetime | None = None,
    ) -> float | None:
        """(max-min)/avg * 100 — stabilization check. `since` (P1-7) narrows the
        window to sales after invalidation, ignoring the pre-dump volatility."""
        now = datetime.now(UTC)
        start = now - timedelta(hours=hours)
        if since is not None and since > start:
            start = since
        row = (
            await self.session.execute(
                select(
                    func.min(AuctionHistoryRow.price),
                    func.max(AuctionHistoryRow.price),
                    func.avg(AuctionHistoryRow.price),
                ).where(
                    AuctionHistoryRow.item_id == item_id,
                    AuctionHistoryRow.quality == quality,
                    AuctionHistoryRow.upgrade == upgrade,
                    start <= EFFECTIVE_AT,
                )
            )
        ).one()
        lo, hi, avg = row
        if lo is None or hi is None or not avg:
            return None
        return (float(hi) - float(lo)) / float(avg) * 100.0

    async def daily_avg(
        self, item_id: str, quality: int, upgrade: int, days: int = 30
    ) -> list[tuple[str, float, int]]:
        """(day, avg_price, volume) series, oldest first."""
        since = datetime.now(UTC) - timedelta(days=days)
        # AUDIT B2: func.date() follows the session TZ on PostgreSQL — pin UTC
        # explicitly there; SQLite stores naive UTC already.
        bind = self.session.bind
        assert bind is not None
        if bind.dialect.name == "postgresql":
            day_col = func.date(sa.func.timezone("UTC", EFFECTIVE_AT))
        else:
            day_col = func.date(EFFECTIVE_AT)
        stmt = (
            select(
                day_col.label("day"),
                func.sum(AuctionHistoryRow.price * AuctionHistoryRow.amount)
                / func.sum(AuctionHistoryRow.amount),
                func.sum(AuctionHistoryRow.amount),
            )
            .where(
                AuctionHistoryRow.item_id == item_id,
                AuctionHistoryRow.quality == quality,
                AuctionHistoryRow.upgrade == upgrade,
                since <= EFFECTIVE_AT,
            )
            .group_by(day_col)
            .order_by(day_col)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(str(d), float(avg), int(vol or 0)) for d, avg, vol in rows]

    async def rollup_daily(self, day: str | None = None) -> int:
        """Aggregate raw rows into daily_rollup for the given UTC day (default: yesterday)."""
        if day is None:
            day = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        # AUDIT B2: func.date() follows the session TZ on PostgreSQL — pin UTC
        # explicitly there; SQLite stores naive UTC already.
        bind = self.session.bind
        assert bind is not None
        if bind.dialect.name == "postgresql":
            day_col = func.date(sa.func.timezone("UTC", EFFECTIVE_AT))
        else:
            day_col = func.date(EFFECTIVE_AT)
        # bind a real date object: asyncpg rejects `date = varchar` comparisons
        day_value = date_type.fromisoformat(day)
        stmt = (
            select(
                AuctionHistoryRow.item_id,
                AuctionHistoryRow.quality,
                AuctionHistoryRow.upgrade,
                func.avg(AuctionHistoryRow.price),
                func.min(AuctionHistoryRow.price),
                func.max(AuctionHistoryRow.price),
                func.sum(AuctionHistoryRow.amount),
            )
            .where(day_col == day_value)
            .group_by(
                AuctionHistoryRow.item_id, AuctionHistoryRow.quality, AuctionHistoryRow.upgrade
            )
        )
        rows = (await self.session.execute(stmt)).all()
        for item_id, quality, upgrade, avg_p, min_p, max_p, vol in rows:
            existing = await self.session.scalar(
                select(DailyRollupRow).where(
                    DailyRollupRow.day == day,
                    DailyRollupRow.item_id == item_id,
                    DailyRollupRow.quality == quality,
                    DailyRollupRow.upgrade == upgrade,
                )
            )
            if existing is None:
                self.session.add(
                    DailyRollupRow(
                        day=day,
                        item_id=item_id,
                        quality=int(quality),
                        upgrade=int(upgrade),
                        avg_price=float(avg_p),
                        min_price=float(min_p),
                        max_price=float(max_p),
                        volume=int(vol or 0),
                    )
                )
            else:
                existing.avg_price = float(avg_p)
                existing.min_price = float(min_p)
                existing.max_price = float(max_p)
                existing.volume = int(vol or 0)
        await self.session.flush()
        return len(rows)

    async def get_rollup_series(
        self, item_id: str, quality: int, upgrade: int, days: int = 90
    ) -> list[DailyRollupRow]:
        stmt = (
            select(DailyRollupRow)
            .where(
                DailyRollupRow.item_id == item_id,
                DailyRollupRow.quality == quality,
                DailyRollupRow.upgrade == upgrade,
            )
            .order_by(DailyRollupRow.day.desc())
            .limit(days)
        )
        rows = (await self.session.scalars(stmt)).all()
        return list(reversed(rows))

    async def prune_older_than(self, days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        result = await self.session.execute(
            delete(AuctionHistoryRow).where(cutoff > EFFECTIVE_AT)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def get_stats(self) -> dict[str, int]:
        total = int(await self.session.scalar(select(func.count(AuctionHistoryRow.id))) or 0)
        items = int(
            await self.session.scalar(select(func.count(func.distinct(AuctionHistoryRow.item_id))))
            or 0
        )
        return {"rows": total, "items": items}
