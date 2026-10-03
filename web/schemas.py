"""Pydantic schemas for the whole REST API (OpenAPI autogen)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from config.version import VERSION


# ---------- auth ----------
class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=20, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=6, max_length=256)

    @field_validator("username")
    @classmethod
    def _canonical_username(cls, value: str) -> str:
        return value.lower()  # logins are case-insensitive: store the canonical form


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    username: str
    is_admin: bool


# ---------- deals ----------
class DealCreate(BaseModel):
    # P1-9d: reject inf/nan and cap field sizes at the API boundary.
    model_config = ConfigDict(allow_inf_nan=False)
    item_id: str = Field(min_length=1, max_length=32)
    rarity: int = Field(ge=0, le=6, default=0)
    upgrade: int = Field(ge=0, le=15, default=0)
    amount: int = Field(ge=1, le=1_000_000, default=1)
    buy_price: float = Field(ge=0, allow_inf_nan=False)
    note: str = Field(default="", max_length=500)


class DealUpdate(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    version: int
    item_id: str | None = Field(default=None, min_length=1, max_length=32)
    rarity: int | None = Field(default=None, ge=0, le=6)
    upgrade: int | None = Field(default=None, ge=0, le=15)
    amount: int | None = Field(default=None, ge=1, le=1_000_000)
    buy_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    note: str | None = Field(default=None, max_length=500)


class DealClose(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    version: int
    sell_price: float = Field(ge=0, allow_inf_nan=False)


class DealResponse(BaseModel):
    id: int
    item_id: str
    rarity: int
    upgrade: int
    amount: int
    buy_price: float
    sell_price: float | None
    status: str
    note: str
    created_at: datetime
    closed_at: datetime | None
    version: int
    net_profit: float | None = None
    roi_pct: float | None = None


class DealsSummaryResponse(BaseModel):
    realized_profit: float
    roi_pct: float
    invested_open: float
    balance: float
    projected_balance: float
    open_count: int
    closed_count: int


# ---------- market ----------
class MarketPriceResponse(BaseModel):
    item_id: str
    quality: int
    upgrade: int
    min_price: float | None


class ArtifactResponse(BaseModel):
    item_id: str
    name: str
    category: str
    min_quality: int | None
    upgrades: list[int]
    profit_threshold_pct: float | None


class DailyPricePoint(BaseModel):
    day: str
    avg_price: float
    volume: int


class ItemSearchResult(BaseModel):
    item_id: str
    name: str


# ---------- feed ----------
class NotificationResponse(BaseModel):
    id: int
    event_type: str
    item_id: str
    payload: dict[str, Any]
    created_at: datetime


# ---------- admin ----------
class ConfigSetRequest(BaseModel):
    value: str


class FlagSetRequest(BaseModel):
    enabled: bool


class AdminStatsResponse(BaseModel):
    history_rows: int
    history_items: int
    dlq_pending: int
    users: int
    deals: int
    ws_clients: int
    metrics: dict[str, float]


class HealthResponse(BaseModel):
    status: str
    version: str = VERSION


class ErrorResponse(BaseModel):
    detail: str
