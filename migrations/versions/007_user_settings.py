"""user_settings: per-user UI preferences (sound, theme).

Revision ID: 007_user_settings
Revises: 006_history_effective
Create Date: 2026-10-03
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from database.orm import UserSettingsRow

revision: str = "007_user_settings"
down_revision: str | None = "006_history_effective"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    UserSettingsRow.__table__.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    UserSettingsRow.__table__.drop(bind, checkfirst=True)
