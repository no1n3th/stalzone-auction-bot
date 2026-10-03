"""Misc repositories: sent lots, DLQ, config KV, flags, meta state, market, notifications."""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.orm import (
    AuditLogRow,
    ConfigRow,
    DLQRow,
    FeatureFlagRow,
    MarketSnapshotRow,
    MetaStateRow,
    NotificationRow,
    OutboxRow,
    SentLotRow,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class SentLotsRepository:
    def __init__(self, session: AsyncSession, cooldown_min: int = 30) -> None:
        self.session = session
        self.cooldown_min = cooldown_min

    async def is_sent(self, signature: str) -> bool:
        row = await self.session.scalar(
            select(SentLotRow.created_at).where(SentLotRow.signature == signature)
        )
        if row is None:
            return False
        return _aware(row) > _utcnow() - timedelta(minutes=self.cooldown_min)

    async def filter_sent(self, signatures: Collection[str]) -> set[str]:
        """Batch variant of is_sent: one IN-query per 500 signatures."""
        sigs = [s for s in signatures if s]
        if not sigs:
            return set()
        cutoff = _utcnow() - timedelta(minutes=self.cooldown_min)
        found: set[str] = set()
        for i in range(0, len(sigs), 500):  # stay under the sqlite variable cap
            rows = await self.session.scalars(
                select(SentLotRow.signature).where(
                    SentLotRow.signature.in_(sigs[i : i + 500]),
                    SentLotRow.created_at > cutoff,
                )
            )
            found.update(rows)
        return found

    async def mark_sent(self, signature: str, item_id: str, price: float) -> None:
        # AUDIT R9: upsert instead of select-then-insert — the old version
        # raced on the UNIQUE(signature) index and surfaced IntegrityError
        # in _on_profitable after the lot was already "sent".
        bind = self.session.bind
        assert bind is not None  # repository always runs inside an open session
        ins = pg_insert if bind.dialect.name == "postgresql" else sqlite_insert
        await self.session.execute(
            ins(SentLotRow)
            .values(signature=signature, item_id=item_id, price=price, created_at=_utcnow())
            .on_conflict_do_update(
                index_elements=["signature"],
                set_={"price": price, "created_at": _utcnow()},
            )
        )
        await self.session.flush()

    async def cleanup(self, older_than_hours: int = 24) -> int:
        cutoff = _utcnow() - timedelta(hours=older_than_hours)
        result = await self.session.execute(
            delete(SentLotRow).where(SentLotRow.created_at < cutoff)
        )
        return int(getattr(result, "rowcount", 0) or 0)


class DLQRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, kind: str, payload: dict[str, Any]) -> DLQRow:
        row = DLQRow(kind=kind, payload=json.dumps(payload, ensure_ascii=False))
        self.session.add(row)
        await self.session.flush()
        return row

    async def due(self, limit: int = 20) -> list[DLQRow]:
        rows = await self.session.scalars(
            select(DLQRow)
            .where(DLQRow.done.is_(False), DLQRow.next_retry_at <= _utcnow())
            .order_by(DLQRow.id)
            .limit(limit)
        )
        return list(rows)

    async def mark_retry(self, row_id: int, backoff_sec: float) -> None:
        row = await self.session.get(DLQRow, row_id)
        if row is not None:
            row.attempts += 1
            row.next_retry_at = _utcnow() + timedelta(seconds=backoff_sec)
            await self.session.flush()

    async def mark_done(self, row_id: int) -> None:
        row = await self.session.get(DLQRow, row_id)
        if row is not None:
            row.done = True
            await self.session.flush()

    async def count_pending(self) -> int:
        return int(
            await self.session.scalar(select(func.count(DLQRow.id)).where(DLQRow.done.is_(False)))
            or 0
        )

    async def prune_done(self, older_than_days: int = 7) -> int:
        """Remove replayed (done) rows older than the given age."""
        cutoff = _utcnow() - timedelta(days=older_than_days)
        result = await self.session.execute(
            delete(DLQRow).where(DLQRow.done.is_(True), DLQRow.created_at < cutoff)
        )
        return int(getattr(result, "rowcount", 0) or 0)


class ConfigRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str, default: str | None = None) -> str | None:
        row = await self.session.get(ConfigRow, key)
        return row.value if row is not None else default

    async def set(self, key: str, value: str) -> None:
        row = await self.session.get(ConfigRow, key)
        if row is None:
            self.session.add(ConfigRow(key=key, value=value))
        else:
            row.value = value
        await self.session.flush()

    async def all(self) -> dict[str, str]:
        rows = await self.session.scalars(select(ConfigRow))
        return {r.key: r.value for r in rows}


class FeatureFlagsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def is_enabled(self, name: str, default: bool = False) -> bool:
        row = await self.session.get(FeatureFlagRow, name)
        return row.enabled if row is not None else default

    async def set(self, name: str, enabled: bool) -> None:
        row = await self.session.get(FeatureFlagRow, name)
        if row is None:
            self.session.add(FeatureFlagRow(name=name, enabled=enabled))
        else:
            row.enabled = enabled
        await self.session.flush()

    async def all(self) -> dict[str, bool]:
        rows = await self.session.scalars(select(FeatureFlagRow))
        return {r.name: r.enabled for r in rows}


class MetaStateRepository:
    """Persistent key-value state for meta rules (invalidated groups etc.)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str) -> str | None:
        row = await self.session.get(MetaStateRow, key)
        return row.value if row is not None else None

    async def set(self, key: str, value: str) -> None:
        row = await self.session.get(MetaStateRow, key)
        if row is None:
            self.session.add(MetaStateRow(key=key, value=value))
        else:
            row.value = value
        await self.session.flush()

    async def delete(self, key: str) -> None:
        await self.session.execute(delete(MetaStateRow).where(MetaStateRow.key == key))

    async def invalidated_keys(self, prefix: str = "invalid:") -> list[str]:
        rows = await self.session.scalars(
            select(MetaStateRow.key).where(MetaStateRow.key.like(f"{prefix}%"))
        )
        return list(rows)


class MarketRepository:
    """Latest min-price snapshot per (item, quality, upgrade)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def replace_for_item(self, item_id: str, snapshots: list[dict[str, Any]]) -> int:
        await self.session.execute(
            delete(MarketSnapshotRow).where(MarketSnapshotRow.item_id == item_id)
        )
        for snap in snapshots:
            self.session.add(
                MarketSnapshotRow(
                    item_id=item_id,
                    quality=int(snap.get("quality", 0)),
                    upgrade=int(snap.get("upgrade", 0)),
                    min_price=float(snap["min_price"]),
                    lots_count=int(snap.get("lots_count", 0)),
                )
            )
        await self.session.flush()
        return len(snapshots)

    async def get_price(self, item_id: str, quality: int, upgrade: int) -> float | None:
        value = await self.session.scalar(
            select(MarketSnapshotRow.min_price)
            .where(
                MarketSnapshotRow.item_id == item_id,
                MarketSnapshotRow.quality == quality,
                MarketSnapshotRow.upgrade == upgrade,
            )
            .order_by(MarketSnapshotRow.created_at.desc())
            .limit(1)
        )
        return float(value) if value is not None else None

    async def all_for_item(self, item_id: str) -> list[MarketSnapshotRow]:
        rows = await self.session.scalars(
            select(MarketSnapshotRow).where(MarketSnapshotRow.item_id == item_id)
        )
        return list(rows)


class NotificationsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, event_type: str, item_id: str, payload: dict[str, Any]) -> NotificationRow:
        row = NotificationRow(
            event_type=event_type,
            item_id=item_id,
            payload=json.dumps(payload, ensure_ascii=False),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def recent(self, limit: int = 50, event_type: str | None = None) -> list[NotificationRow]:
        stmt = select(NotificationRow).order_by(NotificationRow.id.desc()).limit(limit)
        if event_type:
            stmt = stmt.where(NotificationRow.event_type == event_type)
        rows = await self.session.scalars(stmt)
        return list(rows)

    async def count_since(self, event_type: str, item_id: str, minutes: int) -> int:
        cutoff = _utcnow() - timedelta(minutes=minutes)
        return int(
            await self.session.scalar(
                select(func.count(NotificationRow.id)).where(
                    NotificationRow.event_type == event_type,
                    NotificationRow.item_id == item_id,
                    NotificationRow.created_at >= cutoff,
                )
            )
            or 0
        )

    async def prune_older_than(self, days: int = 14) -> int:
        """Retention for the feed table (grows unbounded otherwise)."""
        cutoff = _utcnow() - timedelta(days=days)
        result = await self.session.execute(
            delete(NotificationRow).where(NotificationRow.created_at < cutoff)
        )
        return int(getattr(result, "rowcount", 0) or 0)


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def log(self, username: str, action: str, details: str = "") -> None:
        self.session.add(AuditLogRow(username=username, action=action, details=details))
        await self.session.flush()

    async def list(self, limit: int = 200) -> list[AuditLogRow]:
        """P1-10: the audit log was write-only — expose it (newest first)."""
        stmt = select(AuditLogRow).order_by(AuditLogRow.id.desc()).limit(limit)
        return list((await self.session.scalars(stmt)).all())


class OutboxRepository:
    """AUDIT R4-outbox: durable notification log."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, payload: str) -> int:
        row = OutboxRow(payload=payload)
        self.session.add(row)
        await self.session.flush()
        return int(row.id)

    async def pending(self, limit: int = 50, *, min_age_sec: float = 0.0) -> list[OutboxRow]:
        """min_age_sec (P0-2.4): the sweep passes MIN_OUTBOX_AGE_SEC so rows that may
        still sit in the in-memory queue are not re-enqueued (duplicate messages);
        the default 0 keeps fresh rows visible — repository/tests semantics."""
        now = _utcnow()
        stmt = (
            select(OutboxRow)
            .where(
                OutboxRow.sent_at.is_(None),
                OutboxRow.attempts < MAX_OUTBOX_ATTEMPTS,
                OutboxRow.created_at <= now - timedelta(seconds=min_age_sec),
                # P0-2: mark_attempt schedules the next try (now + 5 min);
                # without this filter the sweep re-enqueued the row every
                # cycle regardless of the backoff, flooding the queue and
                # exhausting attempts while delivery was still being retried.
                or_(
                    OutboxRow.next_attempt_at.is_(None),
                    OutboxRow.next_attempt_at <= now,
                ),
            )
            .order_by(OutboxRow.id)
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def mark_sent(self, row_id: int) -> None:
        row = await self.session.get(OutboxRow, row_id)
        if row is not None:
            row.sent_at = datetime.now(UTC)
            await self.session.flush()

    async def mark_attempt(self, row_id: int, next_attempt_at: datetime | None = None) -> None:
        row = await self.session.get(OutboxRow, row_id)
        if row is not None:
            row.attempts += 1
            row.next_attempt_at = next_attempt_at
            await self.session.flush()

    async def prune(self, sent_older_than_days: int = 3) -> int:
        """P0-2.7: drop delivered and exhausted rows — the table must not grow forever."""
        cutoff = _utcnow() - timedelta(days=sent_older_than_days)
        stmt = delete(OutboxRow).where(
            or_(
                OutboxRow.sent_at < cutoff,
                and_(OutboxRow.sent_at.is_(None), OutboxRow.attempts >= MAX_OUTBOX_ATTEMPTS),
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return int(getattr(result, "rowcount", 0) or 0)


MAX_OUTBOX_ATTEMPTS = 10
MIN_OUTBOX_AGE_SEC = 120.0
