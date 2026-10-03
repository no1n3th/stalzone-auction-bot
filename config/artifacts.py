"""Artifacts machine config: pydantic-validated, hot-reloadable (mtime polling)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator

_CATEGORY_DEFAULT: Literal["liquid", "illiquid"] = "illiquid"


class ArtifactConfig(BaseModel, frozen=True):
    item_id: str
    name: str
    category: Literal["liquid", "illiquid"] = _CATEGORY_DEFAULT
    min_quality: int | None = None  # None -> любая редкость (напр. Регалий)
    upgrades: tuple[int, ...] = (0, 15)
    profit_threshold_pct: float | None = None

    @field_validator("item_id")
    @classmethod
    def _id_lower(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("item_id must not be empty")
        return v

    @field_validator("min_quality")
    @classmethod
    def _quality_range(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 6):
            raise ValueError("min_quality must be 0..6")
        return v

    @field_validator("upgrades", mode="before")
    @classmethod
    def _upgrades_tuple(cls, v: object) -> object:
        if isinstance(v, list):
            return tuple(v)
        return v

    @field_validator("upgrades")
    @classmethod
    def _upgrades_range(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        for lvl in v:
            if not (0 <= lvl <= 15):
                raise ValueError("upgrade level must be 0..15")
        return v

    @field_validator("profit_threshold_pct")
    @classmethod
    def _threshold_positive(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("profit_threshold_pct must be > 0")
        return v

    @property
    def effective_min_quality(self) -> int:
        return self.min_quality if self.min_quality is not None else 0


class ArtifactsFile(BaseModel):
    artifacts: list[ArtifactConfig]

    @property
    def liquid(self) -> list[ArtifactConfig]:
        return [a for a in self.artifacts if a.category == "liquid"]

    @property
    def illiquid(self) -> list[ArtifactConfig]:
        return [a for a in self.artifacts if a.category == "illiquid"]


class ArtifactRegistry:
    """Loads artifacts.yaml once and hot-reloads it when mtime changes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float = -1.0
        self._by_id: dict[str, ArtifactConfig] = {}
        if not path.exists():
            raise OSError(f"artifacts config not found: {path}")
        self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if not force and mtime == self._mtime:
            return False
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        parsed = ArtifactsFile.model_validate(data)
        by_id: dict[str, ArtifactConfig] = {}
        for art in parsed.artifacts:
            if art.item_id in by_id:
                raise ValueError(f"duplicate artifact in config: {art.item_id}")
            by_id[art.item_id] = art
        self._by_id = by_id
        self._mtime = mtime
        return True

    def maybe_reload(self) -> bool:
        return self.reload(force=False)

    def get(self, item_id: str) -> ArtifactConfig | None:
        return self._by_id.get(item_id.lower())

    def all(self) -> list[ArtifactConfig]:
        return list(self._by_id.values())

    def ids(self) -> list[str]:
        return list(self._by_id)

    @property
    def liquid(self) -> list[ArtifactConfig]:
        return [a for a in self._by_id.values() if a.category == "liquid"]

    @property
    def illiquid(self) -> list[ArtifactConfig]:
        return [a for a in self._by_id.values() if a.category == "illiquid"]

    def __len__(self) -> int:
        return len(self._by_id)
