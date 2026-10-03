"""Deals CRUD + summary routes (per-user isolation, optimistic locking)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response, status

from database.repositories.deals import DealConflictError, DealNotFoundError, DealsRepository
from deals.engine import deal_net_profit, deal_roi_pct
from web.deps import ContainerDep, SessionDep, UserDep
from web.schemas import (
    DealClose,
    DealCreate,
    DealResponse,
    DealsSummaryResponse,
    DealUpdate,
)

router = APIRouter(prefix="/api/deals", tags=["deals"])


def _to_response(row: Any, fee: float) -> DealResponse:
    net = roi = None
    if row.status == "closed" and row.sell_price is not None:
        net = round(deal_net_profit(row.buy_price, row.sell_price, row.amount, fee), 2)
        roi = round(deal_roi_pct(row.buy_price, row.sell_price, fee), 1)
    return DealResponse(
        id=row.id,
        item_id=row.item_id,
        rarity=row.rarity,
        upgrade=row.upgrade,
        amount=row.amount,
        buy_price=row.buy_price,
        sell_price=row.sell_price,
        status=row.status,
        note=row.note,
        created_at=row.created_at,
        closed_at=row.closed_at,
        version=row.version,
        net_profit=net,
        roi_pct=roi,
    )


@router.get("", response_model=list[DealResponse])
async def list_deals(
    username: str = UserDep,
    session: Any = SessionDep,
    container: Any = ContainerDep,
    status_filter: str | None = Query(default=None, alias="status"),
    item_id: str | None = None,
) -> Any:
    rows = await DealsRepository(session).list(username, status=status_filter, item_id=item_id)
    return [_to_response(r, container.settings.fee) for r in rows]


@router.post("", response_model=DealResponse, status_code=status.HTTP_201_CREATED)
async def create_deal(
    body: DealCreate,
    username: str = UserDep,
    session: Any = SessionDep,
    container: Any = ContainerDep,
) -> Any:
    row = await DealsRepository(session).create(
        username=username,
        item_id=body.item_id.lower(),
        rarity=body.rarity,
        upgrade=body.upgrade,
        amount=body.amount,
        buy_price=body.buy_price,
        note=body.note,
    )
    container.metrics.inc("deals_total")
    return _to_response(row, container.settings.fee)


@router.patch("/{deal_id}", response_model=DealResponse)
async def update_deal(
    deal_id: int,
    body: DealUpdate,
    username: str = UserDep,
    session: Any = SessionDep,
    container: Any = ContainerDep,
) -> Any:
    fields = body.model_dump(exclude_none=True, exclude={"version"})
    try:
        row = await DealsRepository(session).update_fields(deal_id, username, body.version, fields)
    except DealNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="deal not found") from None
    except DealConflictError as exc:
        detail = "version conflict — refresh the row"
        if "is closed" in str(exc):
            detail = "deal is closed"
        raise HTTPException(status.HTTP_409_CONFLICT, detail=detail) from None
    return _to_response(row, container.settings.fee)


@router.post("/{deal_id}/close", response_model=DealResponse)
async def close_deal(
    deal_id: int,
    body: DealClose,
    username: str = UserDep,
    session: Any = SessionDep,
    container: Any = ContainerDep,
) -> Any:
    try:
        row = await DealsRepository(session).close(deal_id, username, body.version, body.sell_price)
    except DealNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="deal not found") from None
    except DealConflictError as exc:
        detail = "version conflict — refresh the row"
        if "already closed" in str(exc):
            detail = "already closed"
        raise HTTPException(status.HTTP_409_CONFLICT, detail=detail) from None
    return _to_response(row, container.settings.fee)


@router.delete("/{deal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deal(deal_id: int, username: str = UserDep, session: Any = SessionDep) -> Response:
    if not await DealsRepository(session).delete(deal_id, username):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="deal not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/summary", response_model=DealsSummaryResponse)
async def deals_summary(username: str = UserDep, container: Any = ContainerDep) -> Any:
    summary = await container.deals_engine.summary(
        username, history_rows_per_item=container.settings.history_rows_per_item
    )
    return DealsSummaryResponse(**summary.as_dict())
