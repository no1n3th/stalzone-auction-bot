import json
from datetime import UTC, datetime, timedelta

from database.orm import OutboxRow
from database.repositories.misc import OutboxRepository


async def test_pending_respects_next_attempt_at(session):
    """P0-2: sweep не реэнкьюит строку раньше бэкоффа из mark_attempt."""
    repo = OutboxRepository(session)
    row_id = await repo.add(json.dumps({"x": 1}))
    row = await session.get(OutboxRow, row_id)
    row.created_at = datetime.now(UTC) - timedelta(seconds=300)
    await session.flush()
    await repo.mark_attempt(row_id, datetime.now(UTC) + timedelta(minutes=5))
    assert await repo.pending() == []  # бэкофф ещё не истёк
    await repo.mark_attempt(row_id, datetime.now(UTC) - timedelta(seconds=1))
    assert [r.id for r in await repo.pending()] == [row_id]
