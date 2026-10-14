"""WooCommerce MCP server.

Exposes one store's products and orders as MCP tools over Streamable HTTP.
It runs stateless: every request stands on its own, so any replica can answer
any request and a restart loses nothing.

Run with:  uvicorn woo_mcp.server:build_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Settings
from .woo_client import WooClient, WooError

logger = logging.getLogger("woo_mcp")

OrderStatus = Literal["pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"]
Period = Literal["all", "today", "yesterday", "last_7_days", "last_30_days"]

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


# --- Shaping WooCommerce responses -------------------------------------------
# A raw WooCommerce order is several kilobytes of JSON. Tools return a trimmed
# view so that a model can read a page of results without drowning in fields.


def product_summary(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "sku": p.get("sku") or None,
        "type": p.get("type"),
        "status": p.get("status"),
        "price": p.get("price") or None,
        "regular_price": p.get("regular_price") or None,
        "sale_price": p.get("sale_price") or None,
        "stock_status": p.get("stock_status"),
        "stock_quantity": p.get("stock_quantity"),
        "url": p.get("permalink"),
        "created": p.get("date_created"),
    }


def _person(address: dict[str, Any]) -> str:
    return " ".join(part for part in (address.get("first_name"), address.get("last_name")) if part).strip()


def order_summary(o: dict[str, Any]) -> dict[str, Any]:
    billing = o.get("billing") or {}
    return {
        "id": o.get("id"),
        "number": o.get("number"),
        "status": o.get("status"),
        "created": o.get("date_created"),
        "total": o.get("total"),
        "currency": o.get("currency"),
        "customer": _person(billing) or None,
        "email": billing.get("email") or None,
        "payment_method": o.get("payment_method_title") or None,
        "items": [
            {"name": i.get("name"), "quantity": i.get("quantity"), "total": i.get("total")}
            for i in o.get("line_items") or []
        ],
    }


def _address(a: dict[str, Any]) -> dict[str, Any]:
    keys = ("address_1", "address_2", "city", "state", "postcode", "country", "phone")
    return {"name": _person(a) or None, **{k: a[k] for k in keys if a.get(k)}}


def order_detail(o: dict[str, Any]) -> dict[str, Any]:
    return {
        **order_summary(o),
        "subtotal_discount": o.get("discount_total"),
        "shipping_total": o.get("shipping_total"),
        "tax_total": o.get("total_tax"),
        "paid": o.get("date_paid"),
        "completed": o.get("date_completed"),
        "customer_note": o.get("customer_note") or None,
        "billing": _address(o.get("billing") or {}),
        "shipping": _address(o.get("shipping") or {}),
        "shipping_methods": [s.get("method_title") for s in o.get("shipping_lines") or []],
    }


def period_range(period: Period, now: datetime) -> tuple[str | None, str | None]:
    """Turn a named period into (after, before) timestamps in the store's own time.

    WooCommerce reads timestamps without an offset as store-local time, so the
    values are deliberately naive.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start: datetime | None
    end: datetime | None = None
    if period == "today":
        start = midnight
    elif period == "yesterday":
        start, end = midnight - timedelta(days=1), midnight
    elif period == "last_7_days":
        start = midnight - timedelta(days=6)
    elif period == "last_30_days":
        start = midnight - timedelta(days=29)
    else:
        return None, None
    # "after" is exclusive in WooCommerce; step back a second to include midnight.
    fmt = "%Y-%m-%dT%H:%M:%S"
    return (start - timedelta(seconds=1)).strftime(fmt), end.strftime(fmt) if end else None


def _page_info(headers: Any, page: int) -> dict[str, int]:
    def number(name: str, default: int) -> int:
        try:
            return int(headers.get(name, default))
        except (TypeError, ValueError):
            return default

    return {"page": page, "total_pages": number("x-wp-totalpages", 1), "total": number("x-wp-total", 0)}


# --- The MCP server ----------------------------------------------------------


def build_server(settings: Settings, client: WooClient) -> MCPServer:
    mcp = MCPServer(
        "woocommerce",
        title=f"WooCommerce: {settings.store_name}",
        version=__version__,
        instructions=(
            f"Tools for the WooCommerce store '{settings.store_name}' at {settings.public_url}. "
            "Dates are in the store's own timezone. Prices are strings in the store currency."
        ),
    )

    async def api(method: str, path: str, **kwargs: Any) -> tuple[Any, Any]:
        try:
            return await client.request(method, path, **kwargs)
        except WooError as exc:
            # ToolError reaches the caller as a readable, is_error result.
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations=READ_ONLY)
    async def list_products(
        search: Annotated[str, Field(description="Only products whose name or description matches this text.")] = "",
        status: Annotated[
            Literal["any", "publish", "draft", "pending", "private"],
            Field(description="Product status. 'publish' means visible on the storefront."),
        ] = "any",
        per_page: Annotated[int, Field(ge=1, le=50, description="Products per page.")] = 20,
        page: Annotated[int, Field(ge=1, description="Page number, starting at 1.")] = 1,
    ) -> dict[str, Any]:
        """List products in the store, newest first."""
        data, headers = await api(
            "GET",
            "products",
            params={"search": search, "status": status, "per_page": per_page, "page": page, "orderby": "date", "order": "desc"},
        )
        return {**_page_info(headers, page), "products": [product_summary(p) for p in data]}

    @mcp.tool(annotations=READ_ONLY)
    async def get_product(
        product_id: Annotated[int, Field(ge=1, description="The product's numeric id.")],
    ) -> dict[str, Any]:
        """Get one product by id, including its description."""
        data, _ = await api("GET", f"products/{product_id}")
        return {
            **product_summary(data),
            "short_description": data.get("short_description") or None,
            "description": data.get("description") or None,
            "categories": [c.get("name") for c in data.get("categories") or []],
        }

    @mcp.tool(annotations=WRITES)
    async def create_product(
        name: Annotated[str, Field(min_length=1, max_length=200, description="Product name shown to shoppers.")],
        regular_price: Annotated[float, Field(ge=0, description="Price in the store currency, for example 24.99.")],
        description: Annotated[str, Field(description="Long description. Optional.")] = "",
        short_description: Annotated[str, Field(description="One-line summary. Optional.")] = "",
        sku: Annotated[str, Field(description="Stock keeping unit. Optional, must be unique if given.")] = "",
        status: Annotated[
            Literal["publish", "draft"],
            Field(description="'publish' puts it on the storefront straight away; 'draft' keeps it hidden."),
        ] = "publish",
        stock_quantity: Annotated[
            int | None, Field(ge=0, description="Units in stock. Leave empty to not track stock.")
        ] = None,
    ) -> dict[str, Any]:
        """Create a simple product. Returns the new product, including its storefront URL."""
        body: dict[str, Any] = {
            "name": name,
            "type": "simple",
            "regular_price": f"{regular_price:.2f}",
            "status": status,
        }
        if description:
            body["description"] = description
        if short_description:
            body["short_description"] = short_description
        if sku:
            body["sku"] = sku
        if stock_quantity is not None:
            body.update({"manage_stock": True, "stock_quantity": stock_quantity})
        data, _ = await api("POST", "products", json=body)
        logger.info("created product id=%s", data.get("id"))
        return product_summary(data)

    @mcp.tool(annotations=READ_ONLY)
    async def list_orders(
        period: Annotated[
            Period,
            Field(description="Which orders by creation date, in the store's timezone. Use 'today' for today's orders."),
        ] = "all",
        status: Annotated[
            Literal["any", "pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"],
            Field(description="Only orders in this status."),
        ] = "any",
        created_after: Annotated[
            str, Field(description="ISO 8601 store-local time, e.g. 2026-01-31T00:00:00. Overrides 'period'.")
        ] = "",
        created_before: Annotated[
            str, Field(description="ISO 8601 store-local time. Overrides 'period'.")
        ] = "",
        per_page: Annotated[int, Field(ge=1, le=50, description="Orders per page.")] = 20,
        page: Annotated[int, Field(ge=1, description="Page number, starting at 1.")] = 1,
    ) -> dict[str, Any]:
        """List orders, newest first, optionally limited to a period such as today."""
        try:
            now = await client.store_now()
        except WooError as exc:
            raise ToolError(str(exc)) from exc
        after, before = period_range(period, now)
        after, before = created_after or after, created_before or before
        data, headers = await api(
            "GET",
            "orders",
            params={
                "status": status,
                "after": after,
                "before": before,
                "per_page": per_page,
                "page": page,
                "orderby": "date",
                "order": "desc",
            },
        )
        orders = [order_summary(o) for o in data]
        page_totals: dict[str, float] = {}
        for o in orders:
            try:
                page_totals[o["currency"]] = round(page_totals.get(o["currency"], 0.0) + float(o["total"]), 2)
            except (TypeError, ValueError):
                continue
        return {
            "store": settings.store_name,
            "store_time": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": str(now.tzinfo),
            "period": period if not (created_after or created_before) else "custom",
            **_page_info(headers, page),
            "sum_of_totals_on_this_page": page_totals,
            "orders": orders,
        }

    @mcp.tool(annotations=READ_ONLY)
    async def get_order(
        order_id: Annotated[int, Field(ge=1, description="The order's numeric id.")],
    ) -> dict[str, Any]:
        """Get one order by id: items, totals, customer, billing and shipping details."""
        data, _ = await api("GET", f"orders/{order_id}")
        return order_detail(data)

    @mcp.tool(annotations=WRITES)
    async def update_order_status(
        order_id: Annotated[int, Field(ge=1, description="The order's numeric id.")],
        status: Annotated[OrderStatus, Field(description="The status to move the order to.")],
        note: Annotated[str, Field(description="Optional private note recorded on the order.")] = "",
    ) -> dict[str, Any]:
        """Change an order's status, for example from processing to completed."""
        before, _ = await api("GET", f"orders/{order_id}")
        data, _ = await api("PUT", f"orders/{order_id}", json={"status": status})
        if note:
            await api("POST", f"orders/{order_id}/notes", json={"note": note})
        logger.info("order id=%s status %s -> %s", order_id, before.get("status"), data.get("status"))
        return {"previous_status": before.get("status"), **order_summary(data)}

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    # Ready means "able to answer MCP requests". It does not depend on the
    # store being up: when the store is down, tools answer with a clear error
    # instead of this server dropping out of its Service.
    @mcp.custom_route("/readyz", methods=["GET"], include_in_schema=False)
    async def readyz(_: Request) -> Response:
        return JSONResponse({"status": "ready"})

    return mcp


class BearerAuth:
    """Rejects any request that does not carry the shared bearer token.

    The NetworkPolicy already limits callers to the gateway's pods. The token is
    a second, independent check, and it is the credential the gateway keeps on
    the server side and never shows to users.
    """

    def __init__(self, app: ASGIApp, token: str, public_paths: frozenset[str]) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode()
        self._public_paths = public_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self._public_paths:
            await self._app(scope, receive, send)
            return
        supplied = dict(scope["headers"]).get(b"authorization", b"")
        if not hmac.compare_digest(supplied, self._expected):
            response = JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def build_app(settings: Settings | None = None, client: WooClient | None = None) -> ASGIApp:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = settings or Settings.from_env()
    client = client or WooClient(
        settings.internal_url,
        settings.public_url,
        settings.consumer_key,
        settings.consumer_secret,
        timeout=settings.request_timeout,
    )
    mcp = build_server(settings, client)
    # JSON responses and no sessions: one POST in, one JSON body out.
    app = mcp.streamable_http_app(json_response=True, stateless_http=True, host="0.0.0.0")
    return BearerAuth(app, settings.auth_token, frozenset({"/healthz", "/readyz"}))
