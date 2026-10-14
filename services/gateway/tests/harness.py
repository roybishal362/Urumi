"""Test harness: a real gateway and real upstream MCP servers, all on localhost.

Nothing is mocked except the LLM provider. Upstreams are MCP servers started
on free ports; the gateway is the real FastAPI app served by uvicorn with a
SQLite file as its database. Tests talk to it the way users do: the REST API
with a session cookie, and the MCP endpoint with an API key.
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.responses import JSONResponse

from gateway.config import Settings
from gateway.main import create_app

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "correct horse battery"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Every server a test starts, so conftest.py can stop any that a failing test left running.
RUNNING: set["RunningServer"] = set()


class RunningServer:
    """An ASGI app served by uvicorn inside the test's event loop."""

    # Upstreams are killed outright, like a pod being deleted. The gateway is
    # asked to shut down gracefully so its own shutdown path is exercised.
    graceful = False

    def __init__(self, app: Any, port: int | None = None) -> None:
        self.port = port or free_port()
        self._app = app
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> "RunningServer":
        config = uvicorn.Config(
            self._app, host="127.0.0.1", port=self.port, log_level="critical", lifespan="on", timeout_graceful_shutdown=3
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        RUNNING.add(self)
        while not self._server.started:
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.02)
        return self

    async def stop(self) -> None:
        RUNNING.discard(self)
        if self._server is not None and self._task is not None:
            self._server.should_exit = True
            self._server.force_exit = not self.graceful
            await asyncio.gather(self._task, return_exceptions=True)
            self._server = self._task = None


class Upstream(RunningServer):
    """A small MCP server. `seen_headers` records what the gateway sent it."""

    def __init__(self, name: str, *, bearer: str | None = None, header: tuple[str, str] | None = None, port: int | None = None) -> None:
        self.name = name
        self.seen_headers: list[dict[str, str]] = []
        # Changing this and restarting simulates a server rewriting a tool's description.
        self.echo_description = "Echo the text back."
        self._bearer, self._header = bearer, header
        super().__init__(self._build(), port)

    def _build(self) -> Any:
        name = self.name
        mcp = MCPServer(name, version="1.0")

        @mcp.tool()
        async def get_rates() -> str:
            """Rates from this server."""
            return f"rates from {name}"

        async def echo(text: str) -> str:
            return f"{name} says {text}"

        mcp.tool(name="echo", description=self.echo_description)(echo)

        @mcp.tool()
        async def wait(seconds: float) -> str:
            """Wait, then answer."""
            await asyncio.sleep(seconds)
            return "done waiting"

        @mcp.tool()
        async def fail() -> str:
            """Always reports an error."""
            raise ToolError("this tool always fails")

        inner = mcp.streamable_http_app(json_response=True, stateless_http=True, host="0.0.0.0")

        async def app(scope, receive, send):
            if scope["type"] == "http":
                headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
                self.seen_headers.append(headers)
                if self._bearer and headers.get("authorization") != f"Bearer {self._bearer}":
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                    return
                if self._header and headers.get(self._header[0].lower()) != self._header[1]:
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                    return
            await inner(scope, receive, send)

        return app

    async def restart(self) -> None:
        """Come back on the same port, as a restarted pod would behind its Service."""
        await self.stop()
        self._app = self._build()
        await self.start()


class FakeLLM:
    """Stands in for the model provider. Give it the assistant messages to return, in order."""

    def __init__(self) -> None:
        self.replies: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        if not self.replies:
            return httpx2.Response(500, json={"error": {"message": "FakeLLM has no reply queued"}})
        reply = self.replies.pop(0)
        if isinstance(reply, httpx2.Response):  # a provider error, queued as-is
            return reply
        return httpx2.Response(200, json={"choices": [{"message": reply}]})

    @staticmethod
    def rate_limited(retry_after: str = "0.1") -> httpx2.Response:
        return httpx2.Response(429, json={"error": {"message": "Rate limit reached for tokens per minute"}}, headers={"retry-after": retry_after})

    def tool_call(self, name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
        }


class Gateway:
    def __init__(self, server: RunningServer, settings: Settings, llm: FakeLLM) -> None:
        self.server = server
        self.settings = settings
        self.llm = llm
        self.url = server.url
        self.app = server._app  # noqa: SLF001
        self.state = self.app.state.gateway

    # --- REST, as a dashboard user ---

    async def login(self, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD) -> httpx2.AsyncClient:
        http = httpx2.AsyncClient(base_url=self.url, timeout=20)
        response = await http.post("/api/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        return http

    async def add_server(self, admin: httpx2.AsyncClient, slug: str, upstream: Upstream, **extra: Any) -> dict[str, Any]:
        body = {"name": slug.capitalize(), "slug": slug, "url": f"{upstream.url}/mcp", **extra}
        response = await admin.post("/api/servers", json=body)
        assert response.status_code == 201, response.text
        return response.json()["server"]

    async def servers(self, http: httpx2.AsyncClient) -> dict[str, dict[str, Any]]:
        response = await http.get("/api/servers")
        assert response.status_code == 200, response.text
        return {s["slug"]: s for s in response.json()["servers"]}

    async def wait_for_status(self, http: httpx2.AsyncClient, slug: str, status: str, timeout: float = 10.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            server = (await self.servers(http)).get(slug)
            if server and server["status"] == status:
                return server
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"{slug} never became {status}; last seen: {server}")
            await asyncio.sleep(0.1)

    async def new_key(self, http: httpx2.AsyncClient, **body: Any) -> dict[str, Any]:
        response = await http.post("/api/keys", json={"name": "test", **body})
        assert response.status_code == 201, response.text
        return response.json()

    async def invite(self, admin: httpx2.AsyncClient, email: str, password: str = "another long password") -> httpx2.AsyncClient:
        """Invite a member, accept the invite, and return their signed-in client."""
        response = await admin.post("/api/members/invite", json={"email": email, "role": "member"})
        assert response.status_code == 201, response.text
        token = response.json()["invite_url"].split("token=")[1]
        http = httpx2.AsyncClient(base_url=self.url, timeout=20)
        accepted = await http.post("/api/auth/accept-invite", json={"token": token, "name": "Member", "password": password})
        assert accepted.status_code == 200, accepted.text
        return http

    async def audit(self, http: httpx2.AsyncClient, **params: Any) -> list[dict[str, Any]]:
        response = await http.get("/api/audit", params=params)
        assert response.status_code == 200, response.text
        return response.json()["entries"]

    async def chat(self, http: httpx2.AsyncClient, text: str) -> list[dict[str, Any]]:
        response = await http.post("/api/chat", json={"messages": [{"role": "user", "content": text}]})
        assert response.status_code == 200, response.text
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]

    # --- MCP, as an external client with an API key ---

    @asynccontextmanager
    async def mcp(self, api_key: str, mode: str = "auto"):
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
        async with http, Client(
            streamable_http_client(f"{self.url}/mcp", http_client=http), mode=mode, read_timeout_seconds=20, cache=None
        ) as client:
            yield client

    async def raw_mcp(self, api_key: str | None, method: str, params: dict[str, Any] | None = None) -> httpx2.Response:
        """One hand-written JSON-RPC request in the 2025 handshake-era wire format."""
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        async with httpx2.AsyncClient(timeout=20) as http:
            return await http.post(f"{self.url}/mcp", json=body, headers=headers)


@asynccontextmanager
async def running_gateway(**overrides: Any):
    """Start a gateway with a fresh database. Yields a `Gateway`."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        defaults: dict[str, Any] = dict(
            database_url=f"sqlite+aiosqlite:///{Path(tmp, 'gateway.db').as_posix()}",
            session_secret="test-session-secret-0123456789abcdef0123456789",
            encryption_key="test-encryption-key",
            bootstrap_admin_email=ADMIN_EMAIL,
            bootstrap_admin_password=ADMIN_PASSWORD,
            call_timeout=1.5,
            connect_timeout=3.0,
            refresh_interval=0.4,
            reconcile_interval=0.4,
            max_backoff=0.5,
            llm_api_key="test-llm-key",
            llm_base_url="http://llm.invalid/v1",
        )
        settings = Settings(**{**defaults, **overrides})
        llm = FakeLLM()

        async def allow_any_url(_: str) -> None:
            return None

        app = create_app(settings, url_check=allow_any_url, llm_transport=httpx2.MockTransport(llm))
        server = RunningServer(app)
        server.graceful = True
        gateway_url = server.url
        object.__setattr__(settings, "public_url", gateway_url)
        await server.start()
        try:
            yield Gateway(server, settings, llm)
        finally:
            await server.stop()


def text_of(result: Any) -> str:
    return result.content[0].text if result.content else ""
