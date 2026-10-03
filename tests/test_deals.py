"""Deal math (ROI/balance/fee) and race-safety tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from database.repositories.deals import DealConflictError, DealsRepository
from deals.engine import deal_net_profit, deal_roi_pct, summarize

FEE = 0.05


class TestDealMath:
    def test_net_profit(self):
        # (1000*0.95 - 800) * 2 = 300
        assert deal_net_profit(800, 1000, 2, FEE) == pytest.approx(300.0)

    def test_roi(self):
        assert deal_roi_pct(800, 1000, FEE) == pytest.approx(18.75)

    def test_roi_zero_buy(self):
        assert deal_roi_pct(0, 1000, FEE) == 0.0

    def test_summarize(self):
        deals = [
            SimpleNamespace(
                status="closed",
                buy_price=800.0,
                sell_price=1000.0,
                amount=2,
                item_id="a",
                rarity=4,
                upgrade=0,
            ),
            SimpleNamespace(
                status="open",
                buy_price=500.0,
                sell_price=None,
                amount=1,
                item_id="a",
                rarity=4,
                upgrade=0,
            ),
        ]
        avg_prices = {("a", 4, 0): 1200.0}
        s = summarize(deals, FEE, avg_prices)
        assert s.realized_profit == pytest.approx(300.0)
        assert s.invested_open == pytest.approx(500.0)
        assert s.balance == pytest.approx(-200.0)  # realized - invested_open
        # projected = balance + avg*(1-fee)*amount = -200 + 1200*0.95 = 940
        assert s.projected_balance == pytest.approx(940.0)
        assert s.roi_pct == pytest.approx(300.0 / 1600.0 * 100)

    def test_summarize_no_avg_uses_buy_price(self):
        deals = [
            SimpleNamespace(
                status="open",
                buy_price=500.0,
                sell_price=None,
                amount=1,
                item_id="x",
                rarity=0,
                upgrade=0,
            )
        ]
        s = summarize(deals, FEE, {})
        assert s.projected_balance == pytest.approx(-500.0 + 500.0)


class TestDealsRepo:
    async def test_create_and_list_isolation(self, session):
        repo = DealsRepository(session)
        await repo.create("alice", "y5vw", 4, 0, 1, 100.0)
        await repo.create("bob", "y5vw", 4, 0, 1, 200.0)
        alice_deals = await repo.list("alice")
        assert len(alice_deals) == 1 and alice_deals[0].buy_price == 100.0

    async def test_update_version_conflict(self, session):
        repo = DealsRepository(session)
        row = await repo.create("alice", "y5vw", 4, 0, 1, 100.0)
        updated = await repo.update_fields(row.id, "alice", version=1, fields={"buy_price": 150.0})
        assert updated.version == 2
        with pytest.raises(DealConflictError):
            await repo.update_fields(row.id, "alice", version=1, fields={"buy_price": 200.0})

    async def test_update_ignores_unsafe_fields(self, session):
        repo = DealsRepository(session)
        row = await repo.create("alice", "y5vw", 4, 0, 1, 100.0)
        updated = await repo.update_fields(row.id, "alice", 1, {"status": "closed", "note": "n"})
        assert updated.status == "open"
        assert updated.note == "n"

    async def test_close(self, session):
        repo = DealsRepository(session)
        row = await repo.create("alice", "y5vw", 4, 0, 1, 100.0)
        closed = await repo.close(row.id, "alice", version=1, sell_price=200.0)
        assert closed.status == "closed"
        assert closed.sell_price == 200.0
        with pytest.raises(DealConflictError):
            await repo.close(row.id, "alice", version=2, sell_price=300.0)

    async def test_concurrent_close_race(self, session_factory):
        async with session_factory() as s:
            row = await DealsRepository(s).create("alice", "y5vw", 4, 0, 1, 100.0)
            await s.commit()
            deal_id = row.id

        results = []

        async def closer(price: float):
            async with session_factory() as s:
                try:
                    await DealsRepository(s).close(deal_id, "alice", version=1, sell_price=price)
                    await s.commit()
                    results.append("ok")
                except DealConflictError:
                    await s.rollback()
                    results.append("conflict")

        await asyncio.gather(closer(150.0), closer(160.0))
        assert sorted(results) == ["conflict", "ok"]

    async def test_delete(self, session):
        repo = DealsRepository(session)
        row = await repo.create("alice", "y5vw", 4, 0, 1, 100.0)
        assert await repo.delete(row.id, "alice")
        assert not await repo.delete(row.id, "alice")

    async def test_stats(self, session):
        repo = DealsRepository(session)
        await repo.create("alice", "y5vw", 4, 0, 1, 100.0)
        stats = await repo.stats("alice")
        assert stats == {"total": 1, "open": 1}
