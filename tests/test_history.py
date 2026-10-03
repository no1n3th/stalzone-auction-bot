"""History repository: weighted averages, ring-buffer cap, bulk dedup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from database.repositories.history import HistoryRepository


async def seed(repo: HistoryRepository, prices: list[float], item="y5vw"):
    for p in prices:
        await repo.add_sale(item, 4, 0, float(p))


class TestAverages:
    async def test_simple_avg(self, session):
        repo = HistoryRepository(session)
        await seed(repo, [100, 200, 300])
        assert await repo.get_avg("y5vw", 4, 0) == pytest.approx(200.0)

    async def test_weighted_by_amount(self, session):
        repo = HistoryRepository(session)
        await repo.add_sale("y5vw", 4, 0, 100, amount=3)
        await repo.add_sale("y5vw", 4, 0, 1000, amount=1)
        assert await repo.get_avg("y5vw", 4, 0) == pytest.approx(325.0)

    async def test_window_filters_old(self, session):
        repo = HistoryRepository(session)
        await repo.add_sale("y5vw", 4, 0, 10)
        old = datetime.now(UTC) - timedelta(days=40)
        row = await repo.add_sale("y5vw", 4, 0, 1000, created_at=old)
        assert row.id > 0  # inserted
        avg = await repo.get_avg("y5vw", 4, 0, window_days=30)
        assert avg == pytest.approx(10.0)

    async def test_fast_adapt_prefers_recent(self, session):
        """Seasonal mode: recent prices weigh more."""
        repo = HistoryRepository(session)
        await seed(repo, [100, 100, 100, 900, 900])
        base = await repo.get_avg("y5vw", 4, 0)
        boosted = await repo.get_avg("y5vw", 4, 0, fast_adapt_weight=3.0)
        assert boosted > base

    async def test_missing(self, session):
        repo = HistoryRepository(session)
        assert await repo.get_avg("nope", 0, 0) is None


class TestSampleAndCache:
    async def test_sample_size(self, session):
        repo = HistoryRepository(session)
        await seed(repo, [1, 2, 3])
        assert await repo.sample_size("y5vw", 4, 0) == 3
        sizes = await repo.sample_sizes("y5vw")
        assert sizes.get((4, 0)) == 3

    async def test_price_cache(self, session):
        repo = HistoryRepository(session)
        await repo.add_sale("y5vw", 4, 0, 500)
        await repo.add_sale("y5vw", 4, 15, 9000)
        cache = await repo.get_price_cache("y5vw")
        assert cache[(4, 0)] == pytest.approx(500)
        assert cache[(4, 15)] == pytest.approx(9000)

    async def test_avg_for_keys_batch(self, session):
        repo = HistoryRepository(session)
        await repo.add_sale("a", 0, 0, 100)
        await repo.add_sale("b", 5, 15, 500)
        avgs = await repo.avg_for_keys([("a", 0, 0), ("b", 5, 15), ("c", 0, 0)])
        assert avgs[("a", 0, 0)] == pytest.approx(100)
        assert avgs[("b", 5, 15)] == pytest.approx(500)
        assert ("c", 0, 0) not in avgs


class TestRingBuffer:
    async def test_cap_per_group(self, session):
        repo = HistoryRepository(session, rows_per_item=3)
        await seed(repo, [1, 2, 3, 4, 5])
        assert await repo.sample_size("y5vw", 4, 0) == 3
        prices = [
            r
            for (r,) in await session.execute(
                __import__("sqlalchemy").select(
                    __import__("database.orm", fromlist=["AuctionHistoryRow"]).AuctionHistoryRow.price
                )
            )
        ]
        assert sorted(prices) == [3.0, 4.0, 5.0]  # FIFO eviction

    async def test_bulk_insert_dedup(self, session):
        """AUDIT D1: re-sync of the same sale must not duplicate."""
        repo = HistoryRepository(session)
        row = {
            "item_id": "y5vw",
            "quality": 4,
            "upgrade": 0,
            "price": 1000.0,
            "amount": 1,
            "sold_at": datetime.now(UTC),
        }
        assert await repo.bulk_insert([row]) == 1
        assert await repo.bulk_insert([row]) == 0
        assert await repo.sample_size("y5vw", 4, 0) == 1


class TestRollupsAndPrune:
    async def test_rollup_daily_and_series(self, session):
        repo = HistoryRepository(session)
        await seed(repo, [100, 200])
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        rolled = await repo.rollup_daily(day)
        assert rolled >= 1
        series = await repo.get_rollup_series("y5vw", 4, 0)
        assert len(series) >= 1
        assert series[-1].volume == 2

    async def test_prune_old(self, session):
        repo = HistoryRepository(session)
        await seed(repo, [1, 2, 3])
        removed = await repo.prune_older_than(days=0)
        assert removed == 3
        assert await repo.get_avg("y5vw", 4, 0) is None
