"""P1-10: feature flags from the admin UI actually reach the runtime.

- _reload_runtime_flags loads flags from the DB into container.runtime_flags
- charts_enabled flips ChartService.enabled; discord_enabled -> notifier.paused
- workers read flags from memory via workers.flag_on (no DB hit per cycle)
- P1-7: window_spread_pct(since=...) narrows the window to post-invalidation sales
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from database.repositories.history import HistoryRepository
from database.repositories.misc import FeatureFlagsRepository
from scanner import workers


def _container(session_factory):
    from config.settings import Settings

    return SimpleNamespace(
        db=SimpleNamespace(session=session_factory),
        chart_service=SimpleNamespace(enabled=True),
        notifier=SimpleNamespace(paused=False, icons={}),
        settings=Settings(),
        runtime_flags={},
    )


async def test_flags_load_and_apply_side_effects(session_factory):
    container = _container(session_factory)
    async with session_factory() as s:
        flags = FeatureFlagsRepository(s)
        await flags.set("scanner_enabled", False)
        await flags.set("charts_enabled", False)
        await flags.set("discord_enabled", False)
        await s.commit()  # иначе транзакция не видна из сессии _reload_runtime_flags

    await workers._reload_runtime_flags(container)

    assert container.runtime_flags["scanner_enabled"] is False
    assert workers.flag_on(container, "scanner_enabled") is False
    # флага нет в таблице -> дефолт (UI не может сломать бот удалением строки)
    assert workers.flag_on(container, "history_sync_enabled", True) is True
    assert workers.flag_on(container, "never_set_flag", False) is False
    assert container.chart_service.enabled is False  # charts_enabled honored
    assert container.notifier.paused is True  # discord_enabled honored


async def test_flags_refresh_picks_up_changes(session_factory):
    container = _container(session_factory)
    await workers._reload_runtime_flags(container)
    assert workers.flag_on(container, "scanner_enabled") is True  # default on

    async with session_factory() as s:
        await FeatureFlagsRepository(s).set("scanner_enabled", False)
        await s.commit()
    await workers._reload_runtime_flags(container)
    assert workers.flag_on(container, "scanner_enabled") is False


async def test_paused_notifier_drops_enqueue():
    from api.discord import DiscordNotifier

    notifier = object.__new__(DiscordNotifier)  # без сети/сессии: только paused-ветка
    notifier.paused = True
    calls: list = []

    async def _fake_outbox_add(_p):  # pragma: no cover - не должно вызываться
        calls.append("outbox")
        return 1

    notifier._outbox_add = _fake_outbox_add
    await notifier.enqueue("sig", {"title": "t"}, datetime.now(UTC), priority=1)
    assert calls == []  # ничего не ушло ни в outbox, ни в очередь


async def test_spread_respects_since(session):
    """P1-7: окно спреда после инвалидации не содержит старый дамп."""
    repo = HistoryRepository(session)
    now = datetime.now(UTC)
    await repo.bulk_insert(
        [
            {"item_id": "it1", "quality": 0, "upgrade": 0, "price": 500.0,
             "sold_at": now - timedelta(hours=40)},   # волатильная продажа (дамп)
            {"item_id": "it1", "quality": 0, "upgrade": 0, "price": 101.0,
             "sold_at": now - timedelta(hours=2)},
            {"item_id": "it1", "quality": 0, "upgrade": 0, "price": 102.0,
             "sold_at": now - timedelta(hours=1)},
        ]
    )
    full = await repo.window_spread_pct("it1", 0, 0, 48)
    narrowed = await repo.window_spread_pct("it1", 0, 0, 48, since=now - timedelta(hours=3))
    assert full is not None and narrowed is not None
    assert narrowed < full  # дамп исключён из окна
