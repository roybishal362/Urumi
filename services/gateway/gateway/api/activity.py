"""The audit log, the list of tools a user can call, and the chat agent's endpoint."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..chat import run_agent
from ..db import AuditLog, User, utcnow
from ..identity import Principal
from .deps import AppState, current_user, get_state, same_origin

router = APIRouter(prefix="/api", tags=["activity"], dependencies=[Depends(same_origin)])


class ChatRequest(BaseModel):
    # The whole conversation so far. The gateway keeps no chat history itself.
    messages: list[dict[str, Any]] = Field(max_length=60)


def _chat_principal(user: User) -> Principal:
    return Principal(user_id=user.id, email=user.email, role=user.role, source="chat")


def audit_view(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "ts": row.ts.isoformat(),
        "user_email": row.user_email,
        "key_prefix": row.key_prefix,
        "source": row.source,
        "server": row.server_slug,
        "tool": row.tool,
        "duration_ms": row.duration_ms,
        "outcome": row.outcome,
        "error": row.error,
    }


@router.get("/audit")
async def audit_log(
    limit: int = 100,
    before_id: int | None = None,
    user_email: str | None = None,
    server: str | None = None,
    outcome: str | None = None,
    user: User = Depends(current_user),
    state: AppState = Depends(get_state),
) -> dict:
    """Newest first. Admins see every call; a member sees only their own."""
    query = select(AuditLog).order_by(AuditLog.id.desc()).limit(max(1, min(limit, 500)))
    if user.role != "admin":
        query = query.where(AuditLog.user_id == user.id)
    elif user_email:
        query = query.where(AuditLog.user_email == user_email.strip().lower())
    if before_id is not None:
        query = query.where(AuditLog.id < before_id)
    if server:
        query = query.where(AuditLog.server_slug == server)
    if outcome:
        query = query.where(AuditLog.outcome == outcome)
    async with state.db.session() as session:
        rows = (await session.execute(query)).scalars().all()
    return {"entries": [audit_view(row) for row in rows]}


def _percentile(sorted_values: list[int], fraction: float) -> int:
    index = min(len(sorted_values) - 1, max(0, round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


@router.get("/stats")
async def stats(hours: int = 24, user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    """Calls, error rate and latency per server over the last `hours`, from the audit log.

    Admins see the whole team; a member sees their own calls. Computed in
    Python rather than SQL so it works the same on PostgreSQL and SQLite.
    """
    hours = max(1, min(hours, 24 * 7))
    query = select(AuditLog.server_slug, AuditLog.outcome, AuditLog.duration_ms).where(
        AuditLog.ts >= utcnow() - timedelta(hours=hours), AuditLog.server_slug != ""
    )
    if user.role != "admin":
        query = query.where(AuditLog.user_id == user.id)
    async with state.db.session() as session:
        rows = (await session.execute(query.limit(100_000))).all()

    by_server: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for slug, outcome, duration in rows:
        by_server[slug].append((outcome, duration))
    servers = []
    for slug, calls in sorted(by_server.items()):
        # Refused calls (denied, rejected) never reached the server, so they
        # say nothing about its health or speed.
        reached = [(o, d) for o, d in calls if o not in ("denied", "rejected")]
        durations = sorted(d for _, d in reached)
        failed = sum(1 for o, _ in reached if o != "success")
        servers.append(
            {
                "server": slug,
                "calls": len(reached),
                "refused": len(calls) - len(reached),
                "errors": failed,
                "error_rate": round(failed / len(reached), 3) if reached else 0.0,
                "p50_ms": _percentile(durations, 0.5) if durations else None,
                "p95_ms": _percentile(durations, 0.95) if durations else None,
            }
        )
    return {"hours": hours, "servers": servers}


@router.get("/tools")
async def my_tools(user: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    """The tools the signed-in user can call: the same list their API key would get from the MCP endpoint."""
    await state.aggregator.refresh()
    entries = state.aggregator.list_tools(_chat_principal(user))
    return {
        "tools": [
            {
                "name": e.name,
                "server": e.server_name,
                "description": (e.tool.description or "").strip()[:300],
                "admin_only": e.admin_only,
            }
            for e in entries
        ]
    }


@router.post("/chat")
async def chat(body: ChatRequest, user: User = Depends(current_user), state: AppState = Depends(get_state)) -> StreamingResponse:
    """Run the agent and stream its progress as newline-delimited JSON events."""
    principal = _chat_principal(user)
    if (wait := state.chat_limiter.check(user.id)) is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"You are sending messages too quickly. Try again in {wait:.0f} s.",
            headers={"Retry-After": str(max(1, round(wait)))},
        )
    await state.aggregator.refresh()

    async def events() -> AsyncIterator[bytes]:
        async for event in run_agent(principal, body.messages, state.aggregator, state.llm, state.settings):
            yield (json.dumps(event, ensure_ascii=False) + "\n").encode()

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
