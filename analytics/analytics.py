"""Compact analytics: daily digest, trends, simple forecast (pure stdlib)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import func as _func

from database.orm import AuctionHistoryRow

# P0-4: windows by sale time, not download time.
_EFFECTIVE = _func.coalesce(AuctionHistoryRow.sold_at, AuctionHistoryRow.created_at)


class AnalyticsEngine:
    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def daily_digest(self, item_id: str | None = None) -> dict[str, Any]:
        async with self._session_factory() as session:
            since = datetime.now(UTC) - timedelta(hours=24)
            stmt = select(
                AuctionHistoryRow.item_id,
                func.count(AuctionHistoryRow.id),
                func.avg(AuctionHistoryRow.price),
                func.min(AuctionHistoryRow.price),
                func.max(AuctionHistoryRow.price),
            ).where(since <= _EFFECTIVE)
            if item_id:
                stmt = stmt.where(AuctionHistoryRow.item_id == item_id)
            stmt = stmt.group_by(AuctionHistoryRow.item_id)
            rows = (await session.execute(stmt)).all()
            return {
                "since": since.isoformat(),
                "items": [
                    {
                        "item_id": r[0],
                        "sales": int(r[1]),
                        "avg_price": round(float(r[2]), 2) if r[2] else None,
                        "min_price": float(r[3]) if r[3] is not None else None,
                        "max_price": float(r[4]) if r[4] is not None else None,
                    }
                    for r in rows
                ],
            }

    async def trends(self, item_id: str, days: int = 7) -> dict[str, Any]:
        async with self._session_factory() as session:
            now = datetime.now(UTC)
            cur_since = now - timedelta(days=days)
            prev_since = now - timedelta(days=days * 2)
            cur = await session.scalar(
                select(func.avg(AuctionHistoryRow.price)).where(
                    AuctionHistoryRow.item_id == item_id,
                    cur_since <= _EFFECTIVE,
                )
            )
            prev = await session.scalar(
                select(func.avg(AuctionHistoryRow.price)).where(
                    AuctionHistoryRow.item_id == item_id,
                    prev_since <= _EFFECTIVE,
                    cur_since > _EFFECTIVE,
                )
            )
            change = None
            if cur and prev:
                change = round((float(cur) - float(prev)) / float(prev) * 100.0, 1)
            return {
                "item_id": item_id,
                "window_days": days,
                "avg_now": round(float(cur), 2) if cur else None,
                "avg_prev": round(float(prev), 2) if prev else None,
                "change_pct": change,
            }

    async def forecast_price(self, prices: list[float], steps: int = 3) -> list[float]:
        """Linear trend forecast — pure-python least squares (no numpy)."""
        n = len(prices)
        if n < 3:
            return []
        # closed-form OLS for y = a + b*x over x = 0..n-1
        sx = n * (n - 1) / 2.0
        sxx = (n - 1) * n * (2 * n - 1) / 6.0
        sy = sum(prices)
        sxy = sum(i * p for i, p in enumerate(prices))
        denom = n * sxx - sx * sx
        if denom == 0:
            return []
        b = (n * sxy - sx * sy) / denom
        a = (sy - b * sx) / n
        return [a + b * (n + k) for k in range(steps)]
