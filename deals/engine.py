"""Deal math engine: net profit with fee, ROI, balance, projections. Race-safe writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from database.engine import Database
from database.repositories.deals import DealsRepository
from database.repositories.history import HistoryRepository


def deal_net_profit(buy_price: float, sell_price: float, amount: int, fee: float) -> float:
    """(sell * (1 - fee) - buy) * amount."""
    return (sell_price * (1.0 - fee) - buy_price) * amount


def deal_roi_pct(buy_price: float, sell_price: float, fee: float) -> float:
    if buy_price <= 0:
        return 0.0
    return (sell_price * (1.0 - fee) - buy_price) / buy_price * 100.0


@dataclass
class DealsSummary:
    realized_profit: float
    roi_pct: float
    invested_open: float
    balance: float
    projected_balance: float
    open_count: int
    closed_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "realized_profit": round(self.realized_profit, 2),
            "roi_pct": round(self.roi_pct, 1),
            "invested_open": round(self.invested_open, 2),
            "balance": round(self.balance, 2),
            "projected_balance": round(self.projected_balance, 2),
            "open_count": self.open_count,
            "closed_count": self.closed_count,
        }


def summarize(
    deals: list[Any], fee: float, avg_prices: dict[tuple[str, int, int], float]
) -> DealsSummary:
    realized = 0.0
    invested_open = 0.0
    projected = 0.0
    spent_closed = 0.0
    open_count = closed_count = 0
    for deal in deals:
        if deal.status == "closed" and deal.sell_price is not None:
            realized += deal_net_profit(deal.buy_price, deal.sell_price, deal.amount, fee)
            spent_closed += deal.buy_price * deal.amount
            closed_count += 1
        else:
            invested_open += deal.buy_price * deal.amount
            open_count += 1
            avg = avg_prices.get((deal.item_id, deal.rarity, deal.upgrade))
            if avg is not None:
                projected += avg * (1.0 - fee) * deal.amount
            else:
                projected += deal.buy_price * deal.amount
    balance = realized - invested_open
    roi = (realized / spent_closed * 100.0) if spent_closed > 0 else 0.0
    return DealsSummary(
        realized_profit=realized,
        roi_pct=roi,
        invested_open=invested_open,
        balance=balance,
        projected_balance=balance + projected,
        open_count=open_count,
        closed_count=closed_count,
    )


class DealsEngine:
    def __init__(self, db: Database, fee: float) -> None:
        self.db = db
        self.fee = fee

    # P1-9(g): the retry-wrapper close_deal/update_deal were never used (the
    # router talks to the repository directly) and retrying a stale-version
    # optimistic lock is meaningless - removed.

    async def summary(self, username: str, history_rows_per_item: int = 50) -> DealsSummary:
        async with self.db.session() as session:
            repo = DealsRepository(session)
            # P1-9(v): totals must not silently truncate at the default 500
            deals = await repo.list(username, limit=1_000_000)
            history = HistoryRepository(session, rows_per_item=history_rows_per_item)
            open_keys = {
                (deal.item_id, deal.rarity, deal.upgrade) for deal in deals if deal.status == "open"
            }
            avg_prices = await history.avg_for_keys(open_keys)  # one GROUP BY, no N+1
        return summarize(deals, self.fee, avg_prices)
