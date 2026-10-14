"""Sign-in, sign-out and accepting an invite."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import User, utcnow
from ..security import SESSION_COOKIE, hash_password, hash_token, issue_session, verify_password
from .deps import AppState, current_user, get_state, same_origin

router = APIRouter(prefix="/api/auth", tags=["auth"], dependencies=[Depends(same_origin)])

# A small brake on password guessing: per email address, per gateway replica.
_MAX_ATTEMPTS, _WINDOW_SECONDS = 8, 300
_failed_logins: dict[str, deque[float]] = defaultdict(deque)


class LoginRequest(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=200)


class AcceptInviteRequest(BaseModel):
    token: str = Field(max_length=200)
    name: str = Field(default="", max_length=120)
    password: str = Field(min_length=10, max_length=200)


def user_view(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role}


def _start_session(response: Response, user: User, state: AppState) -> None:
    settings = state.settings
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(user.id, settings.session_secret, settings.session_ttl_hours),
        max_age=settings.session_ttl_hours * 3600,
        # HttpOnly keeps the token away from page scripts.
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


_MAX_TRACKED = 5000


def _too_many_failures(email: str) -> bool:
    cutoff = time.monotonic() - _WINDOW_SECONDS
    if len(_failed_logins) > _MAX_TRACKED:
        # Someone is trying many different addresses. Forget the stale ones so
        # this table cannot grow without bound.
        for stale in [k for k, v in _failed_logins.items() if not v or v[-1] < cutoff]:
            del _failed_logins[stale]
    attempts = _failed_logins[email]
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return len(attempts) >= _MAX_ATTEMPTS


@router.post("/login")
async def login(body: LoginRequest, response: Response, state: AppState = Depends(get_state)) -> dict:
    email = body.email.strip().lower()
    if _too_many_failures(email):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts. Wait a few minutes.")
    async with state.db.session() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    # Verify even when the user does not exist, so both cases take the same time.
    valid = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
    if not (user and valid and user.is_active):
        _failed_logins[email].append(time.monotonic())
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password.")
    _failed_logins.pop(email, None)
    _start_session(response, user, state)
    return {"user": user_view(user)}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    settings = state.settings
    return {
        "user": user_view(user),
        "gateway": {
            "mcp_url": f"{settings.public_url}/mcp",
            "chat_ready": state.llm.configured,
            "chat_model": settings.llm_model,
        },
    }


async def _invited_user(state: AppState, token: str) -> User:
    async with state.db.session() as session:
        user = (
            await session.execute(select(User).where(User.invite_token_hash == hash_token(token)))
        ).scalar_one_or_none()
    if user is None or user.invite_expires_at is None or user.invite_expires_at < utcnow():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This invite link is not valid or has expired.")
    return user


@router.get("/invite/{token}")
async def invite_details(token: str, state: AppState = Depends(get_state)) -> dict:
    user = await _invited_user(state, token)
    return {"email": user.email, "name": user.name, "role": user.role}


@router.post("/accept-invite")
async def accept_invite(body: AcceptInviteRequest, response: Response, state: AppState = Depends(get_state)) -> dict:
    invited = await _invited_user(state, body.token)
    async with state.db.session() as session:
        user = await session.get(User, invited.id)
        user.password_hash = hash_password(body.password)
        user.name = body.name.strip() or user.name
        # One use only.
        user.invite_token_hash = None
        user.invite_expires_at = None
        await session.commit()
    _start_session(response, user, state)
    return {"user": user_view(user)}


_DUMMY_HASH = hash_password("not-a-real-password")
