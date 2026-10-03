"""Scanner engine end-to-end with a fake API client."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from analytics.seasonality import SeasonEngine
from config.artifacts import ArtifactRegistry
from config.seasons import SeasonRegistry
from config.settings import Settings
from metrics.metrics import MetricsCollector
from models.events import EventType
from notify.feed import NotificationBus
from scanner.engine import AuctionScanner


class FakeClient:
    def __init__(self, lots):
        self._lots = lots

    async def get_lots(self, item_id, limit=200):
        return self._lots


class FakeDB:
    def __init__(self, session_factory):
        self.session_factory = session_factory

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

        return _CM()


def make_settings(tmp_path) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        artifacts_config_path="config/artifacts.yaml",
        seasons_config_path="config/seasons.yaml",
        items_db_path="data/items_db.json",
        min_sample_size=3,
        chart_enabled=False,
    )


def make_scanner(settings, session_factory, lots, tmp_path):
    artifacts = ArtifactRegistry(Path("config/artifacts.yaml"))
    seasons_registry = SeasonRegistry(Path("config/seasons.yaml"))
    seasons = SeasonEngine(seasons_registry)
    db = FakeDB(session_factory)
    bus = NotificationBus(db)
    metrics = MetricsCollector(enabled=False)
    scanner = AuctionScanner(
        settings=settings,
        db=db,
        client=FakeClient(lots),
        registry=artifacts,
        seasons=seasons,
        bus=bus,
        notifier=None,
        metrics=metrics,
        preseason_provider=None,
    )
    return scanner, bus, metrics


async def seed_history(session, item_id="y5vw", quality=4, upgrade=0, prices=None):
    from database.repositories.history import HistoryRepository

    repo = HistoryRepository(session, rows_per_item=50)
    for p in prices or [1000, 1000, 1000, 1000, 1000]:
        await repo.add_sale(item_id, quality, upgrade, float(p))


class TestScannerEngine:
    async def test_profitable_lot_published(self, session, session_factory, tmp_path):
        settings = make_settings(tmp_path)
        await seed_history(session, prices=[1000] * 5)
        lots = [
            {
                "id": "lot1",
                "buyoutPrice": 300,
                "amount": 1,
                "additional": {"qlt": 4, "ptn": 0},
            }
        ]
        scanner, bus, metrics = make_scanner(settings, session_factory, lots, tmp_path)
        received = []
        bus.subscribe(lambda e: received.append(e) or _noop())
        await scanner.scan_item("y5vw")
        assert any(e.event_type == EventType.LOT for e in received)
        assert metrics.get("lots_profitable") == 1

    async def test_api_failure_tolerated(self, session_factory, tmp_path):
        settings = make_settings(tmp_path)
        scanner, _bus, metrics = make_scanner(settings, session_factory, [], tmp_path)
        scanner.client = SimpleNamespace(get_lots=AsyncMock(side_effect=RuntimeError("api down")))
        await scanner.scan_item("y5vw")  # must not raise
        assert metrics.get("lots_fetch_failed") == 1

    async def test_untracked_item_skipped(self, session_factory, tmp_path):
        settings = make_settings(tmp_path)
        scanner, _bus, metrics = make_scanner(settings, session_factory, [], tmp_path)
        await scanner.scan_item("not-tracked")
        assert metrics.get("lots_fetched") == 0

    async def test_dedup_cooldown(self, session, session_factory, tmp_path):
        settings = make_settings(tmp_path)
        await seed_history(session, prices=[1000] * 5)
        lots = [
            {
                "id": "lot1",
                "buyoutPrice": 300,
                "amount": 1,
                "additional": {"qlt": 4, "ptn": 0},
            }
        ]
        scanner, bus, _m = make_scanner(settings, session_factory, lots, tmp_path)
        received = []
        bus.subscribe(lambda e: received.append(e) or _noop())
        await scanner.scan_item("y5vw")
        await scanner.scan_item("y5vw")  # same lot -> deduped
        assert len([e for e in received if e.event_type == EventType.LOT]) == 1

    async def test_market_snapshot_replaced(self, session, session_factory, tmp_path):
        settings = make_settings(tmp_path)
        lots = [
            {"id": "l1", "buyoutPrice": 900, "amount": 1, "additional": {"qlt": 4, "ptn": 0}},
            {"id": "l2", "buyoutPrice": 800, "amount": 1, "additional": {"qlt": 4, "ptn": 0}},
        ]
        scanner, _bus, _mm = make_scanner(settings, session_factory, lots, tmp_path)
        await scanner.scan_item("y5vw")
        from database.repositories.misc import MarketRepository

        repo = MarketRepository(session)
        assert await repo.get_price("y5vw", 4, 0) == 800.0


async def _noop():
    return None
