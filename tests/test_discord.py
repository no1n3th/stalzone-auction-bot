"""Discord notifier: embed build, batch send, DLQ fallback."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from api.discord import DiscordNotifier
from config.settings import Settings
from models.events import EventType, NotificationEvent
from models.lot import Lot


def make_settings(**kw) -> Settings:
    # discord_webhook_url="" — герметичность от .env в корне репо (иначе тесты
    # ходят в реальный вебхук и «no webhook»-сценарии ломаются)
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:", **{"discord_webhook_url": "", **kw}
    )


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload or {}

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class TestEmbeds:
    def test_build_embed_fields(self):
        notifier = DiscordNotifier(make_settings(), session=SimpleNamespace())
        lot = Lot(item_id="y5vw", lot_id="1", price=700.0, quality=4, upgrade=0)
        embed = notifier.build_embed(
            lot,
            item_name="Скорлупа",
            avg_price=1000.0,
            next_price=900.0,
            market_min=700.0,
            title="Выгодный лот: Скорлупа +0",
        )
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        # P0-5: item name lives in the title now (no duplicated "Предмет" field)
        assert "Скорлупа" in embed["title"]
        assert fields["Качество"] == "Исключительная"
        assert fields["Цена"] == "700,00 руб."
        # next profit: 900*0.95 - 700 = 155
        assert fields["Профит к след."] == "155,00 руб."
        # P0-5: rarity color really applies (the Jinja template used to force one)
        assert embed["color"] == 0xFF5C7A
        # P0-5: thumbnail only with a well-formed https URL
        assert embed["thumbnail"]["url"].startswith("https://")
        assert "timestamp" in embed

    def test_build_embed_no_avg(self):
        notifier = DiscordNotifier(make_settings(), session=SimpleNamespace())
        lot = Lot(item_id="y5vw", price=100.0, quality=2, upgrade=15)
        embed = notifier.build_embed(lot, "X", None, None, None)
        names = [f["name"] for f in embed["fields"]]
        # P0-5: absent averages produce no field at all (no "—" noise)
        assert not any(n.startswith("Средняя") for n in names)

    def test_event_embed(self):
        notifier = DiscordNotifier(make_settings(), session=SimpleNamespace())
        event = NotificationEvent(
            event_type=EventType.DUMP,
            item_id="y5vw",
            title="Дамп",
            fields={"drop_pct": 25.0},
            detected_at=datetime.now(UTC),
        )
        embed = notifier.build_event_embed(event)
        assert "Дамп" in embed["title"]
        assert embed["fields"][0]["name"] == "Падение"
        assert embed["fields"][0]["value"] == "25%"

    def test_event_embed_resolves_names_and_labels(self):
        notifier = DiscordNotifier(make_settings(), session=SimpleNamespace())
        event = NotificationEvent(
            event_type=EventType.SEASON_START,
            item_id="",
            title="Сезон начался: autumn",
            fields={"season": "autumn", "window_days": 14, "fast_adapt_weight": 3.0},
            detected_at=datetime.now(UTC),
        )
        event.fields["items"] = ["rnkl", "51l0"]
        embed = notifier.build_event_embed(event, item_names={"rnkl": "Силки", "51l0": "Харя"})
        assert "Осень" in embed["title"]
        by_name = {f["name"]: f["value"] for f in embed["fields"]}
        assert by_name["Сезон"] == "Осень"
        assert by_name["Окно истории"] == "14 дн."
        assert by_name["Ускорение адаптации"] == "×3"
        assert by_name["Предметы"] == "Силки, Харя"
        assert "description" not in embed

    def test_event_embed_item_title_name_replacement(self):
        notifier = DiscordNotifier(make_settings(), session=SimpleNamespace())
        event = NotificationEvent(
            event_type=EventType.META_EXIT,
            item_id="j3p6",
            title="Выход из меты: j3p6 q0 +0",
            fields={"quality": 0, "upgrade": 0, "avg": 1234.5},
            detected_at=datetime.now(UTC),
        )
        embed = notifier.build_event_embed(event, item_name="Смольник")
        assert "Смольник" in embed["title"]
        by_name = {f["name"]: f["value"] for f in embed["fields"]}
        assert by_name["Качество"] == "Обычная"
        assert by_name["Заточка"] == "без заточки"
        assert by_name["Средняя до выхода"] == "1 234,50 руб."


class TestSending:
    async def test_no_webhook_ok(self):
        notifier = DiscordNotifier(make_settings(), session=SimpleNamespace())
        assert await notifier._send_one({"embed": {}}) is True

    async def test_send_success(self):
        session = SimpleNamespace(post=lambda *a, **kw: FakeResponse(200))
        notifier = DiscordNotifier(
            make_settings(discord_webhook_url="http://hook"), session=session
        )
        assert await notifier._send_one({"embed": {}}) is True
        assert notifier._sent == 1

    async def test_send_failure_to_dlq(self):
        session = SimpleNamespace(post=lambda *a, **kw: FakeResponse(500))
        dlq: list[dict] = []

        async def dlq_factory(payload):
            dlq.append(payload)

        notifier = DiscordNotifier(
            make_settings(discord_webhook_url="http://hook", notify_send_interval_sec=0),
            session=session,
            dlq_factory=dlq_factory,
        )
        await notifier._send_batch([{"embed": {}, "chart_png": None}])
        assert len(dlq) == 1
        assert notifier._failed == 1

    async def test_queue_full_drops(self):
        notifier = DiscordNotifier(make_settings(ws_feed_queue_size=1), session=SimpleNamespace())
        notifier.queue = asyncio.Queue(maxsize=1)
        await notifier.enqueue("a", {}, datetime.now(UTC))
        await notifier.enqueue("b", {}, datetime.now(UTC))  # dropped, no raise
        assert notifier.queue.qsize() == 1

    async def test_run_sender_drains(self):
        session = SimpleNamespace(post=lambda *a, **kw: FakeResponse(200))
        notifier = DiscordNotifier(
            make_settings(discord_webhook_url="http://hook", notify_send_interval_sec=0),
            session=session,
        )
        stop = asyncio.Event()
        await notifier.enqueue("a", {"embeds": []}, datetime.now(UTC))
        stop.set()
        await notifier.run_sender(stop)
        assert notifier._sent == 1
