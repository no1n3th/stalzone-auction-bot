"""Meta rules: dump, meta-exit, stabilization, meta-enter, seasonal suppression."""

from __future__ import annotations

from analytics.meta import (
    MetaThresholds,
    check_dump,
    check_meta_enter,
    check_meta_exit,
    check_stabilization,
    evaluate_group,
    is_falling,
)
from models.events import EventType


class FakeProvider:
    def __init__(self):
        self.avgs: dict[tuple, float | None] = {}
        self.counts: dict[tuple, int] = {}
        self.spreads: dict[tuple, float | None] = {}
        self.cheap: int = 0

    async def window_avg(self, item_id, quality, upgrade, hours, offset_hours=0):
        return self.avgs.get((hours, offset_hours))

    async def window_count(self, item_id, quality, upgrade, hours, offset_hours=0):
        return self.counts.get((hours, offset_hours), 0)

    async def spread_pct(self, item_id, quality, upgrade, hours, since=None):  # P1-7 (D12)
        return self.spreads.get(hours)

    def cheap_lots_count(self, item_id, quality, upgrade, avg, below_pct):
        return self.cheap


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


T = MetaThresholds()


class TestIsFalling:
    def test_basic(self):
        assert is_falling(70.0, 100.0, 20.0)
        assert not is_falling(85.0, 100.0, 20.0)
        assert not is_falling(None, 100.0, 20.0)
        assert not is_falling(50.0, 0.0, 20.0)


class TestDump:
    async def test_dump_triggered(self):
        p = FakeProvider()
        p.avgs = {(24, 0): 70.0, (24, 24): 100.0}
        event = await check_dump(p, FakeStore(), T, "item", 4, 0)
        assert event is not None
        assert event.event_type == EventType.DUMP
        assert event.fields["drop_pct"] == 30.0

    async def test_no_dump(self):
        p = FakeProvider()
        p.avgs = {(24, 0): 95.0, (24, 24): 100.0}
        assert await check_dump(p, FakeStore(), T, "item", 4, 0) is None

    async def test_dump_missing_data(self):
        assert await check_dump(FakeProvider(), FakeStore(), T, "item", 4, 0) is None


class TestMetaExit:
    async def test_exit_triggered_and_invalidated(self):
        p = FakeProvider()
        p.cheap = 12  # >= 10 cheap lots
        p.avgs = {(168, 0): 1000.0, (24, 0): 800.0, (24, 24): 900.0}  # avg falling
        store = FakeStore()
        event = await check_meta_exit(p, store, T, "item", 4, 0)
        assert event is not None
        assert event.event_type == EventType.META_EXIT
        assert await store.get("invalid:item:4:0") is not None

    async def test_no_exit_when_few_lots(self):
        p = FakeProvider()
        p.cheap = 3
        p.avgs = {(168, 0): 1000.0, (24, 0): 800.0, (24, 24): 900.0}
        assert await check_meta_exit(p, FakeStore(), T, "item", 4, 0) is None

    async def test_no_exit_when_avg_rising(self):
        p = FakeProvider()
        p.cheap = 20
        p.avgs = {(168, 0): 1000.0, (24, 0): 950.0, (24, 24): 900.0}  # rising
        assert await check_meta_exit(p, FakeStore(), T, "item", 4, 0) is None

    async def test_already_invalidated_skipped(self):
        store = FakeStore()
        store.data["invalid:item:4:0"] = "1000"
        p = FakeProvider()
        p.cheap = 50
        assert await check_meta_exit(p, store, T, "item", 4, 0) is None


class TestStabilization:
    async def test_revalidate_after_stable(self):
        store = FakeStore()
        store.data["invalid:item:4:0"] = "1000"
        p = FakeProvider()
        p.spreads = {48: 10.0}  # < 15% spread
        assert await check_stabilization(p, store, T, "item", 4, 0)
        assert await store.get("invalid:item:4:0") is None

    async def test_still_unstable(self):
        store = FakeStore()
        store.data["invalid:item:4:0"] = "1000"
        p = FakeProvider()
        p.spreads = {48: 40.0}
        assert not await check_stabilization(p, store, T, "item", 4, 0)
        assert await store.get("invalid:item:4:0") is not None


class TestMetaEnter:
    async def test_enter_triggered(self):
        p = FakeProvider()
        p.counts = {(6, 0): 15, (6, 6): 10}  # +50% surge
        event = await check_meta_enter(p, FakeStore(), T, "item", 4, 0)
        assert event is not None
        assert event.event_type == EventType.META_ENTER

    async def test_no_enter(self):
        p = FakeProvider()
        p.counts = {(6, 0): 10, (6, 6): 10}
        assert await check_meta_enter(p, FakeStore(), T, "item", 4, 0) is None

    async def test_enter_needs_previous(self):
        p = FakeProvider()
        p.counts = {(6, 0): 10, (6, 6): 0}
        assert await check_meta_enter(p, FakeStore(), T, "item", 4, 0) is None


class TestEvaluateGroupSuppression:
    async def test_suppress_meta_hides_dump_exit(self):
        p = FakeProvider()
        p.avgs = {(24, 0): 70.0, (24, 24): 100.0, (168, 0): 1000.0}
        p.cheap = 50
        events = await evaluate_group(p, FakeStore(), T, "item", 4, 0, suppress_meta=True)
        assert all(e.event_type not in (EventType.DUMP, EventType.META_EXIT) for e in events)

    async def test_normal_evaluation(self):
        p = FakeProvider()
        p.avgs = {(24, 0): 70.0, (24, 24): 100.0, (168, 0): 1000.0}
        p.cheap = 50
        events = await evaluate_group(p, FakeStore(), T, "item", 4, 0, suppress_meta=False)
        types = {e.event_type for e in events}
        assert EventType.DUMP in types


class TestAlertDedup:
    """AUDIT R8: repeated alerts are suppressed until the metric worsens."""

    async def test_dump_alert_deduped_until_drop_worsens(self):
        p = FakeProvider()
        p.avgs = {(24, 0): 70.0, (24, 24): 100.0}  # drop 30%
        store = FakeStore()
        assert await check_dump(p, store, T, "item", 4, 0) is not None
        assert await check_dump(p, store, T, "item", 4, 0) is None  # dedup
        p.avgs[(24, 0)] = 64.0  # drop 36% > 30 + 5 margin -> re-alert
        assert await check_dump(p, store, T, "item", 4, 0) is not None

    async def test_dump_state_cleared_when_condition_ends(self):
        p = FakeProvider()
        p.avgs = {(24, 0): 70.0, (24, 24): 100.0}
        store = FakeStore()
        assert await check_dump(p, store, T, "item", 4, 0) is not None
        p.avgs[(24, 0)] = 95.0  # dump over
        assert await check_dump(p, store, T, "item", 4, 0) is None
        p.avgs[(24, 0)] = 70.0  # dump again -> alert fires again
        assert await check_dump(p, store, T, "item", 4, 0) is not None

    async def test_meta_enter_deduped_until_surge_grows(self):
        p = FakeProvider()
        p.counts = {(6, 0): 150, (6, 6): 100}  # surge 50%
        store = FakeStore()
        assert await check_meta_enter(p, store, T, "item", 4, 0) is not None
        assert await check_meta_enter(p, store, T, "item", 4, 0) is None  # dedup
        p.counts[(6, 0)] = 165  # surge 65% > 50 + 10 margin -> re-alert
        assert await check_meta_enter(p, store, T, "item", 4, 0) is not None
