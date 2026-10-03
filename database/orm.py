"""SQLAlchemy 2.0 ORM models (async, dual-dialect: asyncpg / aiosqlite)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

BigId = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionHistoryRow(Base, TimestampMixin):
    __tablename__ = "auction_history"
    __table_args__ = (
        Index("ix_history_item_q_u", "item_id", "quality", "upgrade", "created_at"),
        # AUDIT D1: dedup key — a re-sync of the same sale must not insert twice.
        # NULL sold_at (legacy rows) never conflicts (NULLs are distinct).
        Index(
            "ux_history_dedup",
            "item_id",
            "quality",
            "upgrade",
            "sold_at",
            "price",
            "amount",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    quality: Mapped[int] = mapped_column(Integer, default=0)
    upgrade: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[float] = mapped_column(Float)
    amount: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(16), default="history")  # history|sale
    # AUDIT D1: real sale time from the API `time` field (was ignored — every
    # row was dated by download time, corrupting every time window).
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DailyRollupRow(Base):
    __tablename__ = "daily_rollup"
    __table_args__ = (UniqueConstraint("day", "item_id", "quality", "upgrade"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD (UTC)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    quality: Mapped[int] = mapped_column(Integer, default=0)
    upgrade: Mapped[int] = mapped_column(Integer, default=0)
    avg_price: Mapped[float] = mapped_column(Float)
    min_price: Mapped[float] = mapped_column(Float)
    max_price: Mapped[float] = mapped_column(Float)
    volume: Mapped[int] = mapped_column(Integer, default=0)


class SentLotRow(Base, TimestampMixin):
    __tablename__ = "sent_lots"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    signature: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    price: Mapped[float] = mapped_column(Float)


class DLQRow(Base, TimestampMixin):
    __tablename__ = "dlq"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), default="discord")
    payload: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    done: Mapped[bool] = mapped_column(Boolean, default=False)


class ConfigRow(Base):
    __tablename__ = "config_kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AuditLogRow(Base, TimestampMixin):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[str] = mapped_column(Text, default="")


class FeatureFlagRow(Base):
    __tablename__ = "feature_flags"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class UserRow(Base, TimestampMixin):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)


class DealRow(Base, TimestampMixin):
    __tablename__ = "deals"
    __table_args__ = (Index("ix_deals_user_status", "username", "status"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    rarity: Mapped[int] = mapped_column(Integer, default=0)
    upgrade: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[int] = mapped_column(Integer, default=1)
    buy_price: Mapped[float] = mapped_column(Float)
    sell_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|closed
    note: Mapped[str] = mapped_column(Text, default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    version: Mapped[int] = mapped_column(Integer, default=1)  # optimistic locking


class MarketSnapshotRow(Base, TimestampMixin):
    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_market_item_q_u", "item_id", "quality", "upgrade"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    quality: Mapped[int] = mapped_column(Integer, default=0)
    upgrade: Mapped[int] = mapped_column(Integer, default=0)
    min_price: Mapped[float] = mapped_column(Float)
    lots_count: Mapped[int] = mapped_column(Integer, default=0)


class NotificationRow(Base, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_type", "event_type", "created_at"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(32))
    item_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")


class MetaStateRow(Base):
    __tablename__ = "meta_state"

    key: Mapped[str] = mapped_column(String(192), primary_key=True)  # e.g. invalid:y5vw:4:0
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SessionRow(Base):
    """Web sessions persisted in the DB (survive restarts / OOM self-healing)."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 hex
    username: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSettingsRow(Base):
    """Per-user UI preferences (sound, theme, thresholds) as JSON."""

    __tablename__ = "user_settings"

    username: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[str] = mapped_column(Text, default="{}")  # JSON


class OutboxRow(Base, TimestampMixin):
    """AUDIT R4-outbox: durable record of every Discord notification.

    Written at enqueue time (best-effort), marked sent after delivery; a sweep
    worker re-enqueues unsent rows so a restart (including OOM-kill) does not
    lose events that only lived in the in-memory queue.
    """

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    payload: Mapped[str] = mapped_column(Text)  # JSON of NotificationEvent
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # P0-2: replay scheduling + stable priority/event_type (was read from a
    # key embeds do not have, so every replay ran at the default priority).
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(32), default="")
    priority: Mapped[int] = mapped_column(Integer, default=50)
    # 004 создавал updated_at NOT NULL; ORM обязан его знать, иначе create_all
    # (001) строит таблицу без колонки и миграция 005 падает с KeyError.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=True
    )
