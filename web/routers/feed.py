"""Live feed: REST history + WebSocket broadcast for authenticated users.

The WS loop heartbeats every 30s (a `{"type": "ping"}` frame the SPA ignores),
times out stuck sends, and closes dead sockets instead of leaking them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from database.repositories.misc import NotificationsRepository
from utils.sessions import SESSION_COOKIE
from web.deps import SessionDep, UserDep
from web.schemas import NotificationResponse

router = APIRouter(prefix="/api", tags=["feed"])

WS_PING_INTERVAL_SEC = 30.0
WS_SEND_TIMEOUT_SEC = 10.0


@router.get("/notifications", response_model=list[NotificationResponse])
async def list_notifications(
    limit: int = 50, event_type: str | None = None, _user: str = UserDep, session: Any = SessionDep
) -> Any:
    rows = await NotificationsRepository(session).recent(
        limit=max(1, min(limit, 200)), event_type=event_type
    )
    return [
        NotificationResponse(
            id=r.id,
            event_type=r.event_type,
            item_id=r.item_id,
            payload=json.loads(r.payload),
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.websocket("/ws/feed")
async def ws_feed(websocket: WebSocket) -> None:
    container = websocket.app.state.container
    token = websocket.cookies.get(SESSION_COOKIE)
    username = await websocket.app.state.session_store.get(token)
    if username is None:
        await websocket.accept()
        await websocket.close(code=4401)
        return
    await websocket.accept()
    queue = container.bus.ws_connect()
    container.metrics.set_gauge("ws_clients", container.bus.ws_clients_count)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=WS_PING_INTERVAL_SEC)
            except TimeoutError:
                payload = {"type": "ping"}  # keepalive: dead peers fail on send
            await asyncio.wait_for(
                websocket.send_text(json.dumps(payload, ensure_ascii=False)),
                timeout=WS_SEND_TIMEOUT_SEC,
            )
    except (WebSocketDisconnect, RuntimeError, TimeoutError, OSError):
        pass
    finally:
        container.bus.ws_disconnect(queue)
        container.metrics.set_gauge("ws_clients", container.bus.ws_clients_count)
        with contextlib.suppress(Exception):
            await websocket.close()
