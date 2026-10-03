"""auction_history: real sale time (sold_at) + dedup unique index

Revision ID: 003_history_sold_at
Revises: 002_sessions
Create Date: 2026-09-29

AUDIT D1: history rows were dated by download time and re-inserted on every
sync. Existing rows keep sold_at=NULL; new rows carry the API `time` value and
the unique index makes re-syncs idempotent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003_history_sold_at"
down_revision: str | None = "002_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEDUP_COLS = ["item_id", "quality", "upgrade", "sold_at", "price", "amount"]


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("auction_history")}
    if "sold_at" not in cols:
        with op.batch_alter_table("auction_history") as batch:
            batch.add_column(sa.Column("sold_at", sa.DateTime(timezone=True), nullable=True))
    idx = {ix["name"] for ix in sa.inspect(bind).get_indexes("auction_history")}
    if "ux_history_dedup" not in idx:
        op.create_index("ux_history_dedup", "auction_history", _DEDUP_COLS, unique=True)


def downgrade() -> None:
    op.drop_index("ux_history_dedup", table_name="auction_history")
