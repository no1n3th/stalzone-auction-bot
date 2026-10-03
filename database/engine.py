"""Async engine factory + Alembic migration runner (dual dialect)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

log = structlog.get_logger(__name__)


def normalize_dsn(dsn: str) -> str:
    """Force async drivers."""
    if dsn.startswith("postgres://"):
        return "postgresql+asyncpg://" + dsn[len("postgres://") :]
    if dsn.startswith("postgresql://") and "+asyncpg" not in dsn:
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    if dsn.startswith("sqlite://") and "+aiosqlite" not in dsn:
        return "sqlite+aiosqlite://" + dsn[len("sqlite://") :]
    return dsn


class Database:
    def __init__(self, dsn: str, echo: bool = False, run_migrations: bool = True) -> None:
        self.dsn = normalize_dsn(dsn)
        self.echo = echo
        self.run_migrations = run_migrations
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None

    async def connect(self, retries: int = 5, delay: float = 2.0) -> None:
        if self.dsn.startswith("sqlite"):
            # ensure parent dir for file-based sqlite
            path = self.dsn.split("///")[-1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"echo": self.echo, "pool_pre_ping": True}
        if self.dsn.startswith("sqlite"):
            kwargs.update(pool_size=3, max_overflow=2)
        else:
            kwargs.update(pool_size=5, max_overflow=5, pool_recycle=1800)
        self.engine = create_async_engine(self.dsn, **kwargs)
        if self.dsn.startswith("sqlite"):
            self._install_sqlite_pragmas()
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        last_exc: BaseException | None = None
        for attempt in range(retries):
            try:
                async with self.engine.connect():
                    pass
                break
            except Exception as exc:
                last_exc = exc
                log.warning("db_connect_retry", attempt=attempt, error=str(exc))
                await asyncio.sleep(delay)
        else:
            raise RuntimeError(f"cannot connect to database: {last_exc}")
        if self.run_migrations:
            await asyncio.to_thread(self._run_migrations, self.dsn)
        log.info("db_connected", dsn=self.dsn.split("@")[-1])

    def _install_sqlite_pragmas(self) -> None:
        """WAL + relaxed sync + busy timeout on every new sqlite connection.

        Prevents 'database is locked' when the scanner, history sync and the
        web layer write concurrently.
        """
        assert self.engine is not None

        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA temp_store=MEMORY")
                cursor.execute("PRAGMA cache_size=-8000")  # 8 MiB
                cursor.execute("PRAGMA journal_size_limit=67108864")  # 64 MiB
                cursor.execute("PRAGMA wal_autocheckpoint=1000")
            finally:
                cursor.close()

    @staticmethod
    def _run_migrations(dsn: str) -> None:
        os.environ["DATABASE_URL"] = dsn
        cfg = AlembicConfig(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        command.upgrade(cfg, "head")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self.session_factory is None:
            raise RuntimeError("database is not connected")
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
            self.session_factory = None
