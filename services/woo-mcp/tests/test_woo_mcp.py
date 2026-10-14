"""Tests for the WooCommerce MCP server, with WooCommerce replaced by a fake.

The fake checks each request's OAuth signature the way WooCommerce does: it
rebuilds the signed URL from the store's public address, not from the host the
request was sent to.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import httpx2
import pytest
from mcp import Client

from woo_mcp.config import Settings
from woo_mcp.oauth1 import sign_request, signature
from woo_mcp.server import BearerAuth, build_app, build_server, period_range
from woo_mcp.woo_client import WooClient

SETTINGS = Settings(
    store_name="Test Store",
    internal_url="http://wordpress.store.svc.cluster.local",
    public_url="https://shop.example.com",
    consumer_key="ck_test",
    consumer_secret="cs_test",
    auth_token="gateway-token",
    request_timeout=5,
    port=8000,
)

ORDER = {
    "id": 42,
    "number": "42",
    "status": "processing",
    "date_created": "2026-10-11T09:30:00",
    "total": "19.99",
    "currency": "USD",
    "payment_method_title": "Cash on delivery",
    "billing": {"first_name": "Asha", "last_name": "Rao", "email": "asha@example.com", "city": "Kochi"},
    "shipping": {},
    "line_items": [{"name": "Urumi Classic Tee", "quantity": 1, "total": "19.99", "meta_data": ["noise"]}],
    "meta_data": ["lots", "of", "noise"],
}


class FakeWoo:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.status = "processing"

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/wp-json/":
            return httpx2.Response(200, json={"timezone_string": "Asia/Kolkata", "gmt_offset": 5.5})

        params = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
        supplied = params.pop("oauth_signature", "")
        # WooCommerce signs against http://<site host><path> on a non-TLS request.
        expected = signature(request.method, f"http://shop.example.com{path}", params, "cs_test")
        if params.get("oauth_consumer_key") != "ck_test" or supplied != expected:
            return httpx2.Response(
                401, json={"code": "woocommerce_rest_authentication_error", "message": "Invalid signature"}
            )

        if path.endswith("/products") and request.method == "GET":
            return httpx2.Response(
                200,
                json=[{"id": 7, "name": "Mug", "price": "12.50", "status": "publish", "permalink": "https://shop.example.com/product/mug/"}],
                headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"},
            )
        if path.endswith("/products") and request.method == "POST":
            body = json.loads(request.content)
            return httpx2.Response(201, json={"id": 8, **body, "permalink": "https://shop.example.com/product/new/"})
        if path.endswith("/orders"):
            return httpx2.Response(200, json=[ORDER], headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"})
        if path.endswith("/orders/42") and request.method == "GET":
            return httpx2.Response(200, json={**ORDER, "status": self.status})
        if path.endswith("/orders/42") and request.method == "PUT":
            self.status = json.loads(request.content)["status"]
            return httpx2.Response(200, json={**ORDER, "status": self.status})
        if path.endswith("/orders/42/notes"):
            return httpx2.Response(201, json={"id": 1})
        return httpx2.Response(404, json={"code": "woocommerce_rest_shop_order_invalid_id", "message": "Invalid ID."})


@pytest.fixture
def fake() -> FakeWoo:
    return FakeWoo()


@asynccontextmanager
async def connect(fake: FakeWoo):
    """An MCP client connected in-process to a server that talks to `fake`."""
    woo = WooClient(
        SETTINGS.internal_url,
        SETTINGS.public_url,
        SETTINGS.consumer_key,
        SETTINGS.consumer_secret,
        transport=httpx2.MockTransport(fake),
    )
    try:
        async with Client(build_server(SETTINGS, woo)) as client:
            yield client
    finally:
        await woo.aclose()


def test_signature_is_built_from_the_oauth_base_string():
    signed = sign_request(
        "get",
        "http://shop.example.com/wp-json/wc/v3/orders",
        {"per_page": "20", "after": "2026-10-10T23:59:59", "search": "blue shirt"},
        "ck_test",
        "cs_test",
        timestamp=1760000000,
        nonce="abc123",
    )
    # The base string written out by hand: METHOD & encoded URL & encoded
    # parameters. Parameters are sorted by name, and their values end up
    # encoded twice (":" -> %3A -> %253A, " " -> %20 -> %2520).
    base_string = (
        "GET"
        "&http%3A%2F%2Fshop.example.com%2Fwp-json%2Fwc%2Fv3%2Forders"
        "&after%3D2026-10-10T23%253A59%253A59"
        "%26oauth_consumer_key%3Dck_test"
        "%26oauth_nonce%3Dabc123"
        "%26oauth_signature_method%3DHMAC-SHA256"
        "%26oauth_timestamp%3D1760000000"
        "%26per_page%3D20"
        "%26search%3Dblue%2520shirt"
    )
    expected = base64.b64encode(hmac.new(b"cs_test&", base_string.encode(), hashlib.sha256).digest()).decode()
    assert signed["oauth_signature"] == expected


def test_period_ranges_use_store_local_midnight():
    now = datetime(2026, 10, 11, 14, 5, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert period_range("today", now) == ("2026-10-10T23:59:59", None)
    assert period_range("yesterday", now) == ("2026-10-09T23:59:59", "2026-10-11T00:00:00")
    assert period_range("last_7_days", now) == ("2026-10-04T23:59:59", None)
    assert period_range("all", now) == (None, None)


async def test_exposes_the_required_tools(fake: FakeWoo):
    async with connect(fake) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"list_products", "create_product", "list_orders", "get_order", "update_order_status"} <= names


async def test_requests_go_to_the_internal_service_and_are_signed(fake: FakeWoo):
    async with connect(fake) as client:
        result = await client.call_tool("list_products", {})
        assert not result.is_error
        assert result.structured_content["products"][0]["url"].startswith("https://shop.example.com")
        sent = fake.requests[-1]
        assert sent.url.host == "wordpress.store.svc.cluster.local"
        assert "oauth_signature" in sent.url.query.decode()
        # The consumer secret must never be sent.
        assert "cs_test" not in str(sent.url)


async def test_create_product_formats_the_price(fake: FakeWoo):
    async with connect(fake) as client:
        result = await client.call_tool("create_product", {"name": "Blue Hoodie", "regular_price": 49})
        assert not result.is_error
        body = json.loads(fake.requests[-1].content)
        assert body == {"name": "Blue Hoodie", "type": "simple", "regular_price": "49.00", "status": "publish"}
        assert result.structured_content["id"] == 8


async def test_list_orders_today_sends_the_store_local_range(fake: FakeWoo):
    async with connect(fake) as client:
        result = await client.call_tool("list_orders", {"period": "today"})
        assert not result.is_error
        data = result.structured_content
        assert data["timezone"] == "Asia/Kolkata"
        assert data["total"] == 1
        assert data["sum_of_totals_on_this_page"] == {"USD": 19.99}
        # The trimmed view keeps what matters and drops WooCommerce's bulk.
        assert data["orders"][0] == {
            "id": 42,
            "number": "42",
            "status": "processing",
            "created": "2026-10-11T09:30:00",
            "total": "19.99",
            "currency": "USD",
            "customer": "Asha Rao",
            "email": "asha@example.com",
            "payment_method": "Cash on delivery",
            "items": [{"name": "Urumi Classic Tee", "quantity": 1, "total": "19.99"}],
        }
        params = dict(parse_qsl(fake.requests[-1].url.query.decode()))
        assert params["after"].endswith("T23:59:59")
        assert "before" not in params


async def test_update_order_status_reports_the_change(fake: FakeWoo):
    async with connect(fake) as client:
        result = await client.call_tool("update_order_status", {"order_id": 42, "status": "completed", "note": "Shipped"})
        assert not result.is_error
        assert result.structured_content["previous_status"] == "processing"
        assert result.structured_content["status"] == "completed"
        assert fake.requests[-1].url.path.endswith("/orders/42/notes")


async def test_store_errors_come_back_as_readable_tool_errors(fake: FakeWoo):
    async with connect(fake) as client:
        result = await client.call_tool("get_order", {"order_id": 999})
        assert result.is_error
        assert "Invalid ID" in result.content[0].text


async def test_invalid_arguments_are_rejected_before_calling_the_store(fake: FakeWoo):
    async with connect(fake) as client:
        result = await client.call_tool("update_order_status", {"order_id": 42, "status": "teleported"})
        assert result.is_error
        assert not any(r.method == "PUT" for r in fake.requests)


async def test_bearer_token_is_required():
    app = build_app(SETTINGS, WooClient("http://x", "http://x", "k", "s", transport=httpx2.MockTransport(FakeWoo())))
    assert isinstance(app, BearerAuth)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://woo-mcp") as http:
        assert (await http.get("/healthz")).status_code == 200
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert (await http.post("/mcp", json=body)).status_code == 401
        assert (await http.post("/mcp", json=body, headers={"Authorization": "Bearer wrong"})).status_code == 401
