"""Ring buffer history: FIFO cap, cheapest-only, isolation, windows, rollup, prune."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from database.repositories.history import HistoryRepository


async def _fill(repo: HistoryRepository, item="a", q=4, u=0, prices=None, start=None):
    start = start or datetime.now(UTC)
    for i, price in enumerate(prices or []):
        await repo.add_sale(item, q, u, price, created_at=start + timedelta(minutes=i))


class TestRingBuffer:
    async def test_fifo_eviction(self, session):
        repo = HistoryRepository(session, rows_per_item=3)
        await _fill(repo, prices=[10, 20, 30, 40, 50])
        avg = await repo.get_avg("a", 4, 0)
        assert avg == pytest.approx(40.0)  # kept 30, 40, 50

    async def test_cheapest_only_mode(self, session):
        repo = HistoryRepository(session, rows_per_item=3, cheapest_only=True)
        await _fill(repo, prices=[10, 20, 30, 40, 50])
        avg = await repo.get_avg("a", 4, 0)
        assert avg == pytest.approx(20.0)  # kept 10, 20, 30

    async def test_isolation_between_groups(self, session):
        repo = HistoryRepository(session, rows_per_item=2)
        await _fill(repo, item="a", q=4, u=0, prices=[1, 2, 3])
        await _fill(repo, item="a", q=4, u=15, prices=[100, 200])
        await _fill(repo, item="b", q=4, u=0, prices=[7, 8, 9])
        assert await repo.get_avg("a", 4, 0) == pytest.approx(2.5)
        assert await repo.get_avg("a", 4, 15) == pytest.approx(150.0)
        assert await repo.get_avg("b", 4, 0) == pytest.approx(8.5)

    async def test_amount_normalization(self, session):
        repo = HistoryRepository(session)
        await repo.add_sale("a", 4, 0, 100.0, amount=3)
        await repo.add_sale("a", 4, 0, 200.0, amount=1)
        # weighted: (100*3 + 200*1)/4 = 125
        assert await repo.get_avg("a", 4, 0) == pytest.approx(125.0)


class TestWindows:
    async def test_window_avg(self, session):
        repo = HistoryRepository(session)
        now = datetime.now(UTC)
        await repo.add_sale("a", 4, 0, 100.0, created_at=now - timedelta(hours=1))
        await repo.add_sale("a", 4, 0, 200.0, created_at=now - timedelta(hours=30))
        assert await repo.window_avg("a", 4, 0, hours=24) == pytest.approx(100.0)
        assert await repo.window_avg("a", 4, 0, hours=24, offset_hours=24) == pytest.approx(200.0)

    async def test_fast_adapt_weight(self, session):
        repo = HistoryRepository(session)
        await _fill(repo, prices=[100, 100, 100, 100, 100, 200])
        plain = await repo.get_avg("a", 4, 0)
        weighted = await repo.get_avg("a", 4, 0, fast_adapt_weight=3.0)
        assert weighted is not None and plain is not None
        assert weighted > plain  # recent spike weighs more

    async def test_window_spread(self, session):
        repo = HistoryRepository(session)
        now = datetime.now(UTC)
        await repo.add_sale("a", 4, 0, 100.0, created_at=now - timedelta(hours=1))
        await repo.add_sale("a", 4, 0, 110.0, created_at=now - timedelta(hours=2))
        spread = await repo.window_spread_pct("a", 4, 0, hours=24)
        assert spread == pytest.approx(10 / 105 * 100, rel=0.01)

    async def test_daily_avg(self, session):
        repo = HistoryRepository(session)
        now = datetime.now(UTC)
        await repo.add_sale("a", 4, 0, 100.0, created_at=now - timedelta(days=1))
        await repo.add_sale("a", 4, 0, 300.0, created_at=now - timedelta(days=1))
        series = await repo.daily_avg("a", 4, 0, days=7)
        assert len(series) == 1
        assert series[0][1] == pytest.approx(200.0)
        assert series[0][2] == 2

    async def test_bulk_insert_and_cap(self, session):
        repo = HistoryRepository(session, rows_per_item=5)
        rows = [{"item_id": "a", "quality": 4, "upgrade": 0, "price": float(p)} for p in range(10)]
        inserted = await repo.bulk_insert(rows)
        assert inserted == 10
        assert await repo.sample_size("a", 4, 0) == 5

    async def test_price_cache(self, session):
        repo = HistoryRepository(session)
        await repo.add_sale("a", 4, 0, 100.0)
        await repo.add_sale("a", 4, 15, 500.0)
        cache = await repo.get_price_cache("a")
        assert cache[(4, 0)] == pytest.approx(100.0)
        assert cache[(4, 15)] == pytest.approx(500.0)


class TestRollupPrune:
    async def test_rollup_daily(self, session):
        repo = HistoryRepository(session)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        await repo.add_sale("a", 4, 0, 100.0)
        await repo.add_sale("a", 4, 0, 200.0)
        rolled = await repo.rollup_daily(day=today)
        assert rolled == 1
        series = await repo.get_rollup_series("a", 4, 0)
        assert len(series) == 1
        assert series[0].avg_price == pytest.approx(150.0)
        # idempotent
        rolled = await repo.rollup_daily(day=today)
        assert rolled == 1
        assert len(await repo.get_rollup_series("a", 4, 0)) == 1

    async def test_prune(self, session):
        repo = HistoryRepository(session)
        old = datetime.now(UTC) - timedelta(days=100)
        await repo.add_sale("a", 4, 0, 100.0, created_at=old)
        await repo.add_sale("a", 4, 0, 200.0)
        pruned = await repo.prune_older_than(90)
        assert pruned == 1

    async def test_stats(self, session):
        repo = HistoryRepository(session)
        await repo.add_sale("a", 4, 0, 100.0)
        stats = await repo.get_stats()
        assert stats["rows"] == 1 and stats["items"] == 1
