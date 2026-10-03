"""history: backfill sold_at surrogate + index (item, quality, upgrade, sold_at).

Revision ID: 006_history_effective
Revises: 005_outbox_fix
Create Date: 2026-09-30

P0-4: all time windows now run on coalesce(sold_at, created_at). Legacy rows
without a sale timestamp get a surrogate (download time — real sale times are
unrecoverable; recorded in DECISIONS.md), so coalesce stops leaning on the
fallback for every old row, and the new index serves the per-group windows.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "006_history_effective"
down_revision: str | None = "005_outbox_fix"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE auction_history SET sold_at = created_at WHERE sold_at IS NULL")
    op.create_index(
        "ix_history_item_q_u_sold",
        "auction_history",
        ["item_id", "quality", "upgrade", "sold_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_history_item_q_u_sold", table_name="auction_history")
