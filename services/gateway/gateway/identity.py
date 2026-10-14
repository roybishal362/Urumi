"""Who is making a request, and how the gateway works that out.

Two ways in:
  - an API key, sent as a bearer token by an external MCP client
  - a session cookie, set when someone signs in to the dashboard

Both end in a `Principal`. Everything downstream (which tools are visible,
what the audit log records) works from the principal alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from .db import ApiKey, Database, User, utcnow
from .security import API_KEY_PREFIX, hash_token

# last_used_at is informational, so it is written at most this often per key.
_LAST_USED_GRANULARITY = timedelta(minutes=1)


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    role: str
    # "mcp" for an external client, "chat" for the dashboard agent.
    source: str
    key_prefix: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass(frozen=True)
class KeyRejection:
    """Why a bearer token was refused. `user` is set when the key was once valid."""

    reason: str
    user_id: str | None = None
    email: str = ""
    key_prefix: str | None = None


async def authenticate_api_key(db: Database, token: str) -> Principal | KeyRejection:
    """Resolve a bearer token to a principal.

    The database is consulted on every request and nothing is cached, so
    revoking a key or deactivating a member takes effect on their very next call.
    """
    if not token.startswith(API_KEY_PREFIX):
        return KeyRejection("Unknown API key.")
    async with db.session() as session:
        row = (
            await session.execute(
                select(ApiKey, User).join(User, User.id == ApiKey.user_id).where(ApiKey.key_hash == hash_token(token))
            )
        ).first()
        if row is None:
            return KeyRejection("Unknown API key.")
        key, user = row
        if key.revoked_at is not None:
            return KeyRejection("This API key has been revoked.", user.id, user.email, key.prefix)
        if not user.is_active:
            return KeyRejection("This account has been deactivated.", user.id, user.email, key.prefix)

        now = utcnow()
        if key.last_used_at is None or now - key.last_used_at > _LAST_USED_GRANULARITY:
            key.last_used_at = now
            await session.commit()
        return Principal(user_id=user.id, email=user.email, role=user.role, source="mcp", key_prefix=key.prefix)
