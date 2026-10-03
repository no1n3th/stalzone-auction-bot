"""Auction lot model: parse, normalization, dedup signature."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from utils.helpers import parse_api_time


def _num(value: object) -> float:
    """Price can arrive as 12, "12", 12.5 - normalize defensively."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class Lot:
    item_id: str
    price: float
    quality: int
    upgrade: int
    amount: int = 1
    start_price: float | None = None
    buyout_price: float | None = None
    expiry: datetime | None = None
    lot_id: str = ""
    is_expired: bool = False
    end_time: datetime | None = None
    effective_price: float | None = None

    @classmethod
    def from_api(cls, item_id: str, raw: dict[str, Any]) -> Lot:
        item_id = item_id.strip().lower()
        extra = raw.get("additional") or {}
        end_time = parse_api_time(raw.get("endTime"))
        is_expired = bool(raw.get("expired", False))
        if end_time is not None:
            is_expired = is_expired or (datetime.now(UTC) - end_time) > timedelta(days=2)
        buyout = _num(raw.get("buyoutPrice"))
        return cls(
            item_id=item_id,
            price=buyout or _num(raw.get("price")),
            quality=int(extra.get("qlt", raw.get("quality", 0)) or 0),
            upgrade=int(extra.get("ptn", raw.get("upgrade", 0)) or 0),
            amount=max(1, int(raw.get("amount", 1) or 1)),
            start_price=_num(raw.get("startPrice")),
            buyout_price=buyout,
            expiry=end_time,
            lot_id=str(raw.get("id", "")),
            is_expired=is_expired,
            end_time=end_time,
        )

    @property
    def signature(self) -> str:
        """Dedup signature of the listing position (P1-3: documented)."""
        data = f"{self.item_id}|{self.quality}|{self.upgrade}|{self.price}|{self.amount}"
        return hashlib.sha256(data.encode()).hexdigest()[:32]
