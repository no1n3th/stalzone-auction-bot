"""AUDIT B1: migration parity — `alembic upgrade head` must produce exactly
the schema declared by Base.metadata (no drift via create_all shortcuts)."""

from __future__ import annotations

import sys
from pathlib import Path

import sqlalchemy as sa

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402


def _alembic_cfg(db_url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.attributes["configure_logger"] = False
    return cfg


def _norm_tables(meta) -> dict:
    def _t(col) -> str:
        # SQLite reflection collapses BigInteger -> INTEGER and drops
        # timezone info on DateTime; compare by affinity, not exact type.
        base = str(col.type).split("(")[0].upper()
        return "INTEGER" if base == "BIGINT" else base

    out = {}
    for table in sorted(meta.sorted_tables, key=lambda t: t.name):
        if table.name == "alembic_version":
            continue
        cols = {c.name: (_t(c), c.nullable, c.primary_key) for c in table.columns}
        idx = {
            (ix.name, tuple(c.name for c in ix.columns), bool(ix.unique)) for ix in table.indexes
        }
        uq = {
            tuple(c.name for c in u.columns)
            for u in table.constraints
            if isinstance(u, sa.UniqueConstraint)
        }
        out[table.name] = (cols, idx, uq)
    return out


def test_upgrade_head_matches_metadata(tmp_path):
    from database.orm import Base

    db_file = tmp_path / "parity.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    command.upgrade(_alembic_cfg(url), "head")

    engine = sa.create_engine(url.replace("+aiosqlite", ""))
    migrated = sa.MetaData()
    migrated.reflect(bind=engine)
    engine.dispose()

    migrated_norm = _norm_tables(migrated)
    orm_norm = _norm_tables(Base.metadata)

    assert migrated_norm.keys() == orm_norm.keys(), (
        f"table sets differ: only-in-migrations={set(migrated_norm) - set(orm_norm)}, "
        f"only-in-orm={set(orm_norm) - set(migrated_norm)}"
    )
    diffs = []
    for name in orm_norm:
        if migrated_norm[name] != orm_norm[name]:
            diffs.append(name)
    assert not diffs, f"schema drift in tables: {diffs}"
