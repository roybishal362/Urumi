"""Shared pieces for the dashboard's REST API: app state, who is signed in, and CSRF."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status

from ..aggregator import Aggregator
from ..chat import LLMClient
from ..config import Settings
from ..db import Database, User
from ..ratelimit import RateLimiter
from ..security import SESSION_COOKIE, CredentialCipher, read_session
from ..upstreams import UpstreamManager


@dataclass
class AppState:
    settings: Settings
    db: Database
    cipher: CredentialCipher
    manager: UpstreamManager
    aggregator: Aggregator
    llm: LLMClient
    chat_limiter: RateLimiter
    ready: bool = False


def get_state(request: Request) -> AppState:
    return request.app.state.gateway


async def current_user(request: Request, state: AppState = Depends(get_state)) -> User:
    """The signed-in dashboard user, from the session cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    user_id = read_session(token, state.settings.session_secret) if token else None
    if user_id:
        async with state.db.session() as session:
            user = await session.get(User, user_id)
        # Looked up on every request, so deactivating someone signs them out at once.
        if user is not None and user.is_active:
            return user
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue.")


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can do this.")
    return user


async def same_origin(request: Request) -> None:
    """Refuse state-changing requests that a different site asked the browser to send.

    The session cookie is SameSite=Lax, which already keeps it off cross-site
    POSTs. This is the second check: if the browser says where the request came
    from, it must be this site.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request refused.")
