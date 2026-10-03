"""Auth flow, public market data and hardening (PROMPT_2 requirements)."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient

from tests.test_web import app_container, client  # noqa: F401
from utils.login_throttle import LoginThrottle
from web.app import resolve_spa_file

CREDS = {"username": "trader_1", "password": "secret123"}


class TestRegistration:
    async def test_register_logs_in_immediately(self, client):  # noqa: F811
        r = await client.post("/api/auth/register", json=CREDS)
        assert r.status_code == 201
        assert (await client.get("/api/auth/me")).json()["username"] == "trader_1"

    async def test_login_is_case_insensitive(self, client):  # noqa: F811
        await client.post("/api/auth/register", json=CREDS)
        await client.post("/api/auth/logout")
        r = await client.post("/api/auth/login", json={**CREDS, "username": "TRADER_1"})
        assert r.status_code == 200 and r.json()["username"] == "trader_1"

    async def test_duplicate_differs_only_by_case(self, client):  # noqa: F811
        await client.post("/api/auth/register", json=CREDS)
        r = await client.post("/api/auth/register", json={**CREDS, "username": "Trader_1"})
        assert r.status_code == 409

    @pytest.mark.parametrize("username", ["ab", "a" * 21, "bad name", "bad-name", "имя_кириллица"])
    async def test_username_rules(self, client, username):  # noqa: F811
        r = await client.post("/api/auth/register", json={**CREDS, "username": username})
        assert r.status_code == 422

    async def test_short_password_rejected(self, client):  # noqa: F811
        r = await client.post("/api/auth/register", json={**CREDS, "password": "12345"})
        assert r.status_code == 422


class TestLoginThrottleApi:
    async def test_blocks_after_repeated_failures(self, client):  # noqa: F811
        await client.post("/api/auth/register", json=CREDS)
        await client.post("/api/auth/logout")
        bad = {**CREDS, "password": "wrong-pass"}
        for _ in range(10):
            assert (await client.post("/api/auth/login", json=bad)).status_code == 401
        r = await client.post("/api/auth/login", json=CREDS)  # even the right password
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


class TestThrottleUnit:
    def test_window_expires_and_reset(self):
        now = [0.0]
        t = LoginThrottle(max_attempts=2, window_sec=60, clock=lambda: now[0])
        t.fail("k")
        t.fail("k")
        assert t.retry_after("k") == pytest.approx(60)
        now[0] = 61
        assert t.retry_after("k") == 0.0
        t.fail("k")
        t.reset("k")
        t.fail("k")
        assert t.retry_after("k") == 0.0


class TestPublicMarket:
    async def test_lots_data_open_without_login(self, client):  # noqa: F811
        assert (await client.get("/api/artifacts")).status_code == 200
        r = await client.get("/api/daily-prices/y5vw", params={"quality": 4})
        assert r.status_code == 200
        assert (await client.get("/api/forecast/y5vw")).status_code == 200
        assert (await client.get("/api/market/y5vw")).status_code == 404  # no snapshot yet

    async def test_private_endpoints_still_protected(self, client):  # noqa: F811
        for path in ("/api/digest", "/api/items?q=a", "/api/deals", "/api/notifications"):
            assert (await client.get(path)).status_code == 401, path

    async def test_query_bounds(self, client):  # noqa: F811
        r = await client.get("/api/daily-prices/y5vw", params={"days": -1})
        assert r.status_code == 422


class TestSpaHardening:
    def test_traversal_is_blocked(self, tmp_path: Path):
        root = tmp_path / "spa"
        root.mkdir()
        (root / "ok.txt").write_text("ok")
        (tmp_path / "secret.env").write_text("TOKEN=1")
        assert resolve_spa_file(root, "ok.txt") == (root / "ok.txt").resolve()
        assert resolve_spa_file(root, "../secret.env") is None
        assert resolve_spa_file(root, "") is None
        assert resolve_spa_file(root, "missing.js") is None

    async def test_unknown_api_path_is_json_404(self, client):  # noqa: F811
        r = await client.get("/api/does-not-exist")
        assert r.status_code == 404 and r.json()["detail"].lower() == "not found"


class TestUserDeletionRevokesSessions:
    async def test_deleted_user_is_logged_out(self, client):  # noqa: F811
        await client.post("/api/auth/register", json={**CREDS, "username": "admin"})
        victim = AsyncClient(transport=client._transport, base_url="http://test")
        async with victim:
            await victim.post("/api/auth/register", json={**CREDS, "username": "victim"})
            assert (await victim.get("/api/auth/me")).status_code == 200
            assert (await client.delete("/api/auth/users/victim")).status_code == 204
            assert (await victim.get("/api/auth/me")).status_code == 401
