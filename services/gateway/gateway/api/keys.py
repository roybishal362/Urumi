"""API keys for the gateway's MCP endpoint.

A member creates and revokes their own keys. An admin can also issue a key for
a member and revoke anyone's key.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import ApiKey, User, utcnow
from ..security import generate_api_key
from .deps import AppState, current_user, get_state, same_origin

router = APIRouter(prefix="/api/keys", tags=["keys"], dependencies=[Depends(same_origin)])


class KeyCreate(BaseModel):
    name: str = Field(default="", max_length=80)
    # Admins only: issue the key for this member instead of for yourself.
    user_id: str | None = None


def key_view(key: ApiKey, owner_email: str | None = None) -> dict:
    view = {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        "created_at": key.created_at.isoformat(),
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
        "user_id": key.user_id,
        "issued_by": key.issued_by,
    }
    if owner_email is not None:
        view["user_email"] = owner_email
    return view


@router.get("")
async def list_keys(
    everyone: bool = False, user: User = Depends(current_user), state: AppState = Depends(get_state)
) -> dict:
    """Your own keys. Admins can pass ?everyone=true to see the whole team's."""
    query = select(ApiKey, User.email).join(User, User.id == ApiKey.user_id).order_by(ApiKey.created_at.desc())
    if not (everyone and user.role == "admin"):
        query = query.where(ApiKey.user_id == user.id)
    async with state.db.session() as session:
        rows = (await session.execute(query)).all()
    return {"keys": [key_view(key, email) for key, email in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_key(body: KeyCreate, user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    owner_id = user.id
    if body.user_id and body.user_id != user.id:
        if user.role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can issue keys for other people.")
        owner_id = body.user_id

    new = generate_api_key()
    async with state.db.session() as session:
        owner = await session.get(User, owner_id)
        if owner is None or not owner.is_active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such active member.")
        key = ApiKey(
            user_id=owner.id,
            name=body.name.strip() or "API key",
            prefix=new.prefix,
            key_hash=new.key_hash,
            # Calls made with this key are audited under the owner, so record
            # who really created it when that was someone else.
            issued_by=user.email if owner.id != user.id else None,
        )
        session.add(key)
        await session.commit()
    # The only time the key itself leaves the gateway. Only its hash was stored.
    return {"key": key_view(key, owner.email), "secret": new.plaintext}


@router.delete("/{key_id}")
async def revoke_key(key_id: str, user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    async with state.db.session() as session:
        key = await session.get(ApiKey, key_id)
        # A member asking about someone else's key gets the same answer as for
        # a key that does not exist.
        if key is None or (key.user_id != user.id and user.role != "admin"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such key.")
        if key.revoked_at is None:
            # Kept, not deleted, so the audit log can still name the key.
            key.revoked_at = utcnow()
            await session.commit()
    return {"key": key_view(key)}
