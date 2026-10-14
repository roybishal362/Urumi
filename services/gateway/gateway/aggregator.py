"""Tool aggregation, routing, permissions and the audit trail.

This is the one path every tool call takes, whether it comes from an external
MCP client or from the dashboard's chat agent:

    name -> which server and which of its tools -> may this user call it?
         -> call the server with a timeout -> write the audit row -> result

Tool names
----------
Every tool is exposed as  <server slug>__<tool name>,  for example
woo__list_orders. Slugs are unique, so two servers can both have a `get_rates`
tool and they simply become fx__get_rates and shipping__get_rates. The prefix
is always added, not only when names clash: a tool's name then never changes
because some other server was added later, and the name itself tells a model
(and a person reading the audit log) which system the tool acts on.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import mcp_types as types

from .config import Settings
from .db import AuditLog, Database
from .identity import Principal
from .upstreams import UpstreamCallError, UpstreamManager, UpstreamTimeout, UpstreamUnavailable

logger = logging.getLogger("gateway.aggregator")

SEPARATOR = "__"
# The strictest name rule among the clients we serve (LLM function-calling APIs).
MAX_NAME_LENGTH = 64
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")
MAX_DESCRIPTION_LENGTH = 1500
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ToolNotFound(Exception):
    """No such tool for this user. Also raised for tools they may not use, so a
    member cannot tell a hidden tool from one that does not exist."""


def exposed_name(slug: str, tool_name: str) -> str:
    """The name a tool is published under. Stable for a given (slug, tool name)."""
    safe = _UNSAFE_NAME_CHARS.sub("_", tool_name)
    name = f"{slug}{SEPARATOR}{safe}"
    if safe != tool_name or len(name) > MAX_NAME_LENGTH:
        # The name had to be altered or shortened; a short hash of the original
        # keeps two different tools from collapsing into the same name.
        digest = hashlib.sha1(tool_name.encode()).hexdigest()[:6]
        name = f"{name[: MAX_NAME_LENGTH - 7]}_{digest}"
    return name


def split_name(name: str) -> tuple[str, str]:
    """Best-effort (slug, tool name) for a name that is not in the catalog. Audit use only."""
    slug, separator, tool = name.partition(SEPARATOR)
    return (slug[:16], tool) if separator else ("", name)


def describe_for_clients(server_name: str, description: str | None) -> str:
    """Tool descriptions come from the upstream and are untrusted text that a
    model will read. Label where it came from, strip control characters and cap
    the length. This limits, but cannot remove, the room for a hostile
    description to steer a model."""
    text = _CONTROL_CHARS.sub(" ", description or "").strip()
    if len(text) > MAX_DESCRIPTION_LENGTH:
        text = text[:MAX_DESCRIPTION_LENGTH].rstrip() + "..."
    return f"[{server_name}] {text}" if text else f"[{server_name}]"


@dataclass(frozen=True)
class CatalogEntry:
    name: str  # exposed name, e.g. woo__list_orders
    server_id: str
    server_slug: str
    server_name: str
    tool: types.Tool  # the upstream's own definition
    admin_only: bool
    available: bool  # False while the server is unreachable
    # The server changed this tool after it was approved. Withheld until an admin reviews it.
    changed: bool = False

    def as_tool(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            title=self.tool.title,
            description=describe_for_clients(self.server_name, self.tool.description),
            input_schema=self.tool.input_schema,
            output_schema=self.tool.output_schema,
            annotations=self.tool.annotations,
        )


@dataclass(frozen=True)
class CallOutcome:
    entry: CatalogEntry
    result: types.CallToolResult
    outcome: str  # success | error | timeout | unreachable
    error: str | None
    duration_ms: int


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)


def _first_text(result: types.CallToolResult) -> str:
    for block in result.content:
        if isinstance(block, types.TextContent):
            return block.text
    return ""


class Aggregator:
    def __init__(self, manager: UpstreamManager, db: Database, settings: Settings) -> None:
        self._manager = manager
        self._db = db
        self._settings = settings

    async def refresh(self) -> None:
        """Pick up registry changes made on another replica. Called once at the
        start of each request, before listing or calling tools."""
        await self._manager.ensure_current()

    # --- the catalog ---

    def catalog(self) -> dict[str, CatalogEntry]:
        """Every tool of every enabled server, keyed by exposed name.

        Built from each connection's cached tool list, so it never waits on an
        upstream: listing tools costs the same whether servers are fast, slow
        or dead. Unreachable servers keep their last known tools in the catalog
        (marked unavailable) so that a call to one gets a precise error.
        """
        entries: dict[str, CatalogEntry] = {}
        for connection in self._manager.connections():
            spec = connection.spec
            for tool in connection.tools:
                name = exposed_name(spec.slug, tool.name)
                if name in entries:
                    continue  # a server listing the same tool twice
                entries[name] = CatalogEntry(
                    name=name,
                    server_id=spec.id,
                    server_slug=spec.slug,
                    server_name=spec.name,
                    tool=tool,
                    admin_only=self._manager.is_admin_only(spec.id, tool.name),
                    available=connection.status == "connected",
                    changed=self._manager.changed_since_approval(spec.id, tool.name, connection.fingerprints.get(tool.name)),
                )
        return entries

    @staticmethod
    def _allowed(principal: Principal, entry: CatalogEntry) -> bool:
        return principal.is_admin or not entry.admin_only

    def list_tools(self, principal: Principal) -> list[CatalogEntry]:
        """The tools this user can call right now, in a stable order."""
        visible = [e for e in self.catalog().values() if e.available and not e.changed and self._allowed(principal, e)]
        return sorted(visible, key=lambda e: e.name)

    # --- calling ---

    async def call_tool(self, principal: Principal, name: str, arguments: dict[str, Any] | None) -> CallOutcome:
        started = time.perf_counter()
        entry = self.catalog().get(name)

        if entry is None or not self._allowed(principal, entry) or entry.changed:
            slug, tool = (entry.server_slug, entry.tool.name) if entry else split_name(name)
            if entry is None:
                # Refused by the gateway itself (disabled, removed or never existed):
                # not the server's failure, so not counted against its error rate.
                outcome, error = "denied", "Unknown tool: disabled, removed, or never existed."
            elif entry.changed:
                outcome, error = "denied", "Tool definition changed since it was approved; waiting for admin review."
            else:
                outcome, error = "denied", "Not permitted for this role."
            await self._audit(principal, server_slug=slug, tool=tool, duration_ms=_elapsed_ms(started), outcome=outcome, error=error)
            raise ToolNotFound(name)

        connection = self._manager.connection(entry.server_id)
        outcome, error = "success", None
        try:
            if connection is None:
                raise UpstreamUnavailable("The server was disabled or removed.")
            upstream = await connection.call_tool(entry.tool.name, arguments or {}, self._settings.call_timeout)
            # Rebuilt rather than passed through, so nothing but the content of
            # the answer travels from the upstream to the caller.
            result = types.CallToolResult(
                content=upstream.content,
                structured_content=upstream.structured_content,
                is_error=upstream.is_error,
            )
            if upstream.is_error:
                outcome, error = "error", _first_text(upstream)[:500] or "The tool reported an error."
        except UpstreamTimeout as exc:
            outcome, error = "timeout", str(exc)
            result = _error_result(f"The '{entry.server_name}' server timed out. {exc} Other servers are unaffected.")
        except UpstreamUnavailable as exc:
            outcome, error = "unreachable", str(exc)
            result = _error_result(f"The '{entry.server_name}' server is unreachable. {exc} Other servers are unaffected.")
        except UpstreamCallError as exc:
            outcome, error = "error", str(exc)
            result = _error_result(f"The '{entry.server_name}' server rejected the call: {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad upstream must never take the gateway down
            logger.exception("unexpected failure calling %s", name)
            outcome, error = "error", f"{type(exc).__name__}: {exc}"
            result = _error_result(f"The call to '{entry.server_name}' failed unexpectedly.")

        duration_ms = _elapsed_ms(started)
        await self._audit(
            principal,
            server_slug=entry.server_slug,
            tool=entry.tool.name,
            duration_ms=duration_ms,
            outcome=outcome,
            error=error,
        )
        return CallOutcome(entry=entry, result=result, outcome=outcome, error=error, duration_ms=duration_ms)

    # --- audit ---

    async def record_rejection(self, *, user_id: str | None, email: str, key_prefix: str | None, tool: str, reason: str) -> None:
        """Audit a call that was refused before it reached a tool (revoked key, deactivated user)."""
        slug, bare = split_name(tool)
        await self._write_audit(
            AuditLog(
                user_id=user_id,
                user_email=email,
                key_prefix=key_prefix,
                source="mcp",
                server_slug=slug,
                tool=bare[:200],
                duration_ms=0,
                outcome="rejected",
                error=reason,
            )
        )

    async def _audit(
        self, principal: Principal, *, server_slug: str, tool: str, duration_ms: int, outcome: str, error: str | None
    ) -> None:
        await self._write_audit(
            AuditLog(
                user_id=principal.user_id,
                user_email=principal.email,
                key_prefix=principal.key_prefix,
                source=principal.source,
                server_slug=server_slug[:16],
                tool=tool[:200],
                duration_ms=duration_ms,
                outcome=outcome,
                error=error[:2000] if error else None,
            )
        )

    async def _write_audit(self, row: AuditLog) -> None:
        # The audit log records who called what and how it went. It does not
        # store arguments or results, which can contain customer data.
        try:
            async with self._db.session() as session:
                session.add(row)
                await session.commit()
        except Exception:  # noqa: BLE001
            # Losing an audit row is logged loudly but does not fail the call.
            logger.exception("could not write audit row for %s/%s", row.server_slug, row.tool)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
