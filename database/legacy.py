"""Adoption of pre-v3 databases.

The old project had its own Alembic chain (e.g. revision ``005``) and table
schemas that do not match the v3 ORM. When such a database is detected, all
its tables are renamed to ``legacy_<name>`` (data is preserved, nothing is
dropped) and ``alembic_version`` is reset, so the consolidated ``001_baseline``
migration can start from a clean slate. The ``notes`` table keeps its name so
``001_baseline`` can migrate its rows into ``deals``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

LEGACY_PREFIX = "legacy_"
#: tables that must NOT be renamed during adoption
KEEP_TABLES: AbstractSet[str] = frozenset({"alembic_version", "notes"})
#: candidate column names for the note text in a legacy notes table
NOTE_TEXT_COLUMNS: tuple[str, ...] = ("text", "note", "body", "content")


def unknown_revisions(connection: Connection, known: AbstractSet[str]) -> list[str]:
    """Return revision ids stored in alembic_version that are not in ``known``."""
    inspector = sa.inspect(connection)
    if "alembic_version" not in inspector.get_table_names():
        return []
    rows: Sequence[Any] = (
        connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
    )
    return [r for r in rows if r not in known]


def adopt_legacy_schema(connection: Connection, known: AbstractSet[str]) -> bool:
    """Rename pre-v3 tables to legacy_* and reset alembic_version.

    Returns True when a legacy database was detected and adopted.
    Safe to call on fresh or already-migrated databases (no-op then).
    """
    unknown = unknown_revisions(connection, known)
    if not unknown:
        return False
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    logger.warning(
        "legacy database detected (alembic revisions %s) — renaming old tables to %s*",
        ",".join(unknown),
        LEGACY_PREFIX,
    )
    for name in sorted(tables - set(KEEP_TABLES)):
        legacy = f"{LEGACY_PREFIX}{name}"
        if legacy in tables:
            connection.execute(sa.text(f'DROP TABLE "{legacy}"'))
        connection.execute(sa.text(f'ALTER TABLE "{name}" RENAME TO "{legacy}"'))
        logger.info("renamed table %s -> %s", name, legacy)
    connection.execute(sa.text("DELETE FROM alembic_version"))
    connection.commit()
    return True


def _to_datetime(value: object) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _iso(value: object) -> str | None:
    """Python 3.12 deprecates the default datetime adapter for sqlite3 —
    bind ISO strings instead of datetime objects."""
    dt = _to_datetime(value)
    return dt.isoformat() if dt is not None else None


def _to_int(value: object, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value)))
    except ValueError:
        return default


def _legacy_usernames(bind: Connection, tables: AbstractSet[str]) -> dict[str, str]:
    """Map legacy user ids to usernames via the renamed legacy_users table."""
    if "legacy_users" not in tables:
        return {}
    columns = {c["name"] for c in sa.inspect(bind).get_columns("legacy_users")}
    if not {"id", "username"} <= columns:
        return {}
    rows = bind.execute(sa.text("SELECT id, username FROM legacy_users")).all()
    return {str(r[0]): str(r[1]) for r in rows}


def _migrate_legacy_deals(bind: Connection, columns: AbstractSet[str]) -> int:
    """Migrate a deals-shaped legacy notes table (v2 trader cabinet).

    Old schema: id, user_id, item_id, item_name, rarity, level, quantity,
    buy_price, sell_price, actual_profit, status, created_at, sold_at.
    """
    tables = set(sa.inspect(bind).get_table_names())
    usernames = _legacy_usernames(bind, tables)
    select_cols = ["id", "user_id", "item_id", "buy_price"]
    optional = (
        "item_name",
        "rarity",
        "level",
        "quantity",
        "sell_price",
        "status",
        "created_at",
        "sold_at",
    )
    for opt in optional:
        select_cols.append(opt if opt in columns else f"NULL AS {opt}")
    rows = bind.execute(sa.text(f"SELECT {', '.join(select_cols)} FROM notes")).mappings().all()
    closed_statuses = {"sold", "closed", "done", "закрыта", "продано"}
    migrated = 0
    for row in rows:
        status_raw = str(row["status"] or "").lower()
        sell_price = _to_float(row["sell_price"])
        is_closed = sell_price is not None or status_raw in closed_statuses
        username = usernames.get(str(row["user_id"]), f"user_{row['user_id']}")
        bind.execute(
            sa.text(
                "INSERT INTO deals (username, item_id, rarity, upgrade, amount,"
                " buy_price, sell_price, status, note, version,"
                " created_at, updated_at, closed_at)"
                " VALUES (:u, :i, :r, :up, :am, :bp, :sp, :st, :n, 1,"
                " COALESCE(:ca, CURRENT_TIMESTAMP),"
                " COALESCE(:sa, :ca, CURRENT_TIMESTAMP),"
                " CASE WHEN :closed THEN :sa ELSE NULL END)"
            ),
            {
                "u": username,
                "i": str(row["item_id"] or "legacy"),
                "r": _to_int(row["rarity"]),
                "up": _to_int(row["level"]),
                "am": _to_int(row["quantity"], 1),
                "bp": _to_float(row["buy_price"]) or 0.0,
                "sp": sell_price,
                "st": "closed" if is_closed else "open",
                "n": str(row["item_name"] or ""),
                "ca": _iso(row["created_at"]),
                "sa": _iso(row["sold_at"]),
                "closed": is_closed,
            },
        )
        migrated += 1
    return migrated


def _migrate_freeform_notes(bind: Connection, columns: AbstractSet[str]) -> int:
    """Migrate a free-form notes table (id, username, text/note/...). Returns -1 on mismatch."""
    text_col = next((c for c in NOTE_TEXT_COLUMNS if c in columns), None)
    if text_col is None or not {"id", "username"} <= columns:
        return -1
    if "migrated" not in columns:
        bind.execute(
            sa.text("ALTER TABLE notes ADD COLUMN migrated BOOLEAN NOT NULL DEFAULT FALSE")
        )
    rows = bind.execute(
        sa.text(f"SELECT id, username, {text_col} AS txt FROM notes WHERE migrated = 0")
    ).all()
    for row in rows:
        bind.execute(
            sa.text(
                "INSERT INTO deals (username, item_id, rarity, upgrade, amount,"
                " buy_price, status, note, version, created_at, updated_at)"
                " VALUES (:u, :i, 0, 0, 1, 0.0, 'open', :n, 1,"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"u": row.username, "i": "legacy", "n": f"[note#{row.id}] {row.txt}"},
        )
        bind.execute(sa.text("UPDATE notes SET migrated = 1 WHERE id = :id"), {"id": row.id})
    return len(rows)


def migrate_legacy_notes(bind: Connection) -> int:
    """Copy rows from a legacy ``notes`` table into ``deals``.

    The v2 project stored actual trades in ``notes`` (buy/sell prices, profit,
    status); older schemas may be free-form text notes. Both shapes are
    migrated; unknown shapes are skipped. Afterwards a non-empty notes table
    is renamed to ``legacy_notes`` (an empty one is dropped).
    Returns the number of migrated rows.
    """
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "notes" not in tables or "deals" not in tables:
        return 0
    columns = {c["name"] for c in inspector.get_columns("notes")}
    if {"user_id", "item_id", "buy_price"} <= columns:
        migrated = _migrate_legacy_deals(bind, columns)
    else:
        migrated = _migrate_freeform_notes(bind, columns)
        if migrated < 0:
            logger.warning(
                "legacy notes table has unexpected schema %s — skipping migration", columns
            )
            return 0
    total: Any = bind.execute(sa.text("SELECT COUNT(*) FROM notes")).scalar_one()
    if total:
        bind.execute(sa.text(f'ALTER TABLE "notes" RENAME TO "{LEGACY_PREFIX}notes"'))
    else:
        bind.execute(sa.text('DROP TABLE "notes"'))
    if migrated:
        logger.info("migrated %d legacy notes into deals", migrated)
    return migrated
