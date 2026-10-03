"""Notification events shared between scanner, analytics and transports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventType(StrEnum):
    LOT = "lot"  # profitable base lot (+0/+15)
    MID_LOT = "mid_lot"  # profitable intermediate upgrade +2..+14
    DUMP = "dump"  # price dump detected
    META_EXIT = "meta_exit"  # meta exit signal
    META_ENTER = "meta_enter"  # meta enter signal
    SEASON_PREWARN = "season_prewarn"  # season starts soon
    SEASON_START = "season_start"
    SEASON_END = "season_end"
    SEASONAL_DROP = "seasonal_drop"  # dump on seasonal item (separate type)
    SEASON_DIP_BUY = "season_dip_buy"  # dip-buy signal on seasonal item


@dataclass
class NotificationEvent:
    event_type: EventType
    item_id: str
    title: str
    fields: dict[str, Any] = field(default_factory=dict)
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    lot_signature: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "item_id": self.item_id,
            "title": self.title,
            "fields": self.fields,
            "detected_at": self.detected_at.isoformat(),
            "lot_signature": self.lot_signature,
        }
