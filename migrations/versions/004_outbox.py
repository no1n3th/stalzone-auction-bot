"""outbox: durable Discord notification log (at-least-once delivery)

Revision ID: 004_outbox
Revises: 003_history_sold_at
Create Date: 2026-09-29

AUDIT R4-outbox: the Discord queue was memory-only — a restart dropped every
pending event. The outbox table survives restarts; a sweep worker re-enqueues
unsent rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "004_outbox"
down_revision: str | None = "003_history_sold_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if "outbox" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "outbox",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("outbox")
