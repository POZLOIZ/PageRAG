"""登录接口：注册、登录、登出、当前用户与开关状态。"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from pagerag.auth import (
    SESSION_COOKIE,
    SESSION_DAYS,
    authenticate,
    create_user,
    issue_session,
    resolve_session,
    revoke_session,
)
from pagerag.deps import get_session
from pagerag.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str
    password: str


def _payload(user: User) -> dict:
    return {"user_id": user.public_id, "username": user.username}


def _enabled(request: Request) -> bool:
    return bool(request.app.state.settings.auth_enabled)


async def current_user(request: Request,
                       session: AsyncSession = Depends(get_session)) -> User | None:
    if not _enabled(request):
        return None
    user = getattr(request.state, "user", None)
    if user is not None:
        return user
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return await resolve_session(session, token)


async def require_user(request: Request,
                       session: AsyncSession = Depends(get_session)) -> User:
    user = await current_user(request, session)
    if user is None:
        raise HTTPException(401, "请先登录")
    return user


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_DAYS * 24 * 3600,
        httponly=True, samesite="lax", path="/")


@router.get("/status")
async def status(request: Request) -> dict:
    return {"enabled": _enabled(request), "registration_open": _enabled(request)}


@router.get("/session")
async def session(request: Request,
                  db: AsyncSession = Depends(get_session)) -> dict:
    """登录页首次探测：未登录也返回 200，避免控制台出现无意义的 401。"""
    if not _enabled(request):
        return {"enabled": False, "user": None}
    user = await current_user(request, db)
    return {"enabled": True, "user": _payload(user) if user is not None else None}


@router.get("/me")
async def me(user: User = Depends(require_user)) -> dict:
    return _payload(user)


@router.post("/register")
async def register(payload: Credentials, request: Request, response: Response,
                   session: AsyncSession = Depends(get_session)) -> dict:
    if not _enabled(request):
        raise HTTPException(400, "当前服务没有开启账号登录")
    try:
        user = await create_user(session, payload.username, payload.password)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    token = await issue_session(session, user)
    await session.commit()
    _set_session_cookie(response, token)
    return _payload(user)


@router.post("/login")
async def login(payload: Credentials, request: Request, response: Response,
                session: AsyncSession = Depends(get_session)) -> dict:
    if not _enabled(request):
        raise HTTPException(400, "当前服务没有开启账号登录")
    user = await authenticate(session, payload.username, payload.password)
    if user is None:
        raise HTTPException(401, "用户名或密码不正确")
    token = await issue_session(session, user)
    await session.commit()
    _set_session_cookie(response, token)
    return _payload(user)


@router.post("/logout")
async def logout(request: Request, response: Response,
                 session: AsyncSession = Depends(get_session)) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    await revoke_session(session, token)
    await session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"logged_out": True}
