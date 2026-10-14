"""Registering and managing upstream MCP servers."""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select

from ..aggregator import exposed_name
from ..db import ToolPin, ToolPolicy, UpstreamServer, User, bump_registry_version
from ..ssrf import UnsafeUrlError
from ..upstreams import ServerRecord, tool_definition
from .deps import AppState, current_user, get_state, require_admin, same_origin

router = APIRouter(prefix="/api/servers", tags=["servers"], dependencies=[Depends(same_origin)])

_SLUG = re.compile(r"^[a-z][a-z0-9]{1,15}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,80}$")
# Headers a "custom header" credential must not be allowed to override.
_RESERVED_HEADERS = {"host", "content-length", "content-type", "transfer-encoding", "connection", "accept"}

AuthType = Literal["none", "bearer", "header"]


class ServerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    slug: str
    url: str = Field(min_length=8, max_length=500)
    auth_type: AuthType = "none"
    auth_header: str | None = None
    credential: str | None = Field(default=None, max_length=4000)
    enabled: bool = True

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        value = value.strip().lower()
        if not _SLUG.match(value):
            raise ValueError("2-16 lowercase letters and digits, starting with a letter")
        return value


class ServerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    url: str | None = Field(default=None, min_length=8, max_length=500)
    auth_type: AuthType | None = None
    auth_header: str | None = None
    # Left out: keep the stored credential. A string: replace it.
    credential: str | None = Field(default=None, max_length=4000)
    enabled: bool | None = None


class ToolPolicyUpdate(BaseModel):
    tool: str = Field(min_length=1, max_length=200)
    admin_only: bool


class ToolRef(BaseModel):
    tool: str = Field(min_length=1, max_length=200)


def _check_auth(auth_type: str, auth_header: str | None, has_credential: bool) -> str | None:
    """Validate the credential settings and return the header name to store."""
    if auth_type == "none":
        return None
    if not has_credential:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A credential is required for this auth type.")
    if auth_type == "bearer":
        return None
    name = (auth_header or "").strip()
    if not _HEADER_NAME.match(name) or name.lower() in _RESERVED_HEADERS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Give a valid header name, for example X-API-Key.")
    return name


async def _check_url(url: str, state: AppState) -> None:
    try:
        await state.manager.check_url(url)
    except UnsafeUrlError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _outage_view(outage) -> dict:
    return {
        "started": outage.started.isoformat(),
        "ended": outage.ended.isoformat(),
        "seconds": round(outage.seconds, 1),
        "reason": outage.reason,
    }


def server_view(record: ServerRecord, state: AppState, viewer: User) -> dict:
    """What the dashboard shows for one server. The credential is never included, for anyone."""
    connection = state.manager.connection(record.id)
    admin = viewer.role == "admin"
    if not record.enabled:
        status_name = "disabled"
    elif connection is None:
        status_name = "connecting"
    else:
        status_name = connection.status

    tools = []
    for tool in connection.tools if connection else []:
        admin_only = state.manager.is_admin_only(record.id, tool.name)
        changed = state.manager.changed_since_approval(record.id, tool.name, connection.fingerprints.get(tool.name))
        # Members see neither admin-only tools nor tools waiting for review.
        if (admin_only or changed) and not admin:
            continue
        tools.append(
            {
                "name": exposed_name(record.slug, tool.name),
                "tool": tool.name,
                "description": (tool.description or "").strip()[:400],
                "admin_only": admin_only,
                "changed": changed,
            }
        )

    view = {
        "id": record.id,
        "slug": record.slug,
        "name": record.name,
        "enabled": record.enabled,
        "status": status_name,
        "error": connection.error if connection and record.enabled else None,
        "since": connection.since.isoformat() if connection else None,
        "last_ok": connection.last_ok.isoformat() if connection and connection.last_ok else None,
        "protocol_version": connection.protocol_version if connection else None,
        "last_outage": _outage_view(connection.last_outage) if connection and connection.last_outage else None,
        "outage_count": connection.outage_count if connection else 0,
        "tools": sorted(tools, key=lambda t: t["name"]),
    }
    if admin:
        # Where the server lives and how the gateway authenticates to it is
        # admin business. Members only need to know the server exists.
        view.update(
            {
                "url": record.url,
                "auth_type": record.auth_type,
                "auth_header": record.auth_header,
                "has_credential": record.has_credential,
            }
        )
    return view


@router.get("")
async def list_servers(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    await state.manager.ensure_current()
    return {"servers": [server_view(record, state, user) for record in state.manager.records()]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_server(body: ServerCreate, admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    header = _check_auth(body.auth_type, body.auth_header, bool(body.credential))
    await _check_url(body.url, state)
    async with state.db.session() as session:
        taken = (await session.execute(select(UpstreamServer.id).where(UpstreamServer.slug == body.slug))).first()
        if taken:
            raise HTTPException(status.HTTP_409_CONFLICT, f"The prefix '{body.slug}' is already used by another server.")
        row = UpstreamServer(
            slug=body.slug,
            name=body.name.strip(),
            url=body.url.strip(),
            auth_type=body.auth_type,
            auth_header=header,
            credential_enc=state.cipher.encrypt(body.credential) if body.credential and body.auth_type != "none" else None,
            enabled=body.enabled,
        )
        session.add(row)
        await bump_registry_version(session)
        await session.commit()
        server_id = row.id
    await state.manager.reconcile()
    return {"server": server_view(state.manager.record(server_id), state, admin)}


@router.patch("/{server_id}")
async def update_server(
    server_id: str, body: ServerUpdate, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    async with state.db.session() as session:
        row = await session.get(UpstreamServer, server_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such server.")

        auth_type = body.auth_type or row.auth_type
        has_credential = bool(body.credential) or bool(row.credential_enc)
        header = _check_auth(auth_type, body.auth_header if body.auth_header is not None else row.auth_header, has_credential)
        if body.url is not None and body.url.strip() != row.url:
            await _check_url(body.url, state)
            row.url = body.url.strip()
        if body.name is not None:
            row.name = body.name.strip()
        if body.enabled is not None:
            row.enabled = body.enabled
        row.auth_type, row.auth_header = auth_type, header
        if auth_type == "none":
            row.credential_enc = None
        elif body.credential:
            row.credential_enc = state.cipher.encrypt(body.credential)
        # Tells every replica its connection to this server is out of date.
        row.revision += 1
        await bump_registry_version(session)
        await session.commit()
    await state.manager.reconcile()
    return {"server": server_view(state.manager.record(server_id), state, admin)}


@router.delete("/{server_id}")
async def remove_server(server_id: str, _: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    async with state.db.session() as session:
        row = await session.get(UpstreamServer, server_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such server.")
        await session.execute(delete(ToolPolicy).where(ToolPolicy.server_id == server_id))
        await session.execute(delete(ToolPin).where(ToolPin.server_id == server_id))
        await session.delete(row)
        await bump_registry_version(session)
        await session.commit()
    await state.manager.reconcile()
    return {"ok": True}


@router.post("/{server_id}/refresh")
async def refresh_server(server_id: str, admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    record = state.manager.record(server_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such server.")
    if connection := state.manager.connection(server_id):
        connection.refresh_now()
    return {"server": server_view(record, state, admin)}


@router.put("/{server_id}/tool-policy")
async def set_tool_policy(
    server_id: str, body: ToolPolicyUpdate, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    """Mark a tool as admin-only (hidden from members and refused if they call it), or open it again."""
    if state.manager.record(server_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such server.")
    async with state.db.session() as session:
        policy = await session.get(ToolPolicy, (server_id, body.tool))
        if body.admin_only and policy is None:
            session.add(ToolPolicy(server_id=server_id, tool_name=body.tool, admin_only=True))
        elif not body.admin_only and policy is not None:
            await session.delete(policy)
        await bump_registry_version(session)
        await session.commit()
    await state.manager.reconcile()
    return {"server": server_view(state.manager.record(server_id), state, admin)}


@router.post("/{server_id}/tool-review")
async def review_tool(server_id: str, body: ToolRef, _: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    """The approved and the current definition of a tool, for an admin deciding whether to accept a change."""
    connection = state.manager.connection(server_id)
    current = next((t for t in connection.tools if t.name == body.tool), None) if connection else None
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That tool is not currently offered by the server.")
    return {"approved": await state.manager.approved_definition(server_id, body.tool), "current": tool_definition(current)}


@router.post("/{server_id}/tool-approve")
async def approve_tool(server_id: str, body: ToolRef, admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    """Accept a tool's current definition, which makes it available again."""
    if not await state.manager.approve_tool(server_id, body.tool, admin.email):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That tool is not currently offered by the server.")
    return {"server": server_view(state.manager.record(server_id), state, admin)}
