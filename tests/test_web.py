"""Web API end-to-end tests via httpx ASGI transport."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from deals.engine import DealsEngine
from metrics.metrics import MetricsCollector
from notify.feed import NotificationBus
from web.app import create_app


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
                return False

        return _CM()


@pytest.fixture
def app_container(session_factory, tmp_path):
    from analytics.analytics import AnalyticsEngine
    from config.artifacts import ArtifactRegistry
    from config.settings import Settings

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        artifacts_config_path="config/artifacts.yaml",
        seasons_config_path="config/seasons.yaml",
        items_db_path="data/items_db.json",
        admin_usernames="admin",
        registration_mode="open",
    )
    db = FakeDB(session_factory)
    container = SimpleNamespace(
        settings=settings,
        db=db,
        metrics=MetricsCollector(enabled=False),
        bus=NotificationBus(db),
        artifacts=ArtifactRegistry(__import__("pathlib").Path("config/artifacts.yaml")),
        analytics=AnalyticsEngine(session_factory),
        deals_engine=DealsEngine(db, fee=settings.fee),
        notifier=None,
        item_names={"y5vw": "Скорлупа", "rn1z": "Опал"},
    )
    return container


@pytest.fixture
async def client(app_container):
    app = create_app(app_container)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestAuth:
    async def test_register_login_me(self, client):
        r = await client.post(
            "/api/auth/register", json={"username": "admin", "password": "secret123"}
        )
        assert r.status_code == 201
        assert r.json()["is_admin"] is True

        r = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401

        r = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret123"}
        )
        assert r.status_code == 200
        r = await client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["username"] == "admin"

    async def test_duplicate_register(self, client):
        await client.post("/api/auth/register", json={"username": "user1", "password": "secret123"})
        r = await client.post(
            "/api/auth/register", json={"username": "user1", "password": "secret123"}
        )
        assert r.status_code == 409

    async def test_me_requires_auth(self, client):
        r = await client.get("/api/auth/me")
        assert r.status_code == 401

    async def test_health(self, client):
        r = await client.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"


async def _register_and_login(client, username="admin", password="secret123"):
    await client.post("/api/auth/register", json={"username": username, "password": password})
    await client.post("/api/auth/login", json={"username": username, "password": password})


class TestDealsAPI:
    async def test_crud_flow(self, client):
        await _register_and_login(client)
        r = await client.post(
            "/api/deals",
            json={
                "item_id": "y5vw",
                "rarity": 4,
                "upgrade": 0,
                "amount": 2,
                "buy_price": 800,
            },
        )
        assert r.status_code == 201
        deal = r.json()
        assert deal["status"] == "open"

        r = await client.get("/api/deals")
        assert len(r.json()) == 1

        r = await client.patch(
            f"/api/deals/{deal['id']}", json={"version": deal["version"], "buy_price": 750}
        )
        assert r.status_code == 200
        assert r.json()["buy_price"] == 750

        # version conflict
        r = await client.patch(
            f"/api/deals/{deal['id']}", json={"version": deal["version"], "buy_price": 700}
        )
        assert r.status_code == 409

        ver = (await client.get("/api/deals")).json()[0]["version"]
        r = await client.post(
            f"/api/deals/{deal['id']}/close", json={"version": ver, "sell_price": 1200}
        )
        assert r.status_code == 200
        closed = r.json()
        # (1200*0.95 - 750) * 2 = 780
        assert closed["net_profit"] == pytest.approx(780.0)

        r = await client.get("/api/deals/summary")
        assert r.status_code == 200
        assert r.json()["realized_profit"] == pytest.approx(780.0)

        r = await client.delete(f"/api/deals/{deal['id']}")
        assert r.status_code == 204

    async def test_isolation(self, client):
        await _register_and_login(client, "admin", "secret123")
        await client.post("/api/deals", json={"item_id": "y5vw", "buy_price": 100})
        await client.post("/api/auth/register", json={"username": "bob", "password": "secret123"})
        await client.post("/api/auth/login", json={"username": "bob", "password": "secret123"})
        r = await client.get("/api/deals")
        assert r.json() == []

    async def test_unauthorized(self, client):
        r = await client.get("/api/deals")
        assert r.status_code == 401


class TestMarketAPI:
    async def test_artifacts(self, client):
        await _register_and_login(client)
        r = await client.get("/api/artifacts")
        assert r.status_code == 200
        assert len(r.json()) == 104

    async def test_items_search(self, client):
        await _register_and_login(client)
        r = await client.get("/api/items", params={"q": "Скорлуп"})
        assert any(i["item_id"] == "y5vw" for i in r.json())

    async def test_market_404(self, client):
        await _register_and_login(client)
        r = await client.get("/api/market/y5vw", params={"quality": 4, "upgrade": 0})
        assert r.status_code == 404

    async def test_openapi_generated(self, client):
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        paths = r.json()["paths"]
        assert "/api/deals" in paths
        assert "/api/auth/login" in paths


class TestAdminAPI:
    async def test_registration_bootstrap_mode(self, client, app_container):
        """AUDIT S1: first user registers, then registration closes."""
        object.__setattr__(app_container.settings, "registration_mode", "bootstrap")
        r = await client.post(
            "/api/auth/register", json={"username": "root", "password": "secret123"}
        )
        assert r.status_code == 201
        r = await client.post(
            "/api/auth/register", json={"username": "late", "password": "secret123"}
        )
        assert r.status_code == 403

    async def test_admin_required(self, client):
        await _register_and_login(client, "admin", "secret123")  # first user = admin
        await client.post("/api/auth/register", json={"username": "bob", "password": "secret123"})
        await client.post("/api/auth/login", json={"username": "bob", "password": "secret123"})
        r = await client.get("/api/admin/stats")
        assert r.status_code == 403

    async def test_admin_stats(self, client):
        await _register_and_login(client)  # admin
        r = await client.get("/api/admin/stats")
        assert r.status_code == 200
        assert "history_rows" in r.json()

    async def test_flags_and_config(self, client):
        await _register_and_login(client)
        r = await client.post("/api/admin/flags/scanning", json={"enabled": True})
        assert r.status_code == 200
        r = await client.get("/api/admin/flags")
        assert r.json()["scanning"] is True
        r = await client.post("/api/admin/config/scan_batch_size", json={"value": "16"})
        assert r.status_code == 200
        r = await client.get("/api/admin/config")
        assert r.json()["scan_batch_size"] == "16"
