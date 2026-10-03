"""Alembic env: async engine, URL from DATABASE_URL or app settings."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from database.orm import Base

config = context.config
if config.config_file_name is not None and context.config.attributes.get("configure_logger", True):
    # AUDIT R1: embedded runs pass configure_logger=False so alembic.ini's
    # WARN root level does not silence the app's INFO logs.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    # AUDIT B5: an explicit sqlalchemy.url (tests, CLI) wins over the env var
    # and app settings — relative defaults must not silently target another DB.
    explicit = context.config.get_main_option("sqlalchemy.url")
    if explicit:
        return explicit
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    from config.settings import get_settings
    from database.engine import normalize_dsn

    return normalize_dsn(get_settings().database_url)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(_url()),
    )
    with context.begin_transaction():
        context.run_migrations()


def _known_revisions() -> set[str]:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config)
    return {rev.revision for rev in script.walk_revisions()}


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    # Pre-v3 databases carry an unknown alembic chain (e.g. revision 005):
    # rename their tables to legacy_* before running the consolidated baseline.
    from database.legacy import adopt_legacy_schema

    adopt_legacy_schema(connection, _known_revisions())
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=_is_sqlite(_url()),
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
