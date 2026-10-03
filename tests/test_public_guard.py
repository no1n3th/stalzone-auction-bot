import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, status

from utils.public_guard import PublicEndpointGuard


def _req(ip="1.2.3.4"):
    return SimpleNamespace(client=SimpleNamespace(host=ip))


async def test_limit_allows_below_cap():
    g = PublicEndpointGuard(window_sec=60, max_requests=3, cache_sec=45)
    for _ in range(3):
        await g.limit(_req())
    with pytest.raises(HTTPException) as ei:
        await g.limit(_req())
    assert ei.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS


async def test_limit_is_per_ip():
    g = PublicEndpointGuard(window_sec=60, max_requests=1, cache_sec=45)
    await g.limit(_req("10.0.0.1"))
    await g.limit(_req("10.0.0.2"))  # другой IP не пострадал


def test_cache_ttl():
    g = PublicEndpointGuard(cache_sec=0.01)
    assert g.cached("k") is None
    g.store("k", {"v": 1})
    assert g.cached("k") == {"v": 1}
    time.sleep(0.02)
    assert g.cached("k") is None
