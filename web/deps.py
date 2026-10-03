"""FastAPI dependencies: sessions, container, current user."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from utils.sessions import SESSION_COOKIE


def get_container(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.container


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    container = request.app.state.container
    async with container.db.session() as session:
        yield session


ContainerDep = Depends(get_container)
SessionDep = Depends(get_session)


async def get_current_user(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE)
    username = await request.app.state.session_store.get(token)
    if username is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not authenticated")
    return str(username)


UserDep = Depends(get_current_user)


async def get_admin_user(
    request: Request,
    username: str = UserDep,
    session: AsyncSession = SessionDep,
) -> str:
    # Single source of truth for admin rights is the DB flag (set at registration);
    # ADMIN_USERNAMES only promotes the flag when an account is created.
    from database.repositories.users import UsersRepository

    row = await UsersRepository(session).get(username)
    if row is None or not row.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin only")
    return str(row.username)


AdminDep = Depends(get_admin_user)
