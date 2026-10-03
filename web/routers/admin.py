"""Admin routes: stats, DLQ replay, feature flags, runtime config."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from database.repositories.deals import DealsRepository
from database.repositories.history import HistoryRepository
from database.repositories.misc import (
    AuditRepository,
    ConfigRepository,
    DLQRepository,
    FeatureFlagsRepository,
)
from database.repositories.users import UsersRepository
from web.deps import AdminDep, ContainerDep, SessionDep
from web.schemas import AdminStatsResponse, ConfigSetRequest, FlagSetRequest

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/stats", response_model=AdminStatsResponse)
async def admin_stats(
    _admin: str = AdminDep, session: Any = SessionDep, container: Any = ContainerDep
) -> Any:
    history = await HistoryRepository(session).get_stats()
    dlq_pending = await DLQRepository(session).count_pending()
    users = await UsersRepository(session).count()
    metrics = {
        name: container.metrics.get(name)
        for name in (
            "lots_fetched",
            "lots_profitable",
            "meta_events",
            "seasonal_events",
            "deals_total",
        )
    }
    return AdminStatsResponse(
        history_rows=history["rows"],
        history_items=history["items"],
        dlq_pending=dlq_pending,
        users=users,
        # P1-10: real DB count, not a process-lifetime counter
        deals=await DealsRepository(session).count(),
        ws_clients=container.bus.ws_clients_count,
        metrics=metrics,
    )


@router.post("/replay-dlq")
async def replay_dlq(
    _admin: str = AdminDep, session: Any = SessionDep, container: Any = ContainerDep
) -> Any:
    if container.notifier is None:
        raise HTTPException(400, detail="notifier disabled")
    import json

    repo = DLQRepository(session)
    due = await repo.due(limit=50)
    await AuditRepository(session).log(_admin, "dlq.replay", f"due={len(due)}")
    sent = failed = 0
    for row in due:
        s, f = await container.notifier.replay_dlq([json.loads(row.payload)])
        sent += s
        failed += f
        if s:
            await repo.mark_done(row.id)
        else:
            await repo.mark_retry(row.id, backoff_sec=300.0)
    return {"sent": sent, "failed": failed}


@router.get("/flags")
async def get_flags(_admin: str = AdminDep, session: Any = SessionDep) -> Any:
    return await FeatureFlagsRepository(session).all()


@router.post("/flags/{name}")
async def set_flag(
    name: str, body: FlagSetRequest, _admin: str = AdminDep, session: Any = SessionDep
) -> Any:
    await FeatureFlagsRepository(session).set(name, body.enabled)
    # AUDIT B6/A13: admin actions leave an audit trail.
    await AuditRepository(session).log(_admin, "flag.set", f"{name}={body.enabled}")
    return {"name": name, "enabled": body.enabled}


@router.get("/config")
async def get_config(_admin: str = AdminDep, session: Any = SessionDep) -> Any:
    return await ConfigRepository(session).all()


@router.post("/config/{key}")
async def set_config(
    key: str, body: ConfigSetRequest, _admin: str = AdminDep, session: Any = SessionDep
) -> Any:
    await ConfigRepository(session).set(key, body.value)
    await AuditRepository(session).log(_admin, "config.set", f"{key}={body.value}")
    return {"key": key, "value": body.value}


@router.get("/audit")
async def get_audit(_admin: str = AdminDep, session: Any = SessionDep) -> Any:
    """P1-10: the audit log was write-only — expose it to admins."""
    return [
        {
            "id": r.id,
            "username": r.username,
            "action": r.action,
            "details": r.details,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in await AuditRepository(session).list(limit=500)
    ]
