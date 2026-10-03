"""Shared fixtures: in-memory SQLite with StaticPool.

AUDIT B2: with `--db postgresql` the same fixtures run against a real
PostgreSQL (asyncpg) — TZ-sensitive SQL (func.date) is only trustworthy there.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from database.orm import Base


def pytest_addoption(parser):
    parser.addoption("--db", action="store", default="sqlite", choices=["sqlite", "postgresql"])


def pytest_configure(config):
    if config.getoption("--db") == "postgresql":
        try:
            import asyncpg  # noqa: F401
        except ImportError as exc:
            raise pytest.UsageError("--db postgresql requires the 'asyncpg' package") from exc


@pytest.fixture
async def engine(request):
    if request.config.getoption("--db") == "postgresql":
        # AUDIT B2: real PostgreSQL; DATABASE_URL from env, sane local default.
        url = os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/stalzone_test"
        )
        eng = create_async_engine(url)
    else:
        eng = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s
        await s.commit()
