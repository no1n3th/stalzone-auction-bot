"""DB-backed session store for the web user area.

Sessions live in the `sessions` table (sha256 of the token -> username),
so restarts — including the OOM self-healing exit — no longer log users out.
The TTL comes from `settings.session_ttl_hours` and is shared with the cookie.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete

from database.orm import SessionRow

SESSION_COOKIE = "sz_session"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class SessionStore:
    """Async session repository; `db` is anything with an async `session()` ctx."""

    def __init__(self, db: Any, ttl_hours: int = 168) -> None:
        self.db = db
        self.ttl = timedelta(hours=ttl_hours)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        async with self.db.session() as session:
            session.add(
                SessionRow(
                    token_hash=self._hash(token),
                    username=username,
                    expires_at=_utcnow() + self.ttl,
                )
            )
        return token

    async def get(self, token: str | None) -> str | None:
        if not token:
            return None
        async with self.db.session() as session:
            row = await session.get(SessionRow, self._hash(token))
            if row is None:
                return None
            if _aware(row.expires_at) <= _utcnow():
                await session.delete(row)  # lazy expiry
                return None
            return str(row.username)

    async def drop(self, token: str | None) -> None:
        if not token:
            return
        async with self.db.session() as session:
            await session.execute(
                delete(SessionRow).where(SessionRow.token_hash == self._hash(token))
            )

    async def drop_user(self, username: str) -> int:
        """Revoke all sessions of `username` (used when the account is deleted)."""
        async with self.db.session() as session:
            result = await session.execute(
                delete(SessionRow).where(SessionRow.username == username)
            )
            return int(getattr(result, "rowcount", 0) or 0)

    async def gc(self) -> int:
        """Delete expired sessions; returns the number of rows removed."""
        async with self.db.session() as session:
            result = await session.execute(
                delete(SessionRow).where(SessionRow.expires_at < _utcnow())
            )
            return int(getattr(result, "rowcount", 0) or 0)
