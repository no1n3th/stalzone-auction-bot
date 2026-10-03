"""Seasons config: MM-DD ranges (New-Year wrap safe, leap safe), hot reload."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator


def _parse_mm_dd(value: str) -> tuple[int, int]:
    try:
        month_s, day_s = value.split("-")
        month, day = int(month_s), int(day_s)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"invalid MM-DD date: {value!r}") from exc
    if not (1 <= month <= 12 and 1 <= day <= 31):
        raise ValueError(f"invalid MM-DD date: {value!r}")
    return month, day


def _safe_date(year: int, month: int, day: int) -> date:
    """29.02 clamps to 28.02 on non-leap years."""
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1


class SeasonConfig(BaseModel, frozen=True):
    name: str
    start: str  # MM-DD
    end: str  # MM-DD
    active: bool = True
    item_ids: tuple[str, ...] = ()

    @field_validator("start", "end")
    @classmethod
    def _validate_mm_dd(cls, v: str) -> str:
        _parse_mm_dd(v)
        return v

    @field_validator("item_ids", mode="before")
    @classmethod
    def _ids_tuple(cls, v: object) -> object:
        if isinstance(v, list):
            return tuple(str(i).lower() for i in v)
        return v

    def _dates(self, year: int) -> tuple[date, date]:
        sm, sd = _parse_mm_dd(self.start)
        em, ed = _parse_mm_dd(self.end)
        start_d = _safe_date(year, sm, sd)
        end_d = _safe_date(year, em, ed)
        if end_d < start_d:  # wraps around New Year
            end_d = _safe_date(year + 1, em, ed)
        return start_d, end_d

    def contains(self, day: date) -> bool:
        start_d, end_d = self._dates(day.year)
        if not (start_d <= day <= end_d):
            # maybe we are in the wrapped tail of last year's season
            start_prev, end_prev = self._dates(day.year - 1)
            return start_prev <= day <= end_prev
        return True

    def current_start(self, day: date) -> date:
        start_d, _ = self._dates(day.year)
        if day < start_d and self.contains(day):
            return self._dates(day.year - 1)[0]
        return start_d

    def current_end(self, day: date) -> date:
        _, end_d = self._dates(day.year)
        if day > end_d and self.contains(day):
            return self._dates(day.year + 1)[1]
        return end_d


class SeasonsFile(BaseModel):
    seasons: list[SeasonConfig] = []
    custom_events: list[SeasonConfig] = []


class SeasonRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float = -1.0
        self._seasons: list[SeasonConfig] = []
        self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if force:
                self._seasons = []
            return False
        if not force and mtime == self._mtime:
            return False
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        parsed = SeasonsFile.model_validate(data)
        self._seasons = [s for s in (*parsed.seasons, *parsed.custom_events) if s.active]
        self._mtime = mtime
        return True

    def maybe_reload(self) -> bool:
        return self.reload(force=False)

    def active_at(self, day: date) -> list[SeasonConfig]:
        return [s for s in self._seasons if s.contains(day)]

    def seasons_for(self, item_id: str, day: date) -> list[SeasonConfig]:
        item_id = item_id.lower()
        return [s for s in self.active_at(day) if item_id in s.item_ids]

    def seasonal_item_ids(self, day: date) -> set[str]:
        out: set[str] = set()
        for s in self.active_at(day):
            out.update(s.item_ids)
        return out

    def upcoming(self, day: date, within_days: int) -> list[SeasonConfig]:
        """Seasons whose start is within `within_days` ahead (and not active now)."""
        result: list[SeasonConfig] = []
        for s in self._seasons:
            if s.contains(day):
                continue
            start_d = s._dates(day.year)[0]
            if start_d < day:
                start_d = s._dates(day.year + 1)[0]
            if 0 <= (start_d - day).days <= within_days:
                result.append(s)
        return result

    def all(self) -> list[SeasonConfig]:
        return list(self._seasons)

    def get(self, name: str) -> SeasonConfig | None:
        for s in self._seasons:
            if s.name == name:
                return s
        return None
