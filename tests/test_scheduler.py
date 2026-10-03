"""Weighted scheduler: intervals, deferral, budget-aware loop."""

from __future__ import annotations

import asyncio
from pathlib import Path

from config.artifacts import ArtifactRegistry
from scanner.scheduler import WeightedScheduler, scheduler_loop

REG_PATH = Path("config/artifacts.yaml")


def make_reg() -> ArtifactRegistry:
    return ArtifactRegistry(REG_PATH)


def make_sched(base=60.0) -> WeightedScheduler:
    return WeightedScheduler(make_reg(), base_interval=base)


class TestIntervals:
    def test_liquid_base_interval(self):
        sched = make_sched(60.0)
        art = make_reg().get("rn1z")  # liquid
        assert sched.interval_for(art) == 60.0

    def test_illiquid_explicit_threshold(self):
        sched = make_sched(60.0)
        art = make_reg().get("y5vw")  # illiquid, threshold 200%
        # 60 * 10 * (200/30) = 4000
        assert sched.interval_for(art) == 4000.0

    def test_illiquid_default_weight(self):
        sched = make_sched(60.0)
        art = make_reg().get("9nvy")  # illiquid, no explicit threshold
        # 60 * 10 * 6.67 = 4002
        assert sched.interval_for(art) == 4002.0

    def test_defer_replaces_entry(self):
        """AUDIT D4: one heap entry per item — repeated defers must not stack."""
        sched = make_sched(60.0)
        art = make_reg().get("rn1z")
        before = len(sched._heap)
        sched.defer(art.item_id, delay=5.0)
        sched.defer(art.item_id, delay=10.0)
        sched.defer(art.item_id, delay=15.0)
        after = len(sched._heap)
        assert after == before  # still one entry for the item
        assert len([it for it in sched._heap if it.item_id == art.item_id]) == 1


class TestDueIds:
    def test_cold_start_jitter(self):
        sched = make_sched(100.0)
        # AUDIT D4: first scans are spread out, not all at t=0
        next_due = sched.next_due_in()
        assert 15.0 < next_due <= 101.0  # 0.15..1.0 * 100

    def test_due_after_time(self):
        sched = make_sched(0.0)  # everything due immediately
        ids = sched.due_ids(limit=3)
        assert len(ids) == 3
        ids2 = sched.due_ids(limit=3)
        assert len(ids2) == 3

    def test_rebuild_preserves_due(self):
        """AUDIT D4: rebuild (config reload) must not reset timers."""
        sched = make_sched(100.0)
        art = make_reg().get("rn1z")
        sched.defer(art.item_id, delay=50.0)
        due_before = next(it.due_at for it in sched._heap if it.item_id == art.item_id)
        sched.rebuild()
        due_after = next(it.due_at for it in sched._heap if it.item_id == art.item_id)
        assert due_after == due_before


class TestLoop:
    async def test_scans_due_items(self):
        sched = make_sched(0.0)
        scanned: list[str] = []

        async def scan(item_id: str) -> None:
            scanned.append(item_id)

        async def acquire() -> bool:
            return True

        stop = asyncio.Event()
        task = asyncio.create_task(scheduler_loop(sched, acquire, scan, batch_size=2, stop_event=stop))
        await asyncio.sleep(0.3)
        stop.set()
        await task
        assert len(scanned) >= 2

    async def test_defers_when_no_budget(self):
        """Without budget, items are deferred — never scanned."""
        sched = make_sched(0.0)
        scanned: list[str] = []

        async def scan(item_id: str) -> None:
            scanned.append(item_id)

        async def acquire() -> bool:
            return False  # no budget

        stop = asyncio.Event()
        task = asyncio.create_task(
            scheduler_loop(sched, acquire, scan, batch_size=2, stop_event=stop, concurrency=1)
        )
        await asyncio.sleep(0.2)
        stop.set()
        await task
        assert scanned == []
        assert sched.deferred_count > 0

    async def test_recovery_after_budget_restored(self):
        """AUDIT D4: after budget returns, the deferred item is scanned again."""
        sched = make_sched(0.0)
        scanned: list[str] = []
        budget = {"ok": False}

        async def scan(item_id: str) -> None:
            scanned.append(item_id)

        async def acquire() -> bool:
            return budget["ok"]

        stop = asyncio.Event()
        task = asyncio.create_task(
            scheduler_loop(sched, acquire, scan, batch_size=1, stop_event=stop, concurrency=1)
        )
        await asyncio.sleep(0.2)
        assert not scanned
        budget["ok"] = True
        await asyncio.sleep(0.3)
        stop.set()
        await task
        assert scanned
