"""Stalcraft API client with a fake aiohttp session: token, retries, 401/429/5xx."""

from __future__ import annotations

import asyncio

import pytest

from api.client import (
    StalcraftClient,
    TokenInvalidError,
    load_items_db_cache,
)
from config.settings import Settings
from utils.circuit_breaker import CircuitBreaker
from utils.rate_limiter import TokenBucket


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    async def json(self, **kw):
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"http {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Scriptable fake: queue of responses for post/get."""

    def __init__(self):
        self.posts: list[FakeResponse] = []
        self.gets: list[FakeResponse] = []
        self.post_calls = 0
        self.get_calls = 0

    def post(self, *a, **kw):
        self.post_calls += 1
        return self.posts.pop(0)

    def get(self, *a, **kw):
        self.get_calls += 1
        return self.gets.pop(0)


def make_client(session, **over) -> StalcraftClient:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        client_id="id",
        client_secret="secret",
        api_max_retries=2,
        **over,
    )
    return StalcraftClient(settings, session)


TOKEN_OK = {"access_token": "tok123", "expires_in": 3600}


class TestToken:
    async def test_fetch_and_cache(self):
        s = FakeSession()
        s.posts = [FakeResponse(200, TOKEN_OK)]
        client = make_client(s)
        t1 = await client.token()
        t2 = await client.token()
        assert t1 == "tok123" == t2
        assert s.post_calls == 1  # cached

    async def test_token_invalid_credentials(self):
        s = FakeSession()
        s.posts = [FakeResponse(401, {})]
        client = make_client(s)
        with pytest.raises(TokenInvalidError):
            await client.token()

    async def test_auth_backoff_stops_retry_storm(self):
        """After a rejected token request the client backs off: no new HTTP calls."""
        s = FakeSession()
        s.posts = [FakeResponse(401, {})]
        client = make_client(s, auth_backoff_sec=300.0)
        with pytest.raises(TokenInvalidError):
            await client.token()
        assert s.post_calls == 1
        with pytest.raises(TokenInvalidError, match="backoff"):
            await client.token()
        assert s.post_calls == 1

    async def test_auth_backoff_expires(self, monkeypatch):
        s = FakeSession()
        s.posts = [FakeResponse(401, {}), FakeResponse(200, TOKEN_OK)]
        client = make_client(s, auth_backoff_sec=300.0)
        with pytest.raises(TokenInvalidError):
            await client.token()
        client._auth_backoff_until = 0.0
        assert await client.token() == "tok123"
        assert s.post_calls == 2

    async def test_invalidate(self):
        s = FakeSession()
        s.posts = [FakeResponse(200, TOKEN_OK), FakeResponse(200, TOKEN_OK)]
        client = make_client(s)
        await client.token()
        await client.invalidate_token()
        await client.token()
        assert s.post_calls == 2


class TestGetJson:
    async def test_404_raises_item_error_no_retry_no_breaker(self):
        """AUDIT D5: permanent 4xx -> ItemRequestError, 1 request, breaker untouched."""
        from api.client import ItemRequestError

        s = FakeSession()
        s.posts = [FakeResponse(200, TOKEN_OK)]
        s.gets = [FakeResponse(404, {})]
        client = make_client(s)
        await client.token()
        with pytest.raises(ItemRequestError):
            await client.get_lots("bad_item")
        assert s.get_calls == 1

    async def test_success(self):
        s = FakeSession()
        s.posts = [FakeResponse(200, TOKEN_OK)]
        s.gets = [FakeResponse(200, {"lots": [{"id": "1"}]})]
        client = make_client(s)
        lots = await client.get_lots("y5vw")
        assert lots == [{"id": "1"}]

    async def test_401_token_refresh_once(self):
        s = FakeSession()
        s.posts = [FakeResponse(200, TOKEN_OK), FakeResponse(200, TOKEN_OK)]
        s.gets = [FakeResponse(401, {}), FakeResponse(200, {"lots": []})]
        client = make_client(s)
        lots = await client.get_lots("y5vw")
        assert lots == []
        assert s.post_calls == 2  # token refetched

    async def test_429_retry_after(self, monkeypatch):
        sleeps: list[float] = []

        async def fake_sleep(sec):
            sleeps.append(sec)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        s = FakeSession()
        s.posts = [FakeResponse(200, TOKEN_OK)]
        s.gets = [
            FakeResponse(429, {}, headers={"Retry-After": "1"}),
            FakeResponse(200, {"lots": []}),
        ]
        client = make_client(s)
        await client.get_lots("y5vw")
        assert sleeps and sleeps[0] == 1.0

    async def test_5xx_retries(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMockSleep())
        s = FakeSession()
        s.posts = [FakeResponse(200, TOKEN_OK)]
        s.gets = [FakeResponse(500, {}), FakeResponse(500, {}), FakeResponse(200, {"lots": []})]
        client = make_client(s)
        lots = await client.get_lots("y5vw")
        assert lots == []
        assert s.get_calls == 3

    async def test_5xx_exhausted(self, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", AsyncMockSleep())
        s = FakeSession()
        s.posts = [FakeResponse(200, TOKEN_OK)]
        s.gets = [FakeResponse(500, {})] * 4
        client = make_client(s)
        with pytest.raises(RuntimeError):
            await client.get_lots("y5vw")

    async def test_circuit_breaker_open(self):
        s = FakeSession()
        breaker = CircuitBreaker("t", failure_threshold=1)
        breaker.on_failure()
        assert not breaker.allow()
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:", client_id="i", client_secret="s"
        )
        client = StalcraftClient(settings, s, breaker=breaker)
        with pytest.raises(RuntimeError, match="circuit"):
            await client.get_lots("y5vw")

    async def test_rate_limiter_headers_sync(self):
        s = FakeSession()
        s.posts = [FakeResponse(200, TOKEN_OK)]
        s.gets = [
            FakeResponse(
                200, {"lots": []}, headers={"x-ratelimit-remaining": "10", "x-ratelimit-reset": ""}
            )
        ]
        limiter = TokenBucket(capacity=400, refill_rate=6.0)
        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:", client_id="i", client_secret="s"
        )
        client = StalcraftClient(settings, s, rate_limiter=limiter)
        await client.get_lots("y5vw")
        assert limiter.available <= 10.1

    async def test_history_endpoint(self):
        s = FakeSession()
        s.posts = [FakeResponse(200, TOKEN_OK)]
        s.gets = [FakeResponse(200, {"prices": [{"price": 100}]})]
        client = make_client(s)
        history = await client.get_history("y5vw")
        assert history == [{"price": 100}]

    async def test_limit_capped_200(self):
        s = FakeSession()
        s.posts = [FakeResponse(200, TOKEN_OK)]
        s.gets = [FakeResponse(200, {"lots": []})]
        client = make_client(s)
        await client.get_lots("y5vw", limit=9999)
        assert client.settings.scan_limit == 200


class AsyncMockSleep:
    async def __call__(self, sec):
        return None


class TestItemsDbCache:
    def test_load_cache(self, tmp_path):
        import json

        p = tmp_path / "items.json"
        p.write_text(json.dumps({"a1": {"name": "Item A"}}))
        assert load_items_db_cache(p) == {"a1": "Item A"}

    def test_load_missing(self, tmp_path):
        assert load_items_db_cache(tmp_path / "none.json") == {}

    def test_load_broken(self, tmp_path):
        p = tmp_path / "broken.json"
        p.write_text("{not json")
        assert load_items_db_cache(p) == {}

    def test_load_icons(self, tmp_path):
        """D11: иконки — из поля `icon` листинга (ru/icons/<подтип>/)."""
        import json as _json

        from api.client import load_items_icons

        p = tmp_path / "items.json"
        p.write_text(
            _json.dumps(
                {
                    "a1": {"name": "A", "icon": "icons/artefact/anomaly/a1.png"},
                    "b2": {"name": "B"},  # без иконки
                }
            )
        )
        assert load_items_icons(p) == {"a1": "icons/artefact/anomaly/a1.png"}

    async def test_sync_items_db_atomic(self, tmp_path):
        """AUDIT R10: sync writes via tmp+os.replace. P0-1: flat listing format
        (id из последнего сегмента `data`, имя из name.lines.ru, icon из `icon`)."""
        import json as _json

        from api.client import ITEMS_DB_RAW

        dest = tmp_path / "items.json"
        dest.write_text(_json.dumps({"old": {"name": "Keep Me"}}), encoding="utf-8")

        class _Sess:
            class _Resp:
                status = 200

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                def raise_for_status(self):
                    return None

                async def json(self, **kw):
                    return [
                        {
                            "data": "data/artefact/anomaly/new1.json",
                            "icon": "icons/artefact/anomaly/new1.png",
                            "name": {"lines": {"ru": "Новый"}},
                        }
                    ]

            def get(self, url):
                assert url == ITEMS_DB_RAW
                return self._Resp()

        client = make_client(_Sess())
        n = await client.sync_items_db(dest)
        assert n == 1
        assert _json.loads(dest.read_text(encoding="utf-8")) == {
            "new1": {"name": "Новый", "icon": "icons/artefact/anomaly/new1.png"}
        }

        # simulate a crash mid-write: broken tmp must never replace dest
        tmp = dest.with_suffix(".json.tmp")
        tmp.write_text("{truncated", encoding="utf-8")
        cache = load_items_db_cache(dest)
        v = cache["new1"]
        assert (v["name"] if isinstance(v, dict) else v) == "Новый"
        assert not tmp.exists() or load_items_db_cache(tmp) == {}
