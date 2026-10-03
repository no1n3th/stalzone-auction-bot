"""Meta rules engine: dump / meta-exit / meta-enter. I/O-free via Protocols."""

from __future__ import annotations

from collections.abc import Set as ABSet
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from models.events import EventType, NotificationEvent

# AUDIT R8: refire an alert only when the metric worsened by >= this margin
ALERT_REFIRE_MARGIN_PCT = 5.0
META_ENTER_REFIRE_MARGIN_PCT = 10.0


class MetaDataProvider(Protocol):
    async def window_avg(
        self, item_id: str, quality: int, upgrade: int, hours: int, offset_hours: int = 0
    ) -> float | None: ...

    async def window_count(
        self, item_id: str, quality: int, upgrade: int, hours: int, offset_hours: int = 0
    ) -> int: ...

    async def spread_pct(
        self,
        item_id: str,
        quality: int,
        upgrade: int,
        hours: int,
        since: datetime | None = None,
    ) -> float | None: ...

    def cheap_lots_count(
        self, item_id: str, quality: int, upgrade: int, avg: float, below_pct: float
    ) -> int: ...


class MetaStateStore(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def keys_with_prefix(self, prefix: str) -> ABSet[str]: ...


@dataclass(frozen=True)
class MetaThresholds:
    dump_drop_pct: float = 20.0
    dump_window_hours: int = 24
    meta_exit_min_lots: int = 10
    meta_exit_below_avg_pct: float = 25.0
    stabilization_hours: int = 48
    stabilization_max_spread_pct: float = 15.0
    meta_enter_window_hours: int = 6
    meta_enter_surge_pct: float = 50.0
    fast_adapt_weight: float = 3.0


def group_key(item_id: str, quality: int, upgrade: int) -> str:
    return f"{item_id}:{quality}:{upgrade}"


def is_falling(current: float | None, previous: float | None, drop_pct: float) -> bool:
    if current is None or previous is None or previous <= 0:
        return False
    return (previous - current) / previous * 100.0 >= drop_pct


async def check_dump(
    provider: MetaDataProvider,
    store: MetaStateStore,
    thresholds: MetaThresholds,
    item_id: str,
    quality: int,
    upgrade: int,
) -> NotificationEvent | None:
    """Avg dropped by >DUMP_DROP_PCT over DUMP_WINDOW_HOURS vs the previous window."""
    window = thresholds.dump_window_hours
    current = await provider.window_avg(item_id, quality, upgrade, hours=window)
    previous = await provider.window_avg(
        item_id, quality, upgrade, hours=window, offset_hours=window
    )
    # AUDIT R8: dedup alerts — refire only when the drop worsened by >= margin.
    state_key = f"alert:dump:{group_key(item_id, quality, upgrade)}"
    if is_falling(current, previous, thresholds.dump_drop_pct):
        assert current is not None and previous is not None
        drop = (previous - current) / previous * 100.0
        prev_raw = await store.get(state_key)
        if prev_raw is not None:
            prev = float(prev_raw)
            if drop <= prev + ALERT_REFIRE_MARGIN_PCT:
                await store.set(state_key, f"{max(prev, drop)}")
                return None
        await store.set(state_key, f"{drop}")
        return NotificationEvent(
            event_type=EventType.DUMP,
            item_id=item_id,
            title=f"Дамп цены: {item_id} q{quality} +{upgrade}",
            fields={
                "quality": quality,
                "upgrade": upgrade,
                "avg_now": round(current, 2),
                "avg_prev": round(previous, 2),
                "drop_pct": round(drop, 1),
            },
        )
    else:
        prev_raw = await store.get(state_key)
        if prev_raw is not None:
            await store.delete(state_key)
    return None


async def check_meta_exit(
    provider: MetaDataProvider,
    store: MetaStateStore,
    thresholds: MetaThresholds,
    item_id: str,
    quality: int,
    upgrade: int,
) -> NotificationEvent | None:
    """>=MIN_LOTS active lots BELOW_AVG_PCT under the average while the average is falling.

    On trigger: notify + invalidate (soft-delete) the average until stabilization.
    """
    key = f"invalid:{group_key(item_id, quality, upgrade)}"
    if await store.get(key) is not None:
        return None  # already invalidated
    window = thresholds.dump_window_hours
    avg = await provider.window_avg(item_id, quality, upgrade, hours=window * 7)
    if avg is None:
        return None
    cheap = provider.cheap_lots_count(
        item_id, quality, upgrade, avg, thresholds.meta_exit_below_avg_pct
    )
    if cheap < thresholds.meta_exit_min_lots:
        return None
    current = await provider.window_avg(item_id, quality, upgrade, hours=window)
    previous = await provider.window_avg(
        item_id, quality, upgrade, hours=window, offset_hours=window
    )
    if current is None or previous is None or not (current < previous):
        return None
    await store.set(key, f"{avg}|{datetime.now(UTC).isoformat()}")  # P1-7: stamp
    return NotificationEvent(
        event_type=EventType.META_EXIT,
        item_id=item_id,
        title=f"Выход из меты: {item_id} q{quality} +{upgrade}",
        fields={
            "quality": quality,
            "upgrade": upgrade,
            "cheap_lots": cheap,
            "avg": round(avg, 2),
            "avg_now": round(current, 2),
        },
    )


async def check_stabilization(
    provider: MetaDataProvider,
    store: MetaStateStore,
    thresholds: MetaThresholds,
    item_id: str,
    quality: int,
    upgrade: int,
) -> bool:
    """Re-validate an invalidated average: spread of new sales below the cap
    after STABILIZATION_HOURS -> average recomputed and trading resumes."""
    key = f"invalid:{group_key(item_id, quality, upgrade)}"
    raw = await store.get(key)
    if raw is None:
        return False
    # P1-7: no sales in 48h is NORMAL for an illiquid item - a None spread
    # must not pin the flag forever; re-validate by TTL (2x the window).
    _avg_s, _, ts_s = raw.partition("|")
    since = None
    if ts_s:
        from utils.helpers import parse_api_time

        since = parse_api_time(ts_s)
        if since is not None:
            age_h = (datetime.now(UTC) - since).total_seconds() / 3600.0
            if age_h >= 2 * thresholds.stabilization_hours:
                await store.delete(key)
                return True
    # P1-7: the spread must describe sales AFTER the invalidation — sales from
    # before the dump were exactly what got the group invalidated, so counting
    # them kept the flag pinned forever even once the market calmed down.
    spread = await provider.spread_pct(
        item_id, quality, upgrade, hours=thresholds.stabilization_hours, since=since
    )
    if spread is None:
        return False
    if spread <= thresholds.stabilization_max_spread_pct:
        await store.delete(key)
        return True
    return False


async def check_meta_enter(
    provider: MetaDataProvider,
    store: MetaStateStore,
    thresholds: MetaThresholds,
    item_id: str,
    quality: int,
    upgrade: int,
) -> NotificationEvent | None:
    """Sales surge: window count grew by >=SURGE_PCT vs the previous window."""
    window = thresholds.meta_enter_window_hours
    current = await provider.window_count(item_id, quality, upgrade, hours=window)
    previous = await provider.window_count(
        item_id, quality, upgrade, hours=window, offset_hours=window
    )
    if previous <= 0 or current <= 0:
        return None
    surge = (current - previous) / previous * 100.0
    # AUDIT R8: dedup — refire only when the surge grew by >= margin.
    state_key = f"alert:enter:{group_key(item_id, quality, upgrade)}"
    if surge >= thresholds.meta_enter_surge_pct:
        prev_raw = await store.get(state_key)
        if prev_raw is not None:
            prev = float(prev_raw)
            if surge <= prev + META_ENTER_REFIRE_MARGIN_PCT:
                await store.set(state_key, f"{max(prev, surge)}")
                return None
        await store.set(state_key, f"{surge}")
        return NotificationEvent(
            event_type=EventType.META_ENTER,
            item_id=item_id,
            title=f"Вход в мету: {item_id} q{quality} +{upgrade}",
            fields={
                "quality": quality,
                "upgrade": upgrade,
                "sales_now": current,
                "sales_prev": previous,
                "surge_pct": round(surge, 1),
                "fast_adapt_weight": thresholds.fast_adapt_weight,
            },
        )
    else:
        prev_raw = await store.get(state_key)
        if prev_raw is not None:
            await store.delete(state_key)
    return None


async def evaluate_group(
    provider: MetaDataProvider,
    store: MetaStateStore,
    thresholds: MetaThresholds,
    item_id: str,
    quality: int,
    upgrade: int,
    suppress_meta: bool = False,
) -> list[NotificationEvent]:
    """Run all rules for one (item, quality, upgrade) group.

    suppress_meta=True (seasonal items): dump/meta-exit are suppressed and
    re-emitted by the caller as seasonal events.
    """
    events: list[NotificationEvent] = []
    if not suppress_meta:
        dump = await check_dump(provider, store, thresholds, item_id, quality, upgrade)
        if dump is not None:
            events.append(dump)
        exit_event = await check_meta_exit(provider, store, thresholds, item_id, quality, upgrade)
        if exit_event is not None:
            events.append(exit_event)
    await check_stabilization(provider, store, thresholds, item_id, quality, upgrade)
    enter = await check_meta_enter(provider, store, thresholds, item_id, quality, upgrade)
    if enter is not None:
        events.append(enter)
    return events
