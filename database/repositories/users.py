"""Users repository + pbkdf2 password hashing (stdlib only)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.orm import UserRow

_ITERATIONS = 600_000  # P2: OWASP-recommended minimum for PBKDF2-SHA256


def needs_rehash(stored: str) -> bool:
    """True when the hash was produced with an older iteration count."""
    try:
        _, iterations, _salt, _digest = stored.split("$")
        return int(iterations) < _ITERATIONS
    except (ValueError, AttributeError):
        return True


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


# verified for unknown logins so that response time does not reveal which logins exist
_DUMMY_HASH = hash_password("stalzone-timing-equaliser")


class UsersRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, username: str, password: str, is_admin: bool = False) -> UserRow:
        # PBKDF2 is CPU-bound: keep it off the event loop
        password_hash = await asyncio.to_thread(hash_password, password)
        row = UserRow(username=username, password_hash=password_hash, is_admin=is_admin)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, username: str) -> UserRow | None:
        return await self.session.get(UserRow, username)

    async def find(self, username: str) -> UserRow | None:
        """Case-insensitive lookup (exact match first, then legacy mixed-case rows)."""
        row = await self.get(username)
        if row is not None:
            return row
        return await self.session.scalar(
            select(UserRow).where(func.lower(UserRow.username) == username.lower())
        )

    async def verify(self, username: str, password: str) -> UserRow | None:
        row = await self.find(username)
        stored = row.password_hash if row is not None else _DUMMY_HASH
        matches = await asyncio.to_thread(verify_password, password, stored)
        if not matches or row is None:
            return None
        # P2: transparent rehash on login when the iteration count was bumped
        if needs_rehash(row.password_hash):
            row.password_hash = await asyncio.to_thread(hash_password, password)
            await self.session.flush()
        return row

    async def count(self) -> int:
        return int(await self.session.scalar(select(func.count(UserRow.username))) or 0)

    async def count_admins(self) -> int:
        """P2: guard against deleting the last admin account."""
        return int(
            await self.session.scalar(
                select(func.count(UserRow.username)).where(UserRow.is_admin.is_(True))
            )
            or 0
        )

    async def list_all(self) -> list[UserRow]:
        rows = await self.session.scalars(select(UserRow).order_by(UserRow.username))
        return list(rows)

    async def delete(self, username: str) -> bool:
        result = await self.session.execute(delete(UserRow).where(UserRow.username == username))
        return bool(getattr(result, "rowcount", 0) or 0)
