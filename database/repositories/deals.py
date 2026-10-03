"""Deals repository with optimistic locking (race-safe per-user isolation)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.orm import DealRow


class DealConflictError(Exception):
    """Raised when an optimistic-lock version check fails or the deal is closed."""


class DealNotFoundError(Exception):
    """Raised when the deal does not exist or belongs to another user."""


class DealsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        username: str,
        item_id: str,
        rarity: int,
        upgrade: int,
        amount: int,
        buy_price: float,
        note: str = "",
    ) -> DealRow:
        row = DealRow(
            username=username,
            item_id=item_id,
            rarity=rarity,
            upgrade=upgrade,
            amount=amount,
            buy_price=buy_price,
            note=note,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, deal_id: int, username: str) -> DealRow | None:
        row: DealRow | None = await self.session.scalar(
            select(DealRow).where(DealRow.id == deal_id, DealRow.username == username)
        )
        return row

    async def list(
        self,
        username: str,
        status: str | None = None,
        item_id: str | None = None,
        limit: int = 500,
    ) -> list[DealRow]:
        stmt = select(DealRow).where(DealRow.username == username)
        if status:
            stmt = stmt.where(DealRow.status == status)
        if item_id:
            stmt = stmt.where(DealRow.item_id == item_id)
        stmt = stmt.order_by(DealRow.id.desc()).limit(limit)
        rows = await self.session.scalars(stmt)
        return list(rows)

    async def update_fields(
        self, deal_id: int, username: str, version: int, fields: dict[str, Any]
    ) -> DealRow:
        # P1-9: distinguish "not yours / missing" (404) from "version conflict"
        # (409), and never let an edit rewrite a CLOSED deal's realized profit.
        existing = await self.get(deal_id, username)
        if existing is None:
            raise DealNotFoundError(f"deal {deal_id} not found for user")
        if existing.status != "open":
            raise DealConflictError(f"deal {deal_id} is closed")
        allowed = {"item_id", "rarity", "upgrade", "amount", "buy_price", "note"}
        safe = {k: v for k, v in fields.items() if k in allowed}
        safe["version"] = version + 1
        safe["updated_at"] = datetime.now(UTC)
        result = await self.session.execute(
            update(DealRow)
            .where(
                DealRow.id == deal_id,
                DealRow.username == username,
                DealRow.version == version,
            )
            .values(**safe)
        )
        if not getattr(result, "rowcount", 0):
            if await self.get(deal_id, username) is None:
                raise DealNotFoundError(f"deal {deal_id} not found for user")
            raise DealConflictError(f"deal {deal_id} version conflict")
        row = await self.get(deal_id, username)
        assert row is not None
        return row

    async def close(self, deal_id: int, username: str, version: int, sell_price: float) -> DealRow:
        existing = await self.get(deal_id, username)
        if existing is None:
            raise DealNotFoundError(f"deal {deal_id} not found for user")
        if existing.status != "open":
            raise DealConflictError(f"deal {deal_id} already closed")
        result = await self.session.execute(
            update(DealRow)
            .where(
                DealRow.id == deal_id,
                DealRow.username == username,
                DealRow.version == version,
                DealRow.status == "open",
            )
            .values(
                sell_price=sell_price,
                status="closed",
                closed_at=datetime.now(UTC),
                version=version + 1,
            )
        )
        if not getattr(result, "rowcount", 0):
            raise DealConflictError(f"deal {deal_id} version conflict")
        row = await self.get(deal_id, username)
        assert row is not None
        return row

    async def delete(self, deal_id: int, username: str) -> bool:
        row = await self.get(deal_id, username)
        if row is None:
            return False
        await self.session.delete(row)
        return True

    async def delete_for_user(self, username: str) -> int:
        """P2: deleting an account must not leave deals behind that a future
        account re-registering the same login would silently inherit."""
        result = await self.session.execute(delete(DealRow).where(DealRow.username == username))
        return int(getattr(result, "rowcount", 0) or 0)

    async def stats(self, username: str) -> dict[str, Any]:
        total = await self.session.scalar(
            select(func.count(DealRow.id)).where(DealRow.username == username)
        )
        open_count = await self.session.scalar(
            select(func.count(DealRow.id)).where(
                DealRow.username == username, DealRow.status == "open"
            )
        )
        return {"total": int(total or 0), "open": int(open_count or 0)}

    async def count(self) -> int:
        return int(await self.session.scalar(select(func.count(DealRow.id))) or 0)
