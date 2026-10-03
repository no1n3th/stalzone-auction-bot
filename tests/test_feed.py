"""Notification bus: persistence, WS broadcast, slow-consumer drop."""

from __future__ import annotations

from models.events import EventType, NotificationEvent
from notify.feed import NotificationBus


class FakeDB:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def session(self):
        factory = self.session_factory

        class _CM:
            async def __aenter__(self):
                self.s = factory()
                return self.s

            async def __aexit__(self, *exc):
                if exc[0] is None:
                    await self.s.commit()
                else:
                    await self.s.rollback()
                await self.s.close()
                return False

        return _CM()


class TestNotificationBus:
    async def test_publish_persists_and_broadcasts(self, session_factory):
        bus = NotificationBus(FakeDB(session_factory))
        queue = bus.ws_connect()
        event = NotificationEvent(event_type=EventType.DUMP, item_id="y5vw", title="t")
        await bus.publish(event)
        payload = queue.get_nowait()
        assert payload["event_type"] == "dump"
        assert payload["item_id"] == "y5vw"
        bus.ws_disconnect(queue)

    async def test_subscriber_called(self, session_factory):
        bus = NotificationBus(FakeDB(session_factory))
        got = []

        async def sub(e):
            got.append(e)

        bus.subscribe(sub)
        await bus.publish(NotificationEvent(event_type=EventType.LOT, item_id="a", title="t"))
        assert len(got) == 1

    async def test_subscriber_failure_isolated(self, session_factory):
        bus = NotificationBus(FakeDB(session_factory))

        async def bad(e):
            raise RuntimeError("sub crash")

        bus.subscribe(bad)
        await bus.publish(NotificationEvent(event_type=EventType.LOT, item_id="a", title="t"))

    async def test_slow_consumer_drop_oldest(self, session_factory):
        bus = NotificationBus(FakeDB(session_factory), ws_queue_size=2)
        queue = bus.ws_connect()
        for i in range(5):
            await bus.publish(
                NotificationEvent(event_type=EventType.LOT, item_id=f"i{i}", title="t")
            )
        assert queue.qsize() == 2  # oldest dropped, feed stays live

    async def test_persist_failure_isolated(self):
        class BrokenDB:
            def session(self):
                raise RuntimeError("db down")

        bus = NotificationBus(BrokenDB())
        queue = bus.ws_connect()
        await bus.publish(NotificationEvent(event_type=EventType.LOT, item_id="a", title="t"))
        assert queue.qsize() == 1  # WS still received despite DB failure
