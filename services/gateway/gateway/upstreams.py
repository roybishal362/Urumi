"""The gateway's connections to upstream MCP servers.

The gateway is an MCP client of every registered server. Each enabled server
gets one `UpstreamConnection`, which owns a background task (its supervisor):

    connect -> list tools -> mark Connected -> re-list every few seconds
        ^                                              |
        |______ wait, with growing delay _____ on any failure: mark Unreachable

Tool calls from any number of users share that one connection; MCP requests
carry ids, so they do not block each other. Because every server has its own
task and its own state, a slow or dead server can only ever affect itself.

`UpstreamManager` keeps the set of running connections equal to the set of
enabled servers in the database. Several gateway replicas each converge on the
same registry without talking to each other: every change bumps a version
counter in the database, and each replica compares that counter with the one
it last acted on at the start of every request (and on a timer, as a fallback).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import __version__
from .config import Settings
from .db import Database, RegistryState, ToolPin, ToolPolicy, UpstreamServer, bump_registry_version, utcnow
from .security import CredentialCipher
from .ssrf import UnsafeUrlError, check_upstream_url

logger = logging.getLogger("gateway.upstreams")

MAX_TOOL_PAGES = 20
UrlCheck = Callable[[str], Awaitable[None]]


def tool_definition(tool: types.Tool) -> dict[str, Any]:
    """The parts of a tool a model reads or relies on. A change to any of them needs review."""
    return {
        "title": tool.title,
        "description": tool.description or "",
        "input_schema": tool.input_schema,
        "output_schema": tool.output_schema,
        "annotations": tool.annotations.model_dump(mode="json", exclude_none=True) if tool.annotations else None,
    }


def fingerprint(definition: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Outage:
    """A stretch of time a server was unreachable, kept after it recovers.

    Kubernetes replaces a killed pod within seconds, so an outage can be over
    before anyone looks at the dashboard. Keeping the last one means it is
    still visible, with how long it lasted and why.
    """

    started: datetime
    ended: datetime
    reason: str

    @property
    def seconds(self) -> float:
        return (self.ended - self.started).total_seconds()


class UpstreamUnavailable(Exception):
    """The server cannot be reached right now."""


class UpstreamTimeout(Exception):
    """The server did not answer a call in time."""


class UpstreamCallError(Exception):
    """The server answered the call with a protocol-level error."""


@dataclass(frozen=True)
class UpstreamSpec:
    """Everything needed to connect to one server. Held in memory only."""

    id: str
    slug: str
    name: str
    url: str
    auth_type: str
    auth_header: str | None
    credential: str | None
    revision: int
    # Set when the stored configuration cannot be used at all.
    config_error: str | None = None

    def headers(self) -> dict[str, str]:
        if not self.credential:
            return {}
        if self.auth_type == "bearer":
            return {"Authorization": f"Bearer {self.credential}"}
        if self.auth_type == "header" and self.auth_header:
            return {self.auth_header: self.credential}
        return {}


def _leaves(exc: BaseException) -> list[BaseException]:
    """Flatten nested exception groups (task groups raise them) to the real errors."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in _leaves(inner)]
    return [exc]


def describe_error(exc: BaseException) -> str:
    """One sentence a person can act on, for the dashboard and for error results."""
    leaves = _leaves(exc)
    # Prefer the most specific cause when several tasks failed together.
    ranked = sorted(leaves, key=lambda e: 0 if isinstance(e, (httpx2.HTTPError, UnsafeUrlError)) else 1)
    error = ranked[0]
    text = str(error)
    if isinstance(error, UnsafeUrlError):
        return f"URL not allowed: {text}"
    if isinstance(error, httpx2.ConnectTimeout):
        return "Connection timed out. The host did not respond."
    if isinstance(error, httpx2.ConnectError):
        lowered = text.lower()
        if any(hint in lowered for hint in ("getaddrinfo", "name or service", "name resolution", "nodename")):
            return "DNS lookup failed. The hostname does not resolve."
        return "Connection refused. Nothing is accepting connections at that address."
    if isinstance(error, httpx2.TimeoutException):
        return "Timed out waiting for the server to respond."
    if isinstance(error, httpx2.HTTPError):
        return f"Network error: {type(error).__name__}."
    if isinstance(error, TimeoutError):
        return "Timed out while connecting and listing tools."
    if isinstance(error, MCPError):
        return f"The server returned an MCP error: {error.message}"
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


class UpstreamConnection:
    def __init__(
        self,
        spec: UpstreamSpec,
        settings: Settings,
        url_check: UrlCheck,
        on_tools: Callable[["UpstreamConnection"], Awaitable[None]] | None = None,
    ) -> None:
        self.spec = spec
        self._settings = settings
        self._url_check = url_check
        self._on_tools = on_tools

        # Read by the dashboard and by the aggregator.
        self.status = "connecting"  # connecting | connected | unreachable
        self.error: str | None = None
        self.since: datetime = utcnow()
        self.last_ok: datetime | None = None
        self.tools: list[types.Tool] = []
        # Fingerprint of each tool's current definition, by upstream tool name.
        self.fingerprints: dict[str, str] = {}
        self.protocol_version: str | None = None
        self.server_version: str | None = None
        self.last_outage: Outage | None = None
        self.outage_count = 0

        self._client: Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._wake = asyncio.Event()
        self._first_attempt_done = asyncio.Event()

    # --- lifecycle ---

    def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name=f"upstream:{self.spec.slug}")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._client = None

    def refresh_now(self) -> None:
        """Skip the current wait: reconnect, or re-list tools, straight away."""
        self._wake.set()

    @property
    def supervised(self) -> bool:
        """False if the background task has ended, which it never should on its own."""
        return self._task is not None and not self._task.done()

    async def wait_first_attempt(self) -> None:
        await self._first_attempt_done.wait()

    # --- the supervisor ---

    async def _supervise(self) -> None:
        delay = 1.0
        while not self._stopping:
            was_connected = False
            try:
                if self.spec.config_error:
                    raise UnsafeUrlError(self.spec.config_error)
                await self._connect_and_watch()
            except asyncio.CancelledError:
                # Always passed on. A transport failure arrives as an exception
                # group, not as a bare cancellation, so this is someone stopping
                # the task. If it ever were the transport, the reconciler
                # notices the supervisor has ended and starts a new one.
                raise
            except BaseException as exc:  # noqa: BLE001 - exception groups from the transport land here
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                await self._mark_unreachable(describe_error(exc), cause=exc)
            finally:
                was_connected = self._client is not None
                self._client = None
                self._first_attempt_done.set()
            if self._stopping:
                return
            if was_connected:
                # It worked until just now, so retry quickly before backing off.
                delay = 1.0
            await self._sleep(delay)
            delay = min(delay * 2, self._settings.max_backoff)

    async def _connect_and_watch(self) -> None:
        settings = self._settings
        # Re-checked on every connect: DNS may answer differently than when the URL was saved.
        await self._url_check(self.spec.url)
        http = httpx2.AsyncClient(
            headers=self.spec.headers(),
            # The read timeout is deliberately above the per-call timeout: the
            # per-call limit is enforced per request, and an HTTP-level timeout
            # would tear down the whole connection for every caller.
            timeout=httpx2.Timeout(
                connect=settings.connect_timeout,
                read=settings.call_timeout + 30,
                write=30,
                pool=settings.connect_timeout,
            ),
        )
        client = Client(
            streamable_http_client(self.spec.url, http_client=http),
            # Negotiates the newest protocol the server offers and falls back
            # to the initialize handshake for older servers.
            mode="auto",
            read_timeout_seconds=settings.call_timeout,
            cache=None,
            client_info=types.Implementation(name="urumi-gateway", version=__version__),
        )
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(http)
            async with asyncio.timeout(settings.connect_timeout):
                await stack.enter_async_context(client)
                tools = await self._list_all_tools(client)

            self._client = client
            await self._set_tools(tools)
            self.protocol_version = client.protocol_version
            self.server_version = client.server_info.version if client.server_info else None
            self.last_ok = utcnow()
            if self.status != "connected":
                if self.status == "unreachable":
                    self.last_outage = Outage(started=self.since, ended=utcnow(), reason=self.error or "")
                    self.outage_count += 1
                    logger.info("upstream %s recovered after %.1fs", self.spec.slug, self.last_outage.seconds)
                logger.info("upstream %s connected: %d tools, protocol %s", self.spec.slug, len(tools), self.protocol_version)
                self.status, self.error, self.since = "connected", None, utcnow()
            self._first_attempt_done.set()

            while not self._stopping:
                await self._sleep(settings.refresh_interval)
                if self._stopping:
                    return
                # Doubles as the health check: if this fails, the exception
                # leaves the block and the supervisor marks the server unreachable.
                async with asyncio.timeout(settings.connect_timeout):
                    tools = await self._list_all_tools(client)
                await self._set_tools(tools)
                self.last_ok = utcnow()

    async def _set_tools(self, tools: list[types.Tool]) -> None:
        self.tools = tools
        self.fingerprints = {tool.name: fingerprint(tool_definition(tool)) for tool in tools}
        if self._on_tools is not None:
            try:
                await self._on_tools(self)
            except Exception:  # noqa: BLE001 - pinning trouble must not take the connection down
                logger.exception("could not record tool pins for %s", self.spec.slug)

    async def _list_all_tools(self, client: Client) -> list[types.Tool]:
        tools: list[types.Tool] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            page = await client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                break
        return tools

    async def _mark_unreachable(self, reason: str, cause: BaseException | None = None) -> None:
        # When the server answered HTTP but not MCP, the status code says why
        # far better than a generic protocol error does.
        if cause is not None and any(isinstance(leaf, MCPError) for leaf in _leaves(cause)):
            reason = await self._explain_http_failure() or reason
        if self.status != "unreachable" or self.error != reason:
            logger.warning("upstream %s unreachable: %s", self.spec.slug, reason)
            if self.status != "unreachable":
                self.since = utcnow()
        self.status, self.error = "unreachable", reason

    async def _explain_http_failure(self) -> str | None:
        body = {"jsonrpc": "2.0", "id": "probe", "method": "ping"}
        headers = {**self.spec.headers(), "Accept": "application/json, text/event-stream"}
        try:
            async with httpx2.AsyncClient(timeout=self._settings.connect_timeout) as http:
                response = await http.post(self.spec.url, json=body, headers=headers)
        except httpx2.HTTPError:
            return None
        code = response.status_code
        if code in (401, 403):
            return f"The server rejected the gateway's credential (HTTP {code})."
        if code == 404:
            return "No MCP endpoint at this URL (HTTP 404)."
        if code >= 500:
            return f"The server is failing (HTTP {code})."
        if 300 <= code < 400:
            return f"The URL redirects elsewhere (HTTP {code}); redirects to other hosts are not followed."
        return None

    async def _sleep(self, seconds: float) -> None:
        """Wait, but return early when asked to refresh or stop."""
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # --- used by the aggregator ---

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> types.CallToolResult:
        client = self._client
        if client is None:
            raise UpstreamUnavailable(self.error or "Not connected yet.")
        try:
            async with asyncio.timeout(timeout):
                return await client.call_tool(name, arguments, read_timeout_seconds=timeout + 5)
        except TimeoutError as exc:
            raise UpstreamTimeout(f"No answer within {timeout:g} seconds.") from exc
        except MCPError as exc:
            if exc.code == types.REQUEST_TIMEOUT:
                raise UpstreamTimeout(f"No answer within {timeout:g} seconds.") from exc
            if exc.code == types.CONNECTION_CLOSED:
                self.refresh_now()
                raise UpstreamUnavailable("Connection to the server was lost during the call.") from exc
            raise UpstreamCallError(exc.message) from exc
        except (httpx2.HTTPError, OSError, RuntimeError) as exc:
            self.refresh_now()
            raise UpstreamUnavailable(describe_error(exc)) from exc


@dataclass(frozen=True)
class ServerRecord:
    """A registered server as the rest of the gateway sees it."""

    id: str
    slug: str
    name: str
    url: str
    auth_type: str
    auth_header: str | None
    has_credential: bool
    enabled: bool
    revision: int


class UpstreamManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        cipher: CredentialCipher,
        url_check: UrlCheck | None = None,
    ) -> None:
        self._settings = settings
        self._db = db
        self._cipher = cipher
        self._url_check: UrlCheck = url_check or (lambda url: check_upstream_url(url, settings))
        self._connections: dict[str, UpstreamConnection] = {}
        self._records: dict[str, ServerRecord] = {}
        self._admin_only: set[tuple[str, str]] = set()
        # Approved fingerprint of each pinned tool, by (server id, tool name).
        self._pins: dict[tuple[str, str], str] = {}
        # The registry version this replica last reconciled against.
        self._seen_version = -1
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.reconcile()
        self._task = asyncio.create_task(self._reconcile_forever(), name="upstream-reconciler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await asyncio.gather(*(c.stop() for c in self._connections.values()), return_exceptions=True)
        self._connections.clear()

    async def wait_until_settled(self, timeout: float) -> None:
        """Wait until every server has finished its first connection attempt."""
        waits = [c.wait_first_attempt() for c in self._connections.values()]
        if waits:
            await asyncio.wait([asyncio.ensure_future(w) for w in waits], timeout=timeout)

    async def _reconcile_forever(self) -> None:
        while True:
            await asyncio.sleep(self._settings.reconcile_interval)
            try:
                await self.reconcile()
            except Exception:  # noqa: BLE001 - keep reconciling through a database blip
                logger.exception("reconcile failed; will retry")

    async def _registry_version(self) -> int:
        async with self._db.session() as session:
            state = await session.get(RegistryState, 1)
        return state.version if state else 0

    async def ensure_current(self) -> None:
        """Catch up with the registry if another replica (or this one) changed it.

        One primary-key read per request. It is what makes "disable" and
        "remove" take effect everywhere at once when several replicas run.
        """
        version = await self._registry_version()
        if version == self._seen_version:
            return
        async with self._lock:
            if version != self._seen_version:  # nobody caught up while we waited
                await self._reconcile()

    async def reconcile(self) -> None:
        """Make the running connections match the registry in the database."""
        async with self._lock:
            await self._reconcile()

    async def _reconcile(self) -> None:
        async with self._db.session() as session:
            state = await session.get(RegistryState, 1)
            rows = (await session.execute(select(UpstreamServer))).scalars().all()
            policies = (await session.execute(select(ToolPolicy).where(ToolPolicy.admin_only.is_(True)))).scalars().all()
            pins = (await session.execute(select(ToolPin.server_id, ToolPin.tool_name, ToolPin.fingerprint))).all()

        self._pins = {(server_id, tool_name): fp for server_id, tool_name, fp in pins}
        self._seen_version = state.version if state else 0
        self._admin_only = {(p.server_id, p.tool_name) for p in policies}
        self._records = {
            row.id: ServerRecord(
                id=row.id,
                slug=row.slug,
                name=row.name,
                url=row.url,
                auth_type=row.auth_type,
                auth_header=row.auth_header,
                has_credential=bool(row.credential_enc),
                enabled=row.enabled,
                revision=row.revision,
            )
            for row in rows
        }
        wanted = {row.id: row for row in rows if row.enabled}

        # Stop connections whose server was disabled, removed or edited,
        # and any whose supervisor has died, so it gets a fresh one below.
        for server_id, connection in list(self._connections.items()):
            row = wanted.get(server_id)
            if row is None or row.revision != connection.spec.revision or not connection.supervised:
                await connection.stop()
                del self._connections[server_id]
                logger.info("upstream %s stopped", connection.spec.slug)

        for server_id, row in wanted.items():
            if server_id not in self._connections:
                connection = UpstreamConnection(self._spec(row), self._settings, self._url_check, self._pin_new_tools)
                connection.start()
                self._connections[server_id] = connection

    def _spec(self, row: UpstreamServer) -> UpstreamSpec:
        credential, config_error = None, None
        if row.credential_enc:
            try:
                credential = self._cipher.decrypt(row.credential_enc)
            except ValueError as exc:
                config_error = f"{exc}. Re-enter the credential."
        return UpstreamSpec(
            id=row.id,
            slug=row.slug,
            name=row.name,
            url=row.url,
            auth_type=row.auth_type,
            auth_header=row.auth_header,
            credential=credential,
            revision=row.revision,
            config_error=config_error,
        )

    async def check_url(self, url: str) -> None:
        """Raise UnsafeUrlError if `url` may not be used as an upstream."""
        await self._url_check(url)

    # --- read access for the aggregator and the API ---

    def records(self) -> list[ServerRecord]:
        return sorted(self._records.values(), key=lambda r: r.slug)

    def record(self, server_id: str) -> ServerRecord | None:
        return self._records.get(server_id)

    def connection(self, server_id: str) -> UpstreamConnection | None:
        return self._connections.get(server_id)

    def connections(self) -> list[UpstreamConnection]:
        return list(self._connections.values())

    def is_admin_only(self, server_id: str, tool_name: str) -> bool:
        return (server_id, tool_name) in self._admin_only

    # --- tool pinning ---

    def changed_since_approval(self, server_id: str, tool_name: str, current_fingerprint: str | None) -> bool:
        """True if the tool's definition differs from the one that was approved."""
        approved = self._pins.get((server_id, tool_name))
        return approved is not None and current_fingerprint is not None and approved != current_fingerprint

    async def _pin_new_tools(self, connection: UpstreamConnection) -> None:
        """Pin tools seen for the first time. Changed tools are left alone: they wait for an admin."""
        server_id = connection.spec.id
        new = [tool for tool in connection.tools if (server_id, tool.name) not in self._pins]
        if not new:
            return
        async with self._db.session() as session:
            for tool in new:
                definition = tool_definition(tool)
                session.add(
                    ToolPin(
                        server_id=server_id,
                        tool_name=tool.name,
                        fingerprint=fingerprint(definition),
                        definition=json.dumps(definition, sort_keys=True),
                        approved_by="first seen",
                    )
                )
            try:
                await session.commit()
            except IntegrityError:
                # Another replica pinned them a moment earlier; its pins win.
                await session.rollback()
                rows = (await session.execute(select(ToolPin).where(ToolPin.server_id == server_id))).scalars().all()
                for row in rows:
                    self._pins[(server_id, row.tool_name)] = row.fingerprint
                return
        for tool in new:
            self._pins[(server_id, tool.name)] = connection.fingerprints[tool.name]

    async def approve_tool(self, server_id: str, tool_name: str, approver: str) -> bool:
        """Accept the tool's current definition as the approved one. False if the tool is not there."""
        connection = self._connections.get(server_id)
        tool = next((t for t in connection.tools if t.name == tool_name), None) if connection else None
        if tool is None:
            return False
        definition = tool_definition(tool)
        async with self._db.session() as session:
            pin = await session.get(ToolPin, (server_id, tool_name))
            if pin is None:
                pin = ToolPin(server_id=server_id, tool_name=tool_name)
                session.add(pin)
            pin.fingerprint = fingerprint(definition)
            pin.definition = json.dumps(definition, sort_keys=True)
            pin.approved_by = approver
            pin.approved_at = utcnow()
            await bump_registry_version(session)
            await session.commit()
        await self.reconcile()
        return True

    async def approved_definition(self, server_id: str, tool_name: str) -> dict[str, Any] | None:
        async with self._db.session() as session:
            pin = await session.get(ToolPin, (server_id, tool_name))
        return json.loads(pin.definition) if pin else None
