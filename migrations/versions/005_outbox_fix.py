"""outbox: fix 004 NOT NULL updated_at drift; add next_attempt_at/priority/event_type.

Revision ID: 005_outbox_fix
Revises: 004_outbox
Create Date: 2026-09-30

P0-2.8: 004 created `updated_at NOT NULL` without a default while the ORM
(TimestampMixin) does not populate it — any ORM INSERT on an alembic-managed
DB failed, and the persist hook swallowed the error as a warning. Bring the
schema in line with the ORM and add the replay-scheduling columns.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005_outbox_fix"
down_revision: str | None = "004_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("outbox")}
    if "next_attempt_at" not in cols:
        op.add_column(
            "outbox", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
        )
    if "event_type" not in cols:
        op.add_column("outbox", sa.Column("event_type", sa.String(32), server_default=""))
    if "priority" not in cols:
        op.add_column("outbox", sa.Column("priority", sa.Integer, server_default="50"))
    # sqlite-safe batch mode for the nullability change
    with op.batch_alter_table("outbox") as b:
        b.alter_column(
            "updated_at", existing_type=sa.DateTime(timezone=True), nullable=True
        )


def downgrade() -> None:
    with op.batch_alter_table("outbox") as b:
        b.alter_column(
            "updated_at", existing_type=sa.DateTime(timezone=True), nullable=False
        )
    op.drop_column("outbox", "priority")
    op.drop_column("outbox", "event_type")
    op.drop_column("outbox", "next_attempt_at")
