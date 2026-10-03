"""Auth routes: register / login / logout / me / user management."""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from database.repositories.users import UsersRepository
from utils.sessions import SESSION_COOKIE
from web.deps import AdminDep, ContainerDep, SessionDep, UserDep
from web.schemas import LoginRequest, RegisterRequest, UserResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


async def _start_session(
    request: Request, response: Response, container: Any, username: str
) -> None:
    """Create a DB session and attach its cookie (shared by register and login)."""
    token = await request.app.state.session_store.create(username)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=container.settings.session_ttl_hours * 3600,  # same TTL as the DB row
        httponly=True,
        samesite="lax",
        secure=container.settings.cookie_secure,
    )


def _throttle_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{username.lower()}"


def _ip_key(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    return f"ip:{host}"


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    container: Any = ContainerDep,
    session: Any = SessionDep,
) -> Any:
    """Create an account and log the user in right away (PROMPT_2: no second step)."""
    repo = UsersRepository(session)
    mode = getattr(container.settings, "registration_mode", "bootstrap")
    if mode == "invite":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="registration by invite is not enabled"
        )
    # P1-8: mode check first (a 409 before 403 used to enumerate existing
    # logins through a closed registration). The lock now covers the actual
    # INSERT+COMMIT - releasing it before the write let two parallel registers
    # on an empty DB both become admin (AUDIT S1 was incomplete).
    async with request.app.state.register_lock:
        if await repo.find(body.username) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="username taken")
        first_user = await repo.count() == 0
        if mode == "bootstrap" and not first_user:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="registration is closed")
        admins = {name.lower() for name in container.settings.admin_list}
        is_admin = first_user or body.username in admins
        try:
            row = await repo.create(body.username, body.password, is_admin=is_admin)
        except IntegrityError as exc:  # concurrent registration of the same login
            raise HTTPException(status.HTTP_409_CONFLICT, detail="username taken") from exc
        payload = UserResponse(username=row.username, is_admin=row.is_admin)
        # commit before opening the session row: a second connection would
        # otherwise wait for our uncommitted write lock (SQLite)
        await session.commit()
    # не перезаписываем существующую сессию: админ, создающий юзера, остаётся собой
    if request.cookies.get(SESSION_COOKIE) is None:
        await _start_session(request, response, container, payload.username)
    return payload


@router.post("/login", response_model=UserResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    container: Any = ContainerDep,
    session: Any = SessionDep,
) -> Any:
    throttle = request.app.state.login_throttle
    key = _throttle_key(request, body.username)
    # P2: throttle a single password sprayed across many logins too — the
    # per-(ip, username) key alone does not stop that.
    ip_key = _ip_key(request)
    retry_after = max(throttle.retry_after(key), throttle.retry_after(ip_key))
    if retry_after > 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts",
            headers={"Retry-After": str(math.ceil(retry_after))},
        )
    row = await UsersRepository(session).verify(body.username, body.password)
    if row is None:
        throttle.fail(key)
        throttle.fail(ip_key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    throttle.reset(key)
    throttle.reset(ip_key)
    await _start_session(request, response, container, row.username)
    return UserResponse(username=row.username, is_admin=row.is_admin)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response:
    await request.app.state.session_store.drop(request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/me", response_model=UserResponse)
async def me(username: str = UserDep, session: Any = SessionDep) -> Any:
    row = await UsersRepository(session).get(username)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
    return UserResponse(username=row.username, is_admin=row.is_admin)


@router.get("/me/settings")
async def get_settings(username: str = UserDep, session: Any = SessionDep) -> Any:
    from database.repositories.settings import UserSettingsRepository

    return await UserSettingsRepository(session).get(username)


class SettingsPatch(BaseModel):
    sound_enabled: bool | None = None
    sound_volume: float | None = None
    sound_events: list[str] | None = None
    profit_threshold_pct: float | None = None
    theme: str | None = None


@router.put("/me/settings")
async def put_settings(body: SettingsPatch, username: str = UserDep, session: Any = SessionDep) -> Any:
    from database.repositories.settings import UserSettingsRepository

    repo = UserSettingsRepository(session)
    current = await repo.get(username)
    patch_data = body.model_dump(exclude_none=True)
    current.update(patch_data)
    await repo.set(username, current)
    return current


@router.get("/users", response_model=list[UserResponse])
async def users_list(_admin: str = AdminDep, session: Any = SessionDep) -> Any:
    rows = await UsersRepository(session).list_all()
    return [UserResponse(username=r.username, is_admin=r.is_admin) for r in rows]


@router.delete("/users/{username}", status_code=status.HTTP_204_NO_CONTENT)
async def users_delete(
    username: str, request: Request, _admin: str = AdminDep, session: Any = SessionDep
) -> Response:
    from database.repositories.deals import DealsRepository

    repo = UsersRepository(session)
    target = await repo.find(username)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
    # P2: an admin must not be able to remove themselves or the last admin,
    # and a deleted account must not leave deals behind that a future account
    # re-registering the same login would silently inherit.
    if target.username == _admin:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="cannot delete your own account"
        )
    if target.is_admin and await repo.count_admins() <= 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="cannot delete the last admin"
        )
    if not await repo.delete(username):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="user not found")
    await DealsRepository(session).delete_for_user(username)
    await request.app.state.session_store.drop_user(username)  # deleted user is logged out
    return Response(status_code=status.HTTP_204_NO_CONTENT)
