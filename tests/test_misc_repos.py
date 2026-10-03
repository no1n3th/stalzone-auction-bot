"""Misc repositories, analytics engine, deals engine, misc utils."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from database.orm import SentLotRow
from database.repositories.misc import (
    ConfigRepository,
    DLQRepository,
    FeatureFlagsRepository,
    MarketRepository,
    MetaStateRepository,
    NotificationsRepository,
    SentLotsRepository,
)
from database.repositories.users import UsersRepository, hash_password, verify_password


class TestUsers:
    def test_hash_verify(self):
        h = hash_password("secret")
        assert verify_password("secret", h)
        assert not verify_password("wrong", h)
        assert not verify_password("x", "garbage")

    async def test_repo(self, session):
        repo = UsersRepository(session)
        await repo.create("alice", "pw123456", is_admin=True)
        assert await repo.verify("alice", "pw123456") is not None
        assert await repo.verify("alice", "bad") is None
        assert await repo.count() == 1
        await repo.create("bob", "pw123456")
        assert len(await repo.list_all()) == 2
        assert await repo.delete("bob")
        assert not await repo.delete("bob")


class TestSentLots:
    async def test_mark_and_check(self, session):
        repo = SentLotsRepository(session, cooldown_min=30)
        assert not await repo.is_sent("sig1")
        await repo.mark_sent("sig1", "y5vw", 100.0)
        assert await repo.is_sent("sig1")

    async def test_remark_extends(self, session):
        repo = SentLotsRepository(session)
        await repo.mark_sent("sig1", "y5vw", 100.0)
        await repo.mark_sent("sig1", "y5vw", 150.0)  # refresh
        assert await repo.is_sent("sig1")

    async def test_concurrent_marks_no_error(self, session_factory):
        """AUDIT R9: parallel mark_sent on the same signature must not raise
        (old select-then-insert raced on UNIQUE(signature) -> IntegrityError)."""
        import asyncio

        async def _mark() -> None:
            async with session_factory() as s:
                repo = SentLotsRepository(s)
                await repo.mark_sent("sig-concurrent", "y5vw", 100.0)
                await s.commit()

        await asyncio.gather(*[_mark() for _ in range(5)])
        async with session_factory() as s:
            repo = SentLotsRepository(s)
            assert await repo.is_sent("sig-concurrent")

    async def test_upsert_updates_price(self, session):
        """AUDIT R9: re-mark refreshes price/created_at via ON CONFLICT."""
        repo = SentLotsRepository(session)
        await repo.mark_sent("sig-up", "y5vw", 100.0)
        await session.commit()
        await repo.mark_sent("sig-up", "y5vw", 250.0)
        await session.commit()
        row = (
            await session.execute(select(SentLotRow).where(SentLotRow.signature == "sig-up"))
        ).scalar_one()
        assert row.price == 250.0

    async def test_outbox_lifecycle(self, session):
        """AUDIT R4-outbox: add -> pending -> mark_sent; attempt cap enforced."""
        from database.repositories.misc import MAX_OUTBOX_ATTEMPTS, OutboxRepository

        repo = OutboxRepository(session)
        row_id = await repo.add('{"kind": "test"}')
        # FIX (3.1.1, P0-2.4): fresh rows (< MIN_OUTBOX_AGE_SEC) may still sit in
        # the in-memory queue and must not be re-enqueued — age the row first.
        row = await session.get(type(await session.get(__import__("database.orm", fromlist=["OutboxRow"]).OutboxRow, row_id)), row_id)
        row.created_at = datetime.now(UTC) - timedelta(seconds=300)
        await session.flush()
        pending = await repo.pending()
        assert [r.id for r in pending] == [row_id]
        await repo.mark_sent(row_id)
        assert await repo.pending() == []
        # attempt cap: rows with attempts >= MAX are not pending anymore
        row2 = await repo.add('{"kind": "test2"}')
        for _ in range(MAX_OUTBOX_ATTEMPTS):
            await repo.mark_attempt(row2)
        assert await repo.pending() == []

    async def test_cleanup(self, session):
        repo = SentLotsRepository(session)
        await repo.mark_sent("sig1", "y5vw", 100.0)
        assert await repo.cleanup(older_than_hours=0) == 1
        assert not await repo.is_sent("sig1")


class TestDLQ:
    async def test_add_due_done(self, session):
        repo = DLQRepository(session)
        row = await repo.add("discord", {"a": 1})
        due = await repo.due()
        assert len(due) == 1
        await repo.mark_done(row.id)
        assert await repo.due() == []
        assert await repo.count_pending() == 0

    async def test_retry_backoff(self, session):
        repo = DLQRepository(session)
        row = await repo.add("discord", {"a": 1})
        await repo.mark_retry(row.id, backoff_sec=3600)
        assert await repo.due() == []  # not due yet
        assert await repo.count_pending() == 1


class TestConfigFlagsMeta:
    async def test_config_kv(self, session):
        repo = ConfigRepository(session)
        assert await repo.get("k") is None
        await repo.set("k", "v")
        assert await repo.get("k") == "v"
        await repo.set("k", "v2")
        assert (await repo.all())["k"] == "v2"

    async def test_flags(self, session):
        repo = FeatureFlagsRepository(session)
        assert not await repo.is_enabled("x")
        await repo.set("x", True)
        assert await repo.is_enabled("x")
        assert (await repo.all())["x"] is True

    async def test_meta_state(self, session):
        repo = MetaStateRepository(session)
        await repo.set("invalid:a:4:0", "1000")
        assert await repo.get("invalid:a:4:0") == "1000"
        assert "invalid:a:4:0" in await repo.invalidated_keys()
        await repo.delete("invalid:a:4:0")
        assert await repo.get("invalid:a:4:0") is None


class TestMarket:
    async def test_replace_and_get(self, session):
        repo = MarketRepository(session)
        await repo.replace_for_item(
            "y5vw",
            [
                {"quality": 4, "upgrade": 0, "min_price": 800.0, "lots_count": 3},
                {"quality": 4, "upgrade": 15, "min_price": 5000.0, "lots_count": 1},
            ],
        )
        assert await repo.get_price("y5vw", 4, 0) == 800.0
        assert len(await repo.all_for_item("y5vw")) == 2
        await repo.replace_for_item(
            "y5vw", [{"quality": 4, "upgrade": 0, "min_price": 700.0, "lots_count": 5}]
        )
        assert await repo.get_price("y5vw", 4, 0) == 700.0
        assert len(await repo.all_for_item("y5vw")) == 1


class TestNotificationsRepo:
    async def test_add_and_recent(self, session):
        repo = NotificationsRepository(session)
        await repo.add("dump", "y5vw", {"drop_pct": 25})
        await repo.add("lot", "rn1z", {"price": 100})
        rows = await repo.recent(limit=10)
        assert len(rows) == 2
        dumps = await repo.recent(event_type="dump")
        assert len(dumps) == 1
        assert await repo.count_since("dump", "y5vw", minutes=60) == 1


class TestAnalyticsEngine:
    async def test_digest_trends_forecast(self, session_factory):
        from analytics.analytics import AnalyticsEngine
        from database.repositories.history import HistoryRepository

        async with session_factory() as s:
            repo = HistoryRepository(s)
            for p in (100, 110, 120, 130, 140):
                await repo.add_sale("a", 4, 0, float(p))
            await s.commit()

        engine = AnalyticsEngine(session_factory)
        digest = await engine.daily_digest()
        assert digest["items"][0]["item_id"] == "a"

        trends = await engine.trends("a", days=7)
        assert trends["avg_now"] == pytest.approx(120.0)

        forecast = await engine.forecast_price([100, 110, 120, 130, 140])
        assert len(forecast) == 3
        assert forecast[0] > 140  # linear uptrend
        assert await engine.forecast_price([1.0, 2.0]) == []


class TestDealsEngine:
    async def test_close_and_summary(self, session_factory):
        """FIX (3.1.1, P1-9г): DealsEngine.close_deal/update_deal (retry wrappers)
        removed — closing goes through the repository; engine keeps summary."""
        from database.repositories.deals import DealConflictError, DealsRepository
        from deals.engine import DealsEngine

        class FakeDB:
            def __init__(self, sf):
                self.session_factory = sf

            def session(self):
                factory = self.session_factory

                class _CM:
                    async def __aenter__(self):
                        self.s = factory()
                        return self.s

                    async def __aexit__(self, *exc):
                        if exc[0] is None:
                            await self.s.commit()
                        else:
                            await self.s.rollback()
                        await self.s.close()
                        return False

                return _CM()

        db = FakeDB(session_factory)
        engine = DealsEngine(db, fee=0.05)
        async with db.session() as s:
            row = await DealsRepository(s).create("alice", "y5vw", 4, 0, 1, 800.0)
            deal_id = row.id

        async with db.session() as s:
            closed = await DealsRepository(s).close(deal_id, "alice", version=1, sell_price=1000.0)
        assert closed.status == "closed"

        async with db.session() as s:
            with pytest.raises(DealConflictError):
                await DealsRepository(s).close(deal_id, "alice", version=1, sell_price=1000.0)

        summary = await engine.summary("alice")
        assert summary.realized_profit == pytest.approx(150.0)
