"""baseline: create all tables + migrate legacy notes into deals

Revision ID: 001_baseline
Revises:
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from database.legacy import migrate_legacy_notes
from database.orm import Base

revision: str = "001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)
    # legacy notes -> deals migration (defensive about the old schema)
    migrate_legacy_notes(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(Base.metadata.sorted_tables):
        table.drop(bind, checkfirst=True)
