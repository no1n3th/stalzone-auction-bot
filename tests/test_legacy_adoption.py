"""Legacy database adoption: pre-v3 alembic chains (e.g. revision 005) and notes migration."""

from __future__ import annotations

import sqlite3
from collections.abc import Set as AbstractSet

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from database.legacy import adopt_legacy_schema, migrate_legacy_notes
from database.orm import DealRow

KNOWN: AbstractSet[str] = frozenset({"001_baseline"})


def _make_legacy_db(connection: sa.engine.Connection) -> None:
    connection.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    connection.execute(sa.text("INSERT INTO alembic_version (version_num) VALUES ('005')"))
    connection.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64))"))
    connection.execute(
        sa.text(
            "CREATE TABLE auction_history (id INTEGER PRIMARY KEY, item_id VARCHAR(16),"
            " price FLOAT)"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, username VARCHAR(64),"
            " text TEXT, created_at DATETIME)"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO notes (username, text, created_at) VALUES ('oleg', 'buy regalii',"
            " '2026-01-01')"
        )
    )
    connection.commit()


def test_unknown_revision_triggers_adoption() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        _make_legacy_db(conn)
        assert adopt_legacy_schema(conn, KNOWN) is True
        tables = set(sa.inspect(conn).get_table_names())
        assert "legacy_users" in tables
        assert "legacy_auction_history" in tables
        assert "users" not in tables
        assert "auction_history" not in tables
        assert "notes" in tables  # kept for the notes -> deals migration
        remaining = conn.execute(sa.text("SELECT COUNT(*) FROM alembic_version")).scalar_one()
        assert remaining == 0
    engine.dispose()


def test_known_revision_is_noop() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(sa.text("INSERT INTO alembic_version (version_num) VALUES ('001_baseline')"))
        conn.execute(sa.text("CREATE TABLE deals (id INTEGER PRIMARY KEY)"))
        conn.commit()
        assert adopt_legacy_schema(conn, KNOWN) is False
        assert "deals" in sa.inspect(conn).get_table_names()
    engine.dispose()


def test_missing_version_table_is_noop() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE something (id INTEGER PRIMARY KEY)"))
        conn.commit()
        assert adopt_legacy_schema(conn, KNOWN) is False
        assert "something" in sa.inspect(conn).get_table_names()
    engine.dispose()


def test_migrate_legacy_notes_without_migrated_column() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        DealRow.__table__.create(conn)
        conn.execute(
            sa.text("CREATE TABLE notes (id INTEGER PRIMARY KEY, username VARCHAR(64), text TEXT)")
        )
        conn.execute(sa.text("INSERT INTO notes (username, text) VALUES ('oleg', 'first')"))
        conn.execute(sa.text("INSERT INTO notes (username, text) VALUES ('ivan', 'second')"))
        conn.commit()

        assert migrate_legacy_notes(conn) == 2

        deals = conn.execute(
            sa.text("SELECT username, item_id, status, note FROM deals ORDER BY id")
        ).all()
        assert [(d[0], d[1], d[2]) for d in deals] == [
            ("oleg", "legacy", "open"),
            ("ivan", "legacy", "open"),
        ]
        assert deals[0][3] == "[note#1] first"
        tables = set(sa.inspect(conn).get_table_names())
        assert "notes" not in tables
        assert "legacy_notes" in tables
    engine.dispose()


def test_migrate_legacy_notes_empty_table_dropped() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        DealRow.__table__.create(conn)
        conn.execute(
            sa.text(
                "CREATE TABLE notes (id INTEGER PRIMARY KEY, username VARCHAR(64),"
                " text, migrated BOOLEAN NOT NULL DEFAULT FALSE)"
            )
        )
        conn.commit()
        assert migrate_legacy_notes(conn) == 0
        assert "notes" not in sa.inspect(conn).get_table_names()
    engine.dispose()


def test_migrate_legacy_deals_shape() -> None:
    """The real v2 'notes' table was a trade journal: user_id, prices, status."""
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        DealRow.__table__.create(conn)
        conn.execute(
            sa.text("CREATE TABLE legacy_users (id INTEGER PRIMARY KEY, username VARCHAR(64))")
        )
        conn.execute(sa.text("INSERT INTO legacy_users (id, username) VALUES (7, 'oleg')"))
        conn.execute(
            sa.text(
                "CREATE TABLE notes (id INTEGER PRIMARY KEY, user_id INTEGER,"
                " item_id VARCHAR(16), item_name VARCHAR(128), rarity INTEGER, level INTEGER,"
                " quantity INTEGER, buy_price FLOAT, sell_price FLOAT, actual_profit FLOAT,"
                " status VARCHAR(16), created_at DATETIME, sold_at DATETIME)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO notes (user_id, item_id, item_name, rarity, level, quantity,"
                " buy_price, sell_price, actual_profit, status, created_at, sold_at)"
                " VALUES (7, 'p6qz2', 'Черный Регалий', 0, 15, 2, 1000.0, 1400.0, 330.0,"
                " 'sold', '2026-01-10 12:00:00', '2026-01-11 09:30:00')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO notes (user_id, item_id, item_name, rarity, level, quantity,"
                " buy_price, sell_price, actual_profit, status, created_at, sold_at)"
                " VALUES (9, 'w4jo', 'Черная дыра', 3, 0, 1, 500.0, NULL, NULL,"
                " 'open', '2026-01-12 10:00:00', NULL)"
            )
        )
        conn.commit()

        assert migrate_legacy_notes(conn) == 2

        deals = conn.execute(
            sa.text(
                "SELECT username, item_id, upgrade, amount, buy_price, sell_price, status,"
                " note, closed_at FROM deals ORDER BY id"
            )
        ).all()
        # user_id 7 resolved to username via legacy_users; 9 has no user -> fallback
        assert deals[0][:3] == ("oleg", "p6qz2", 15)
        assert deals[0][3:7] == (2, 1000.0, 1400.0, "closed")
        assert deals[0][7] == "Черный Регалий"
        assert deals[0][8] is not None  # closed_at from sold_at
        assert deals[1][:3] == ("user_9", "w4jo", 0)
        assert deals[1][5] is None and deals[1][6] == "open" and deals[1][8] is None
        tables = set(sa.inspect(conn).get_table_names())
        assert "notes" not in tables
        assert "legacy_notes" in tables
    engine.dispose()


def test_migrate_legacy_notes_unexpected_schema_skipped() -> None:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        DealRow.__table__.create(conn)
        conn.execute(sa.text("CREATE TABLE notes (id INTEGER PRIMARY KEY, payload TEXT)"))
        conn.commit()
        assert migrate_legacy_notes(conn) == 0
        assert "notes" in sa.inspect(conn).get_table_names()
    engine.dispose()


def test_full_upgrade_on_legacy_database(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: old v2 database (revision 005) upgrades cleanly to v3.

    Mirrors the real production schema: notes is the v2 trade journal.
    """
    db_path = tmp_path / "legacy.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(sa.text("INSERT INTO alembic_version (version_num) VALUES ('005')"))
        conn.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64))"))
        conn.execute(sa.text("INSERT INTO users (id, username) VALUES (7, 'oleg')"))
        conn.execute(
            sa.text(
                "CREATE TABLE auction_history (id INTEGER PRIMARY KEY, item_id VARCHAR(16),"
                " price FLOAT)"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TABLE notes (id INTEGER PRIMARY KEY, user_id INTEGER,"
                " item_id VARCHAR(16), item_name VARCHAR(128), rarity INTEGER, level INTEGER,"
                " quantity INTEGER, buy_price FLOAT, sell_price FLOAT, actual_profit FLOAT,"
                " status VARCHAR(16), created_at DATETIME, sold_at DATETIME)"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO notes (user_id, item_id, item_name, rarity, level, quantity,"
                " buy_price, sell_price, actual_profit, status, created_at, sold_at)"
                " VALUES (7, 'p6qz2', 'Черный Регалий', 0, 15, 2, 1000.0, 1400.0, 330.0,"
                " 'sold', '2026-01-10 12:00:00', '2026-01-11 09:30:00')"
            )
        )
        conn.commit()
    engine.dispose()

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    raw = sqlite3.connect(db_path)
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "deals" in tables
    assert "users" in tables
    assert "auction_history" in tables
    assert "legacy_users" in tables
    assert "legacy_notes" in tables
    assert "notes" not in tables
    deals = raw.execute("SELECT username, item_id, status, note FROM deals").fetchall()
    assert deals == [("oleg", "p6qz2", "closed", "Черный Регалий")]
    version = raw.execute("SELECT version_num FROM alembic_version").fetchall()
    # FIX (3.1.1): репозиторий на 006_history_effective, тест отставал на 004
    assert version == [("006_history_effective",)]
    raw.close()
