"""User settings repository (JSON blob per user)."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from database.orm import UserSettingsRow

DEFAULTS: dict[str, Any] = {
    "sound_enabled": True,
    "sound_volume": 0.5,
    "sound_events": ["lot", "mid_lot"],   # какие типы событий со звуком
    "profit_threshold_pct": 10.0,          # звук только если профит >= этого %
    "theme": "dark",                       # dark | light
}


class UserSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, username: str) -> dict[str, Any]:
        row = await self.session.get(UserSettingsRow, username)
        if row is None:
            return dict(DEFAULTS)
        try:
            stored = json.loads(row.data)
        except (TypeError, ValueError):
            stored = {}
        # merge with defaults so new keys appear for existing users
        return {**DEFAULTS, **stored}

    async def set(self, username: str, data: dict[str, Any]) -> None:
        merged = {**DEFAULTS, **data}
        row = await self.session.get(UserSettingsRow, username)
        payload = json.dumps(merged, ensure_ascii=False)
        if row is None:
            self.session.add(UserSettingsRow(username=username, data=payload))
        else:
            row.data = payload
        await self.session.flush()
