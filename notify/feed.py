"""In-process pub/sub: persists notifications, broadcasts to WebSocket clients."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import structlog

from database.engine import Database
from database.repositories.misc import NotificationsRepository
from models.events import NotificationEvent

log = structlog.get_logger(__name__)

Subscriber = Callable[[NotificationEvent], Coroutine[Any, Any, None]]


class NotificationBus:
    """Fan-out for notification events: DB persist + WS broadcast + subscribers."""

    def __init__(self, db: Database, ws_queue_size: int = 200) -> None:
        self.db = db
        self.ws_queue_size = ws_queue_size
        self._subscribers: list[Subscriber] = []
        self._ws_clients: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    def ws_connect(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.ws_queue_size)
        self._ws_clients.add(queue)
        return queue

    def ws_disconnect(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._ws_clients.discard(queue)

    @property
    def ws_clients_count(self) -> int:
        return len(self._ws_clients)

    async def publish(self, event: NotificationEvent) -> None:
        """Persist -> WS broadcast -> subscribers. Never raises."""
        try:
            async with self.db.session() as session:
                await NotificationsRepository(session).add(
                    event.event_type.value, event.item_id, event.as_dict()
                )
        except Exception as exc:
            log.error("notify_persist_failed", error=str(exc))

        for queue in list(self._ws_clients):
            try:
                queue.put_nowait(event.as_dict())
            except asyncio.QueueFull:
                # slow consumer: drop oldest, keep the feed live
                try:
                    queue.get_nowait()
                    queue.put_nowait(event.as_dict())
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

        for fn in self._subscribers:
            try:
                await fn(event)
            except Exception as exc:
                log.error("notify_subscriber_failed", error=str(exc))
