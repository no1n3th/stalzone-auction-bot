"""Market data routes: artifacts, prices, daily series, forecast, digest, item search."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from database.repositories.history import HistoryRepository
from database.repositories.misc import MarketRepository
from web.deps import ContainerDep, SessionDep, UserDep
from web.schemas import (
    ArtifactResponse,
    DailyPricePoint,
    ItemSearchResult,
    MarketPriceResponse,
)

router = APIRouter(prefix="/api", tags=["market"])

# Public (no login): artifacts, market/{id}, daily-prices/{id}, forecast/{id}.
# Protected: digest, items (used by the trader cabinet).


@router.get("/artifacts", response_model=list[ArtifactResponse])
async def list_artifacts(container: Any = ContainerDep) -> Any:
    return [
        ArtifactResponse(
            item_id=a.item_id,
            name=a.name,
            category=a.category,
            min_quality=a.min_quality,
            upgrades=list(a.upgrades),
            profit_threshold_pct=a.profit_threshold_pct,
        )
        for a in container.artifacts.all()
    ]


@router.get("/market/{item_id}", response_model=MarketPriceResponse)
async def market_price(
    request: Request,
    item_id: str,
    quality: int = Query(default=0, ge=0, le=6),
    upgrade: int = Query(default=0, ge=0, le=15),
    session: Any = SessionDep,
) -> Any:
    guard = request.app.state.public_guard  # P2: rate limit + short cache
    await guard.limit(request)
    ck = f"market:{item_id.lower()}:{quality}:{upgrade}"
    if (hit := guard.cached(ck)) is not None:
        return hit
    price = await MarketRepository(session).get_price(item_id.lower(), quality, upgrade)
    if price is None:
        raise HTTPException(404, detail="no market snapshot yet")
    return guard.store(
        ck, MarketPriceResponse(item_id=item_id, quality=quality, upgrade=upgrade, min_price=price)
    )


@router.get("/daily-prices/{item_id}", response_model=list[DailyPricePoint])
async def daily_prices(
    request: Request,
    item_id: str,
    quality: int = Query(default=0, ge=0, le=6),
    upgrade: int = Query(default=0, ge=0, le=15),
    days: int = Query(default=30, ge=1, le=365),
    session: Any = SessionDep,
) -> Any:
    guard = request.app.state.public_guard
    await guard.limit(request)
    ck = f"daily:{item_id.lower()}:{quality}:{upgrade}:{days}"
    if (hit := guard.cached(ck)) is not None:
        return hit
    repo = HistoryRepository(session)
    rows = await repo.daily_avg(item_id.lower(), quality, upgrade, days=days)
    points = [DailyPricePoint(day=d, avg_price=p, volume=v) for d, p, v in rows]
    return guard.store(ck, points)


@router.get("/forecast/{item_id}")
async def forecast(
    request: Request,
    item_id: str,
    quality: int = Query(default=0, ge=0, le=6),
    upgrade: int = Query(default=0, ge=0, le=15),
    session: Any = SessionDep,
    container: Any = ContainerDep,
) -> Any:
    guard = request.app.state.public_guard
    await guard.limit(request)
    ck = f"forecast:{item_id.lower()}:{quality}:{upgrade}"
    if (hit := guard.cached(ck)) is not None:
        return hit
    repo = HistoryRepository(session)
    rows = await repo.daily_avg(item_id.lower(), quality, upgrade, days=30)
    prices = [p for _, p, _ in rows]
    values = await container.analytics.forecast_price(prices)
    return guard.store(ck, {"item_id": item_id, "forecast": [round(v, 2) for v in values]})


@router.get("/digest")
async def digest(_user: str = UserDep, container: Any = ContainerDep) -> Any:
    return await container.analytics.daily_digest()


@router.get("/items", response_model=list[ItemSearchResult])
async def search_items(
    q: str = Query(min_length=1),
    limit: int = 20,
    _user: str = UserDep,
    container: Any = ContainerDep,
) -> Any:
    q_lower = q.lower()
    results = [
        ItemSearchResult(item_id=iid, name=name)
        for iid, name in container.item_names.items()
        if q_lower in name.lower()
    ]
    return results[: max(1, min(limit, 100))]
