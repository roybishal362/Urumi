"""Team members: inviting people, changing roles, removing access. Admins only."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from ..db import ApiKey, User, utcnow
from ..security import generate_invite_token
from .deps import AppState, get_state, require_admin, same_origin

router = APIRouter(
    prefix="/api/members", tags=["members"], dependencies=[Depends(same_origin), Depends(require_admin)]
)

Role = Literal["admin", "member"]


class InviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    name: str = Field(default="", max_length=120)
    role: Role = "member"


class MemberUpdate(BaseModel):
    role: Role | None = None
    is_active: bool | None = None


def member_view(user: User, active_keys: int = 0) -> dict:
    if not user.is_active:
        state = "deactivated"
    elif user.password_hash is None:
        state = "invited"
    else:
        state = "active"
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "state": state,
        "active_keys": active_keys,
        "created_at": user.created_at.isoformat(),
    }


def _new_invite(user: User, state: AppState) -> str:
    """Give the user a fresh one-time invite and return the link to send them."""
    token, token_hash = generate_invite_token()
    user.invite_token_hash = token_hash
    user.invite_expires_at = utcnow() + timedelta(hours=state.settings.invite_ttl_hours)
    return f"{state.settings.public_url}/accept-invite?token={token}"


async def _other_active_admins(session, user_id: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.is_active.is_(True), User.password_hash.is_not(None), User.id != user_id)
        )
    ).scalar_one()


@router.get("")
async def list_members(state: AppState = Depends(get_state)) -> dict:
    async with state.db.session() as session:
        users = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
        counts = dict(
            (
                await session.execute(
                    select(ApiKey.user_id, func.count()).where(ApiKey.revoked_at.is_(None)).group_by(ApiKey.user_id)
                )
            ).all()
        )
    return {"members": [member_view(u, counts.get(u.id, 0)) for u in users]}


@router.post("/invite", status_code=status.HTTP_201_CREATED)
async def invite_member(body: InviteRequest, state: AppState = Depends(get_state)) -> dict:
    """Create the member and return a one-time link for them to set a password.

    There is no mail server here, so the admin passes the link on themselves.
    The link is returned once; only its hash is stored.
    """
    email = body.email.strip().lower()
    async with state.db.session() as session:
        if (await session.execute(select(User.id).where(User.email == email))).first():
            raise HTTPException(status.HTTP_409_CONFLICT, "Someone with this email is already on the team.")
        user = User(email=email, name=body.name.strip(), role=body.role)
        invite_url = _new_invite(user, state)
        session.add(user)
        await session.commit()
    return {"member": member_view(user), "invite_url": invite_url}


@router.post("/{user_id}/invite")
async def renew_invite(user_id: str, state: AppState = Depends(get_state)) -> dict:
    """A new link, for someone who lost theirs or needs a password reset. The old link stops working."""
    async with state.db.session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such member.")
        invite_url = _new_invite(user, state)
        await session.commit()
    return {"member": member_view(user), "invite_url": invite_url}


@router.patch("/{user_id}")
async def update_member(
    user_id: str, body: MemberUpdate, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    async with state.db.session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such member.")
        losing_admin = user.role == "admin" and (body.role == "member" or body.is_active is False)
        if losing_admin and await _other_active_admins(session, user.id) == 0:
            raise HTTPException(status.HTTP_409_CONFLICT, "The team needs at least one active admin.")
        if body.role is not None:
            user.role = body.role
        if body.is_active is not None:
            # Deactivating stops the dashboard session and every API key on
            # the member's next request, without deleting anything.
            user.is_active = body.is_active
        await session.commit()
    return {"member": member_view(user)}


@router.delete("/{user_id}")
async def remove_member(user_id: str, admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    if user_id == admin.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot remove yourself.")
    async with state.db.session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such member.")
        # Their keys go with them. Audit rows stay: they carry their own copy
        # of the email address.
        await session.execute(delete(ApiKey).where(ApiKey.user_id == user_id))
        await session.delete(user)
        await session.commit()
    return {"ok": True}
