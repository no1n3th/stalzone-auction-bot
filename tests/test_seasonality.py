"""Seasonality: activation, wrap/leap dates, window, weight, dip buy, transitions."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from analytics.seasonality import SeasonEngine
from config.seasons import SeasonConfig, SeasonRegistry
from models.events import EventType


class FakeStore:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value):
        self.data[key] = value

    async def delete(self, key):
        self.data.pop(key, None)

    async def keys_with_prefix(self, prefix):
        return {k for k in self.data if k.startswith(prefix)}


class FakePreseasonProvider:
    def __init__(self, avg):
        self._avg = avg

    async def avg_before(self, item_id, quality, upgrade, hours_back, season_hours):
        return self._avg


class FakeRegistry:
    def __init__(self, seasons):
        self._s = seasons

    def seasons_for(self, item_id, day):
        return [s for s in self._s if item_id in s.item_ids and s.contains(day)]

    def active_at(self, day):
        return [s for s in self._s if s.contains(day)]

    def seasonal_item_ids(self, day):
        out = set()
        for s in self.active_at(day):
            out.update(s.item_ids)
        return out

    def upcoming(self, day, within_days):
        return []

    def all(self):
        return list(self._s)


WINTER = SeasonConfig(
    name="winter", start="12-01", end="02-28", active=True, item_ids=("opal", "iney")
)


class TestSeasonDates:
    def test_wrap_around_new_year(self):
        assert WINTER.contains(date(2026, 1, 15))
        assert WINTER.contains(date(2026, 12, 15))
        assert not WINTER.contains(date(2026, 6, 15))

    def test_leap_clamp(self):
        season = SeasonConfig(name="s", start="02-28", end="02-29", item_ids=())
        assert season.contains(date(2025, 2, 28))  # non-leap year clamps 29 -> 28

    def test_mm_dd_validation(self):
        try:
            SeasonConfig(name="bad", start="13-01", end="02-01")
            raise AssertionError("should have raised")
        except Exception:
            pass


class TestSeasonEngine:
    def setup_method(self):
        self.reg = FakeRegistry([WINTER])
        self.engine = SeasonEngine(
            self.reg,
            prewarn_days=7,
            seasonal_window_days=14,
            dip_buy_pct=30.0,
            fast_adapt_weight=3.0,
            default_window_days=30,
        )

    def test_activation(self):
        assert self.engine.is_seasonal("opal", date(2026, 1, 10))
        assert not self.engine.is_seasonal("opal", date(2026, 7, 10))
        assert not self.engine.is_seasonal("other", date(2026, 1, 10))

    def test_seasonal_window(self):
        assert self.engine.history_window_days("opal", date(2026, 1, 10)) == 14
        assert self.engine.history_window_days("opal", date(2026, 7, 10)) == 30

    def test_fast_adapt_weight(self):
        assert self.engine.adapt_weight("opal", date(2026, 1, 10)) == 3.0
        assert self.engine.adapt_weight("opal", date(2026, 7, 10)) == 1.0

    async def test_dip_buy(self):
        provider = FakePreseasonProvider(avg=1000.0)
        day = date(2026, 1, 10)
        event = await self.engine.check_dip_buy(provider, "opal", 4, 0, 650.0, day=day)
        assert event is not None
        assert event.event_type == EventType.SEASON_DIP_BUY
        no_event = await self.engine.check_dip_buy(provider, "opal", 4, 0, 750.0, day=day)
        assert no_event is None

    async def test_dip_buy_not_seasonal(self):
        provider = FakePreseasonProvider(avg=1000.0)
        assert (
            await self.engine.check_dip_buy(provider, "opal", 4, 0, 100.0, day=date(2026, 7, 1))
            is None
        )

    async def test_dip_buy_no_preseason_avg(self):
        provider = FakePreseasonProvider(avg=None)
        assert (
            await self.engine.check_dip_buy(provider, "opal", 4, 0, 100.0, day=date(2026, 1, 10))
            is None
        )

    def test_seasonal_drop_event(self):
        event = self.engine.seasonal_drop_event("opal", 4, 0, 25.0, day=date(2026, 1, 10))
        assert event is not None and event.event_type == EventType.SEASONAL_DROP
        assert self.engine.seasonal_drop_event("opal", 4, 0, 25.0, day=date(2026, 7, 1)) is None


class TestTransitions:
    async def test_season_start_and_end(self):
        reg = SeasonRegistry.__new__(SeasonRegistry)  # bypass file loading
        reg._seasons = [WINTER]
        engine = SeasonEngine(reg, prewarn_days=7)
        store = FakeStore()

        # season start
        events = await engine.check_transitions(store, day=date(2026, 1, 10))
        assert any(e.event_type == EventType.SEASON_START for e in events)
        # not fired twice
        events = await engine.check_transitions(store, day=date(2026, 1, 11))
        assert not any(e.event_type == EventType.SEASON_START for e in events)

        # season end
        events = await engine.check_transitions(store, day=date(2026, 3, 5))
        assert any(e.event_type == EventType.SEASON_END for e in events)

    async def test_prewarn(self):
        class RegWithUpcoming(FakeRegistry):
            def upcoming(self, day, within_days):
                return [WINTER] if day == date(2026, 11, 25) else []

            def active_at(self, day):
                return []

            def all(self):
                return [WINTER]

        engine = SeasonEngine(RegWithUpcoming([WINTER]), prewarn_days=7)
        store = FakeStore()
        events = await engine.check_transitions(store, day=date(2026, 11, 25))
        assert any(e.event_type == EventType.SEASON_PREWARN for e in events)
        # once only
        events = await engine.check_transitions(store, day=date(2026, 11, 25))
        assert not any(e.event_type == EventType.SEASON_PREWARN for e in events)


def test_leap_day_falls_in_winter():
    """AUDIT Q9: 29 Feb must be covered (winter end extended to 02-29)."""
    from datetime import date

    from config.seasons import SeasonRegistry

    reg = SeasonRegistry(Path("config/seasons.yaml"))
    active = reg.active_at(date(2024, 2, 29))
    assert any(s.name == "winter" for s in active)
