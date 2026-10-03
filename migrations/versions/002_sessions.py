"""sessions: persist web sessions (token hash -> username, expires_at)

Revision ID: 002_sessions
Revises: 001_baseline
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from database.orm import SessionRow

revision: str = "002_sessions"
down_revision: str | None = "001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # checkfirst: the baseline's create_all already makes the table on fresh installs
    SessionRow.__table__.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    SessionRow.__table__.drop(bind, checkfirst=True)
