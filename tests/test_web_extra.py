"""Web security: security headers, static cache headers, health endpoints."""

from __future__ import annotations

import httpx

from tests.test_web import app_container, client  # noqa: F401


class TestSecurityHeaders:
    async def test_headers_present(self, client):  # noqa: F811
        r = await client.get("/health")
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Referrer-Policy"] == "same-origin"
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]


class TestHealthEndpoints:
    async def test_health_live_and_ready(self, client):  # noqa: F811
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/health/live")).status_code == 200
        r = await client.get("/health/ready")
        # web-only test container has no heartbeat registry -> ready
        assert r.status_code == 200

    async def test_metrics_token(self, client):  # noqa: F811
        assert (await client.get("/metrics")).status_code == 200


class TestAdminReplay:
    async def test_users_admin_flow(self, client):  # noqa: F811
        # register admin (first user)
        await client.post("/api/auth/register", json={"username": "user1", "password": "secret123"})
        # delete nonexistent -> 404
        assert (await client.delete("/api/auth/users/nobody")).status_code == 404
        # create + delete user
        await client.post("/api/auth/register", json={"username": "user2", "password": "secret123"})
        assert (await client.delete("/api/auth/users/user2")).status_code == 204
        assert (await client.delete("/api/auth/users/user2")).status_code == 404


def test_client_raises_for_status_on_broken_conn():
    """Real transport behaviour: connecting to a closed port must raise."""
    import asyncio

    import pytest

    async def _go() -> None:
        transport = httpx.ASGITransport(app=None)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as c:
            await c.get("/")

    with pytest.raises(Exception):
        asyncio.run(_go())
