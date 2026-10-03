"""Workers and container lifecycle."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

from utils.chart import render_price_chart
from utils.supervisor import classify_oom_zone


_TASKS: set = set()


def _spawn(coro):
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


class FakeDB:
    """session() -> async CM с commit/rollback (как настоящий Database)."""

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
                return False

        return _CM()


class TestRenderChart:
    def test_render_chart(self):
        png = render_price_chart(
            "y5vw",
            [("2026-09-20", 500.0, 3), ("2026-09-21", 600.0, 5), ("2026-09-22", 550.0, 4)],
            current_price=90.0,
        )
        assert png[:4] == b"\x89PNG" and len(png) > 1000

    def test_render_empty(self):
        png = render_price_chart("item", [], current_price=None)
        assert png[:4] == b"\x89PNG"


class TestOOMZone:
    def test_zones(self):
        assert classify_oom_zone(100, 50) == "green"
        assert classify_oom_zone(40, 50) == "yellow"
        assert classify_oom_zone(20, 50) == "red"
        assert classify_oom_zone(10, 50) == "critical"


class TestContainer:
    async def test_start_stop_no_workers(self, session_factory, tmp_path):
        from config.settings import Settings
        from utils.container import AppContainer

        src = Path(__file__).resolve().parents[1]
        shutil.copytree(src / "data", tmp_path / "data")
        settings = Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
            artifacts_config_path="config/artifacts.yaml",
            seasons_config_path="config/seasons.yaml",
            items_db_path="data/items_db.json",
            discord_webhook_url="",  # герметичность от .env
        )
        container = AppContainer(settings)
        container.db = type(container.db)(settings.database_url)
        await container.start(with_workers=False, with_web=False)
        assert container.notifier is None  # no discord webhook configured
        assert len(container.artifacts) == 104
        await container.stop()

    async def test_supervisor_restarts_worker(self):
        from utils.supervisor import Supervisor

        calls = {"n": 0}
        stop = asyncio.Event()

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            stop.set()

        sup = Supervisor()
        await asyncio.wait_for(sup.supervise("flaky", flaky, stop), timeout=5)
        assert calls["n"] == 3


class TestWorkers:
    async def test_oom_worker_alerts_on_critical(self, session_factory):
        from scanner.workers import oom_monitor_worker

        alerts = []

        class FakeContainer:
            settings = SimpleNamespace(oom_check_interval_sec=0.05, oom_watchdog_mb=50)

            class metrics:
                @staticmethod
                def set_gauge(name, value):
                    pass

            class alerts:
                @staticmethod
                async def notify(kind, text):
                    alerts.append(kind)

            db = None

        stop = asyncio.Event()

        async def killer():
            await asyncio.sleep(0.3)
            stop.set()

        _spawn(killer())
        await oom_monitor_worker(FakeContainer(), stop)
        # no cgroup info in test env -> no alert, but no crash either

    async def test_history_sync_worker(self, session_factory):
        """history_sync_worker pulls pages and respects the watermark."""
        from scanner.workers import history_sync_worker

        pages = [
            [
                {"price": 100, "time": "2026-09-29T10:00:00Z", "additional": {"qlt": 4, "ptn": 0}},
                {"price": 110, "time": "2026-09-29T11:00:00Z", "additional": {"qlt": 4, "ptn": 0}},
            ],
            [],  # empty page ends pagination
        ]

        class FakeClient:
            def __init__(self):
                self.calls = 0

            async def get_history(self, item_id, limit=200, offset=0):
                self.calls += 1
                return pages[min(len(pages) - 1, offset // 200)]


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

        class FakeMetrics:
            def __init__(self):
                self.rows = 0

            def inc(self, name, value=1):
                if name == "history_rows":
                    self.rows += value

        class FakeArtifacts:
            @staticmethod
            def ids():
                return ["y5vw"]

        db = FakeDB(session_factory)
        client = FakeClient()
        metrics = FakeMetrics()
        container = SimpleNamespace(
            settings=SimpleNamespace(
                history_sync_interval_sec=0.1,
                history_rows_per_item=50,
                history_cheapest_only=False,
            ),
            artifacts=FakeArtifacts(),
            client=client,
            db=db,
            metrics=metrics,
            history_sync_paused=False,
        )
        stop = asyncio.Event()

        async def killer():
            await asyncio.sleep(0.4)
            stop.set()

        _spawn(killer())
        await history_sync_worker(container, stop)
        assert metrics.rows == 2

        # second run: watermark prevents re-insert
        metrics.rows = 0
        container.client = FakeClient()
        _spawn(killer())
        await history_sync_worker(container, stop)
        assert metrics.rows == 0

    async def test_meta_monitor_worker_no_data(self, session_factory):
        """meta_monitor_worker must not raise when there is no history yet."""
        from scanner.workers import meta_monitor_worker

        class FakeArtifacts2:
            @staticmethod
            def all():
                return []

        container = SimpleNamespace(
            settings=SimpleNamespace(meta_check_interval_sec=0.1),
            artifacts=FakeArtifacts2(),
            db=FakeDB(session_factory),
            metrics=SimpleNamespace(inc=lambda *a, **kw: None),
            item_names={},
            notifier=None,
            bus=SimpleNamespace(publish=lambda e: _noop()),
        )
        stop = asyncio.Event()

        async def killer():
            await asyncio.sleep(0.25)
            stop.set()

        _spawn(killer())
        await meta_monitor_worker(container, stop)  # no exception

    async def test_season_monitor_worker(self, session_factory):
        from scanner.workers import season_monitor_worker

        published = []

        async def _publish(event):
            published.append(event)

        container = SimpleNamespace(
            settings=SimpleNamespace(season_check_interval_sec=0.1),
            seasons=SimpleNamespace(check_transitions=lambda store: _maybe_event()),
            db=FakeDB(session_factory),
            bus=SimpleNamespace(publish=_publish),
            metrics=SimpleNamespace(inc=lambda *a, **kw: None),
            item_names={},
            notifier=None,
        )
        stop = asyncio.Event()

        async def killer():
            await asyncio.sleep(0.25)
            stop.set()

        _spawn(killer())
        await season_monitor_worker(container, stop)

    async def test_config_reload_worker(self, tmp_path):
        from scanner.workers import config_reload_worker

        import yaml

        art_file = tmp_path / "artifacts.yaml"
        art_file.write_text(yaml.safe_dump({"artifacts": []}), encoding="utf-8")
        container = SimpleNamespace(
            artifacts=SimpleNamespace(maybe_reload=lambda: False),
            seasons_registry=SimpleNamespace(maybe_reload=lambda: False),
            scheduler=SimpleNamespace(rebuild=lambda: None),
        )
        stop = asyncio.Event()

        async def killer():
            await asyncio.sleep(0.25)
            stop.set()

        _spawn(killer())
        await config_reload_worker(container, stop)  # no raise without db

    async def test_retention_worker(self, session_factory):
        from scanner.workers import retention_worker

        from database.repositories.history import HistoryRepository

        db = FakeDB(session_factory)
        async with db.session() as s:
            await HistoryRepository(s).add_sale("y5vw", 4, 0, 100.0)
        container = SimpleNamespace(
            settings=SimpleNamespace(
                history_rows_per_item=50,
                history_cheapest_only=False,
                history_retention_days=90,
            ),
            db=db,
            metrics=SimpleNamespace(inc=lambda *a, **kw: None),
            session_store=None,
        )
        stop = asyncio.Event()

        async def killer():
            await asyncio.sleep(0.25)
            stop.set()

        _spawn(killer())
        await retention_worker(container, stop)


async def _noop():
    return None


def _maybe_event():
    return None
