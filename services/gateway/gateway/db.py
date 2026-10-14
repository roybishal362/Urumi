"""Database models and session setup.

PostgreSQL in the cluster; SQLite is also supported so the test suite runs
without a database server. Tables are created at startup. A project that had
to evolve this schema in place would add a migration tool (Alembic).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, TypeDecorator, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class UTCDateTime(TypeDecorator[datetime]):
    """A timestamp that is always timezone-aware UTC in Python.

    PostgreSQL keeps the offset; SQLite drops it, so it is put back on read.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    # "admin" manages servers and members; "member" uses tools.
    role: Mapped[str] = mapped_column(String(16), default="member")
    # Empty until the person accepts their invite and chooses a password.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # SHA-256 of the one-time invite token. The token itself is shown once.
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80), default="")
    # First characters of the key, kept so people can tell their keys apart.
    prefix: Mapped[str] = mapped_column(String(24))
    # SHA-256 of the full key. The key itself is never stored.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Set when an admin issued the key for someone else: who did it.
    issued_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class UpstreamServer(Base):
    __tablename__ = "upstream_servers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # Short unique name. It prefixes every tool this server exposes.
    slug: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    url: Mapped[str] = mapped_column(String(500))
    # "none", "bearer" (Authorization: Bearer ...) or "header" (a named header).
    auth_type: Mapped[str] = mapped_column(String(16), default="none")
    auth_header: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # The credential, encrypted. Decrypted only in memory, only to call the upstream.
    credential_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Bumped on every change, so each replica can tell its connection is stale.
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ToolPolicy(Base):
    """Marks one tool of one server as usable by admins only."""

    __tablename__ = "tool_policies"

    server_id: Mapped[str] = mapped_column(ForeignKey("upstream_servers.id", ondelete="CASCADE"), primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(200), primary_key=True)
    admin_only: Mapped[bool] = mapped_column(Boolean, default=True)


class ToolPin(Base):
    """The approved definition of one tool, so a later change can be caught.

    A tool is pinned the first time the gateway sees it on a server an admin
    added. If the server later changes the tool's description or inputs (the
    "rug pull": harmless when approved, hostile later), the tool is withheld
    from everyone until an admin reviews the change and approves it.
    """

    __tablename__ = "tool_pins"

    server_id: Mapped[str] = mapped_column(ForeignKey("upstream_servers.id", ondelete="CASCADE"), primary_key=True)
    tool_name: Mapped[str] = mapped_column(String(200), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    # The approved definition as JSON, so an admin can compare it with the new one.
    definition: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str] = mapped_column(String(255))
    approved_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RegistryState(Base):
    """A single row holding a counter that is bumped whenever the server
    registry changes (a server added, edited, disabled or removed, or a tool
    policy changed).

    Every gateway replica compares it with the value it last acted on before
    serving a request, so a change made through one replica takes effect on
    all of them on their next request, not on their next polling interval.
    """

    __tablename__ = "registry_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, default=0)


async def bump_registry_version(session: AsyncSession) -> None:
    """Mark the registry as changed. Call inside the transaction that makes the change."""
    state = await session.get(RegistryState, 1)
    if state is None:
        session.add(RegistryState(id=1, version=1))
    else:
        state.version += 1


class SeedRecord(Base):
    """Remembers which seed servers were already created.

    Seeding happens once per slug. After that the dashboard owns the server:
    an admin who edits or removes it does not see it come back on restart.
    """

    __tablename__ = "seed_records"

    slug: Mapped[str] = mapped_column(String(16), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuditLog(Base):
    """One row per tool call, whoever made it and however it ended."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    # Copied rather than joined, so the log still reads correctly after a
    # member or a server is removed.
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    user_email: Mapped[str] = mapped_column(String(255), default="")
    key_prefix: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # "mcp" for an external client using an API key, "chat" for the dashboard agent.
    source: Mapped[str] = mapped_column(String(8))
    server_slug: Mapped[str] = mapped_column(String(16), default="", index=True)
    tool: Mapped[str] = mapped_column(String(200), default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    # success | error | timeout | unreachable: the call reached (or tried to reach) the server.
    # denied: refused by the gateway (unknown, withheld or admin-only tool).
    # rejected: refused before that, because the API key or account is no longer valid.
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


def _add_missing_columns(sync_conn) -> None:
    """Additive schema changes for existing installs.

    `create_all` creates missing tables but never alters existing ones. A new
    nullable column added in a later version is therefore added here, so an
    upgraded gateway starts against its old database. Anything beyond adding
    nullable columns (renames, type changes) would need a migration tool.
    """
    inspector = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing and column.nullable:
                column_type = column.type.compile(dialect=sync_conn.dialect)
                sync_conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {column_type}'))


# Any fixed number; replicas agree on it to take the same lock.
_STARTUP_LOCK_KEY = 72_826_354


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def startup_lock(self) -> AsyncIterator[None]:
        """Let one replica at a time run the startup steps.

        Two replicas starting together would otherwise both try to create the
        same tables, and PostgreSQL rejects the second CREATE TABLE. An advisory
        lock makes the others wait until the first has finished. SQLite (tests)
        has one process and needs no lock.
        """
        if self.engine.dialect.name != "postgresql":
            yield
            return
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _STARTUP_LOCK_KEY})
            try:
                yield
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _STARTUP_LOCK_KEY})

    async def create_tables(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_add_missing_columns)

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()
