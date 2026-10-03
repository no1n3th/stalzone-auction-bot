"""P0-2: outbox durability — persist at enqueue, close on delivery, sweep replays."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from api.discord import DiscordNotifier
from database.orm import OutboxRow
from database.repositories.misc import OutboxRepository


def make_notifier(session):
    settings = SimpleNamespace(
        discord_webhooks=["http://hook"],  # тестовый фейк, не .env
        discord_rate_per_sec=100,
        ws_feed_queue_size=10,
        notify_batch_size=10,
    )
    return DiscordNotifier(settings, session=SimpleNamespace())


async def test_outbox_lifecycle(session):
    """enqueue persists -> delivery closes the row -> second sweep finds nothing."""
    repo = OutboxRepository(session)
    notifier = make_notifier(session)
    notifier._outbox_add = repo.add
    notifier._outbox_done = repo.mark_sent

    # enqueue -> row persisted with priority/event_type, no chart bytes
    await notifier.enqueue("sig1", {"title": "t", "event_type": "lot"}, datetime.now(UTC), priority=0)
    rows = await repo.pending()
    assert len(rows) == 1
    payload = json.loads(rows[0].payload)
    assert payload["lot_sig"] == "sig1"
    assert payload["priority"] == 0  # P0-2.5: durable priority, not the embed lookup
    assert payload["event_type"] == "lot"
    assert "chart_png" not in payload  # bytes never go into the outbox
    embed = payload["embed"]
    assert embed["title"] and all(f["value"] for f in embed["fields"])

    # fresh rows are NOT pending yet (P0-2.4: min age) — simulate delivery window
    await repo.mark_attempt(rows[0].id, None)  # no-op except row exists
    row = await session.get(type(rows[0]), rows[0].id)
    row.created_at = datetime.now(UTC) - timedelta(seconds=300)
    await session.flush()

    # sweep-style replay: persist=False, outbox_id passed
    notifier2 = make_notifier(session)
    notifier2._outbox_add = repo.add
    notifier2._outbox_done = repo.mark_sent
    await notifier2.enqueue(
        payload["lot_sig"],
        payload["embed"],
        datetime.fromisoformat(payload["detected_at"]),
        priority=payload["priority"],
        persist=False,
        outbox_id=rows[0].id,
    )
    # exactly one queued item, carrying the row id
    queued = notifier2.queue.get_nowait()[3]
    assert queued["_outbox_id"] == rows[0].id

    # deliver -> row closed
    class FakeResp:
        status = 200

        async def json(self):
            return {"id": "m1"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    notifier2.session = SimpleNamespace(post=lambda *a, **kw: FakeResp())
    assert await notifier2._send_one(queued) is True
    done = await repo.pending()
    assert done == []


async def test_second_sweep_sends_nothing_after_restart(session, session_factory):
    """Restart simulation: notifier with the same outbox never duplicates."""
    repo = OutboxRepository(session)
    notifier = make_notifier(session)
    notifier._outbox_add = repo.add
    notifier._outbox_done = repo.mark_sent
    await notifier.enqueue("sig1", {"title": "t"}, datetime.now(UTC), priority=1)
    row = (await session.scalars(select(OutboxRow))).first()
    row.created_at = datetime.now(UTC) - timedelta(seconds=300)
    await session.flush()
    await repo.mark_sent(row.id)

    # "restart": brand new notifier over a fresh session
    async with session_factory() as s2:
        repo2 = OutboxRepository(s2)
        assert await repo2.pending() == []  # nothing re-enqueued


async def test_outbox_survives_restart_replay(session, session_factory):
    """Unsent rows re-enter the queue after a restart (at-least-once)."""
    repo = OutboxRepository(session)
    row_id = await repo.add(json.dumps({"lot_sig": "s", "embed": {"title": "x"}, "detected_at": datetime.now(UTC).isoformat(), "priority": 3}))
    row = await session.get(OutboxRow, row_id)
    row.created_at = datetime.now(UTC) - timedelta(seconds=300)
    await session.flush()

    async with session_factory() as s2:
        rows = await OutboxRepository(s2).pending()
        assert [r.id for r in rows] == [row_id]
