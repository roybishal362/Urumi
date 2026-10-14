"""Two small MCP servers that give the gateway more than one upstream to manage.

One image, two personalities, chosen with SERVER_KIND:

  fx        currency conversion with a fixed rate table. No credential.
  shipping  carrier quotes and tracking. Requires an X-API-Key header.

Both deliberately expose a tool called `get_rates`, so the gateway's handling
of same-named tools from different servers can be seen working. Their data is
made up and fixed, so demos do not depend on any outside service.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Units of each currency per 1 US dollar. Fixed demo values, not live rates.
USD_RATES = {
    "USD": 1.0,
    "EUR": 0.92,
    "GBP": 0.79,
    "INR": 83.50,
    "JPY": 150.0,
    "AUD": 1.52,
    "CAD": 1.37,
    "SGD": 1.34,
    "AED": 3.6725,
}

CARRIERS = [
    # name, service, base price (USD), price per kg, days domestic, days international
    ("BlueDart", "Express", 6.0, 2.5, 2, 6),
    ("Delhivery", "Standard", 3.5, 1.2, 4, 10),
    ("DHL", "Worldwide Priority", 14.0, 4.0, 1, 3),
]
HOME_COUNTRY = "IN"


def _currency(code: str) -> str:
    code = code.strip().upper()
    if code not in USD_RATES:
        raise ToolError(f"Unknown currency '{code}'. Supported: {', '.join(sorted(USD_RATES))}.")
    return code


def build_fx() -> MCPServer:
    mcp = MCPServer(
        "fx",
        title="Currency conversion",
        version="0.1.0",
        instructions="Currency conversion using a fixed demo rate table. Not live market data.",
    )

    @mcp.tool(annotations=READ_ONLY)
    async def get_rates(
        base: Annotated[str, Field(description="ISO 4217 currency code to quote against, e.g. USD.")] = "USD",
    ) -> dict[str, Any]:
        """Exchange rates: how many units of each currency one unit of the base currency buys."""
        base = _currency(base)
        return {
            "base": base,
            "rates": {code: round(rate / USD_RATES[base], 6) for code, rate in USD_RATES.items() if code != base},
            "note": "Fixed demo rates.",
        }

    @mcp.tool(annotations=READ_ONLY)
    async def convert(
        amount: Annotated[float, Field(description="Amount in the source currency.")],
        from_currency: Annotated[str, Field(description="ISO 4217 code of the source currency, e.g. USD.")],
        to_currency: Annotated[str, Field(description="ISO 4217 code of the target currency, e.g. INR.")],
    ) -> dict[str, Any]:
        """Convert an amount of money from one currency to another."""
        source, target = _currency(from_currency), _currency(to_currency)
        rate = USD_RATES[target] / USD_RATES[source]
        return {
            "amount": amount,
            "from": source,
            "to": target,
            "rate": round(rate, 6),
            "converted": round(amount * rate, 2),
        }

    @mcp.tool(annotations=READ_ONLY)
    async def debug_sleep(
        seconds: Annotated[float, Field(ge=0, le=300, description="How long to wait before answering.")],
    ) -> str:
        """Waits, then answers. Exists only to demonstrate the gateway's call timeout; do not use otherwise."""
        await asyncio.sleep(seconds)
        return f"Slept for {seconds} seconds."

    return mcp


def build_shipping() -> MCPServer:
    mcp = MCPServer(
        "shipping",
        title="Shipping carriers",
        version="0.1.0",
        instructions="Shipping quotes and tracking from a simulated carrier network.",
    )

    @mcp.tool(annotations=READ_ONLY)
    async def get_rates(
        destination_country: Annotated[str, Field(description="Two-letter ISO country code, e.g. IN or US.")],
        weight_kg: Annotated[float, Field(gt=0, le=70, description="Parcel weight in kilograms.")],
    ) -> dict[str, Any]:
        """Shipping quotes for a parcel: carrier, service, price in USD and delivery time in days."""
        country = destination_country.strip().upper()
        if len(country) != 2 or not country.isalpha():
            raise ToolError("destination_country must be a two-letter country code such as IN or US.")
        international = country != HOME_COUNTRY
        quotes = [
            {
                "carrier": name,
                "service": service,
                "price_usd": round((base + per_kg * weight_kg) * (2.4 if international else 1.0), 2),
                "delivery_days": days_intl if international else days_home,
            }
            for name, service, base, per_kg, days_home, days_intl in CARRIERS
        ]
        return {
            "origin_country": HOME_COUNTRY,
            "destination_country": country,
            "weight_kg": weight_kg,
            "quotes": sorted(quotes, key=lambda q: q["price_usd"]),
        }

    @mcp.tool(annotations=READ_ONLY)
    async def track_shipment(
        tracking_number: Annotated[str, Field(min_length=4, max_length=40, description="The carrier's tracking number.")],
    ) -> dict[str, Any]:
        """Current status of a shipment."""
        # Derived from the tracking number so the same number always tracks the same way.
        seed = hashlib.sha256(tracking_number.strip().upper().encode()).digest()
        stages = ["Label created", "Picked up", "In transit", "Out for delivery", "Delivered"]
        hubs = ["Bengaluru", "Mumbai", "Delhi", "Chennai", "Hyderabad", "Kochi"]
        stage = seed[0] % len(stages)
        return {
            "tracking_number": tracking_number.strip().upper(),
            "carrier": CARRIERS[seed[1] % len(CARRIERS)][0],
            "status": stages[stage],
            "last_location": hubs[seed[2] % len(hubs)],
            "estimated_days_remaining": max(0, len(stages) - 1 - stage),
        }

    return mcp


class HeaderAuth:
    """Requires a fixed value in a named request header."""

    def __init__(self, app: ASGIApp, header: str, value: str, public_paths: frozenset[str]) -> None:
        self._app = app
        self._header = header.lower().encode()
        self._expected = value.encode()
        self._public_paths = public_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self._public_paths:
            await self._app(scope, receive, send)
            return
        supplied = dict(scope["headers"]).get(self._header, b"")
        if not hmac.compare_digest(supplied, self._expected):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self._app(scope, receive, send)


def build_app() -> ASGIApp:
    kind = os.environ.get("SERVER_KIND", "fx").strip().lower()
    if kind not in ("fx", "shipping"):
        raise RuntimeError("SERVER_KIND must be 'fx' or 'shipping'")
    mcp = build_fx() if kind == "fx" else build_shipping()

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok", "server": kind})

    app: ASGIApp = mcp.streamable_http_app(json_response=True, stateless_http=True, host="0.0.0.0")
    api_key = os.environ.get("API_KEY", "")
    if api_key:
        app = HeaderAuth(app, os.environ.get("API_KEY_HEADER", "X-API-Key"), api_key, frozenset({"/healthz"}))
    return app
