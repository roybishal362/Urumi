"""The gateway's own MCP endpoint: the one URL team members give their MCP client.

To its clients the gateway is an ordinary MCP server over Streamable HTTP.
`tools/list` answers with the tools of every enabled upstream this user may
use; `tools/call` is routed to the upstream that owns the tool.

The endpoint is stateless: no session ids, one JSON response per POST. A
request can be served by any replica, and restarting the gateway never strands
a client mid-session. The cost is that the server cannot push "tool list
changed" notifications; clients see changes the next time they list tools.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import mcp_types as types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from starlette.responses import JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from . import __version__
from .aggregator import Aggregator, ToolNotFound
from .db import Database
from .identity import KeyRejection, Principal, authenticate_api_key
from .ratelimit import RateLimiter

logger = logging.getLogger("gateway.mcp")

PRINCIPAL_SCOPE_KEY = "urumi.principal"
_MAX_PEEK_BYTES = 64 * 1024

_CORS_HEADERS = {
    # Callers authenticate with a bearer key, never with cookies, so allowing
    # any origin gives a web page nothing it could not do with the key anyway.
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Max-Age": "600",
}


def _principal(ctx: ServerRequestContext[Any, Any]) -> Principal:
    # The HTTP request that carried this MCP message; the endpoint below put
    # the authenticated caller into its scope before handing over to the SDK.
    principal = ctx.request.scope.get(PRINCIPAL_SCOPE_KEY) if ctx.request is not None else None
    if principal is None:
        raise MCPError(types.INVALID_REQUEST, "Not authenticated.")
    return principal


def build_mcp_server(aggregator: Aggregator) -> Server[Any]:
    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[entry.as_tool() for entry in aggregator.list_tools(_principal(ctx))])

    async def call_tool(ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            outcome = await aggregator.call_tool(_principal(ctx), params.name, params.arguments)
        except ToolNotFound:
            raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {params.name}") from None
        # Upstream failures (unreachable, timeout) come back as is_error results
        # with a message, so a model can read what went wrong and carry on.
        return outcome.result

    return Server(
        "urumi-gateway",
        version=__version__,
        title="Urumi team MCP gateway",
        instructions=(
            "One endpoint for every MCP server your team has connected. Tool names start with the "
            "server they belong to, for example woo__list_orders."
        ),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _bearer_token(scope: Scope) -> str | None:
    header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _jsonrpc_error(status: int, message: str, headers: dict[str, str] | None = None) -> Response:
    body = {"jsonrpc": "2.0", "id": None, "error": {"code": types.INVALID_REQUEST, "message": message}}
    return JSONResponse(body, status_code=status, headers={**_CORS_HEADERS, **(headers or {})})


async def _peek_attempted_call(receive: Receive) -> str:
    """What a refused request was trying to do, for the audit log."""
    body = b""
    while len(body) <= _MAX_PEEK_BYTES:
        message = await receive()
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    try:
        payload = json.loads(body)
    except ValueError:
        return "(unreadable request)"
    if not isinstance(payload, dict):
        return "(unreadable request)"
    method = str(payload.get("method", ""))[:60]
    params = payload.get("params")
    if method == "tools/call" and isinstance(params, dict) and isinstance(params.get("name"), str):
        return params["name"]
    return f"({method or 'unknown'})"


class McpEndpoint:
    """ASGI app mounted at /mcp: authenticates the caller, then lets the MCP SDK serve the request."""

    def __init__(self, aggregator: Aggregator, db: Database, limiter: RateLimiter | None = None) -> None:
        self._aggregator = aggregator
        self._db = db
        self._limiter = limiter or RateLimiter(0)
        self.sessions = StreamableHTTPSessionManager(
            app=build_mcp_server(aggregator), json_response=True, stateless=True
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = scope["method"]
        if method == "OPTIONS":
            await Response(status_code=204, headers=_CORS_HEADERS)(scope, receive, send)
            return
        if method != "POST":
            # GET would open a server-to-client event stream and DELETE would
            # end a session. A stateless endpoint has neither, and the spec
            # says to answer 405 in that case.
            await _jsonrpc_error(405, "This endpoint accepts POST only.", {"Allow": "POST, OPTIONS"})(scope, receive, send)
            return

        token = _bearer_token(scope)
        caller = await authenticate_api_key(self._db, token) if token else KeyRejection("An API key is required.")
        if isinstance(caller, KeyRejection):
            if caller.user_id is not None:
                # A key that used to work: record the refused attempt against its owner.
                await self._aggregator.record_rejection(
                    user_id=caller.user_id,
                    email=caller.email,
                    key_prefix=caller.key_prefix,
                    tool=await _peek_attempted_call(receive),
                    reason=caller.reason,
                )
            challenge = f'Bearer error="invalid_token", error_description="{caller.reason}"'
            await _jsonrpc_error(401, caller.reason, {"WWW-Authenticate": challenge})(scope, receive, send)
            return

        wait = self._limiter.check(caller.key_prefix or caller.user_id)
        if wait is not None:
            await _jsonrpc_error(
                429, f"Too many requests for this API key. Retry in {wait:.0f} s.", {"Retry-After": str(max(1, round(wait)))}
            )(scope, receive, send)
            return

        scope[PRINCIPAL_SCOPE_KEY] = caller
        # A server disabled a moment ago through another replica must already
        # be gone for this request.
        await self._aggregator.refresh()

        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"access-control-allow-origin", b"*"))
                message = {**message, "headers": headers}
            await send(message)

        await self.sessions.handle_request(scope, receive, send_with_cors)
