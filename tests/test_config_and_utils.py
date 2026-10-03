"""Settings validation + small utilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import Settings, reveal_secret
from config.artifacts import ArtifactRegistry
from config.seasons import SeasonRegistry


def test_settings_defaults():
    s = Settings(database_url="sqlite+aiosqlite:///:memory:")
    assert s.fee == pytest.approx(0.05)
    assert s.region == "RU"
    assert s.profit_threshold_pct == s.default_profit_threshold_pct


def test_region_validation():
    with pytest.raises(Exception):
        Settings(database_url="sqlite+aiosqlite:///:memory:", region="XX")


def test_liquid_earnings_json_parsing():
    s = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        liquid_earnings_by_quality_json='{"0": 11, "5": 25}',
    )
    assert s.liquid_earnings_by_quality == {0: 11.0, 5: 25.0}


def test_liquid_earnings_json_broken_falls_back():
    # AUDIT Q3: broken JSON must not silently change pricing — warn + defaults
    s = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        liquid_earnings_by_quality_json="{oops",
    )
    assert s.liquid_earnings_by_quality[0] == 10.0


def test_reveal_secret_plain_and_wrapped():
    assert reveal_secret("plain") == "plain"
    from pydantic import SecretStr

    assert reveal_secret(SecretStr("hidden")) == "hidden"


def test_discord_webhooks_property():
    s = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        discord_webhook_url="http://h1",
        discord_extra_webhooks_json='["http://h2", ""]',
    )
    assert s.discord_webhooks == ["http://h1", "http://h2"]


def test_discord_webhooks_invalid_json_warns(caplog):
    # P1-13: invalid JSON must not vanish silently
    s = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        discord_webhook_url="",  # герметичность от .env
        discord_extra_webhooks_json="{bad json",
    )
    assert s.discord_webhooks == []
    assert any("DISCORD_EXTRA_WEBHOOKS_JSON" in r.message for r in caplog.records)


def test_threshold_mode_literal():
    with pytest.raises(Exception):
        Settings(database_url="sqlite+aiosqlite:///:memory:", threshold_mode="sideways")


def test_registration_mode_literal():
    with pytest.raises(Exception):
        Settings(database_url="sqlite+aiosqlite:///:memory:", registration_mode="nope")


class TestArtifactRegistry:
    def test_loads_real_config(self):
        reg = ArtifactRegistry(Path("config/artifacts.yaml"))
        assert len(reg) == 104
        art = reg.get("y5vw")
        assert art is not None and art.name == "Скорлупа"

    def test_case_insensitive(self):
        reg = ArtifactRegistry(Path("config/artifacts.yaml"))
        assert reg.get("Y5VW") is not None

    def test_missing_file(self, tmp_path):
        from pydantic import ValidationError

        with pytest.raises((OSError, ValidationError)):
            ArtifactRegistry(tmp_path / "nope.yaml")

    def test_hot_reload_no_change(self, tmp_path):
        import shutil

        shutil.copy("config/artifacts.yaml", tmp_path / "a.yaml")
        reg = ArtifactRegistry(tmp_path / "a.yaml")
        assert reg.maybe_reload() is False  # same mtime

    def test_hot_reload_picks_up_change(self, tmp_path):
        import shutil
        import time

        shutil.copy("config/artifacts.yaml", tmp_path / "a.yaml")
        reg = ArtifactRegistry(tmp_path / "a.yaml")
        time.sleep(1.1)  # гранулярность mtime — иначе maybe_reload не увидит изменение
        with open(tmp_path / "a.yaml", "a", encoding="utf-8") as fh:
            fh.write("\n# touched\n")
        assert reg.maybe_reload() is True
        assert len(reg) == 104


class TestSeasonRegistry:
    def test_loads(self):
        reg = SeasonRegistry(Path("config/seasons.yaml"))
        assert len(reg.all()) == 4
