"""Walk the assignment's definition-of-done flow against a running cluster.

It behaves like the people in the demo: an admin and two members (A and B)
using the dashboard's API, a shopper using the storefront, and an external MCP
client using only the gateway URL and an API key. It reads one thing from the
cluster that a person would be given: the admin's generated password.

    python scripts/smoke_test.py
    python scripts/smoke_test.py --gateway https://gateway.example.com --store https://store.example.com

The chat-agent steps need an LLM API key configured on the gateway; without
one they are skipped and the same tools are exercised over MCP instead.

Requires: kubectl pointed at the cluster, and `pip install mcp httpx2`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import time
import warnings
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

warnings.simplefilter("ignore")

PASSWORD = "smoke-test-password-1"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print(f"\n== {title}")


def kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", *args], check=True, capture_output=True, text=True).stdout.strip()


def text_of(result: Any) -> str:
    return result.content[0].text if result.content else ""


class Dashboard:
    """The gateway's REST API, as one signed-in person."""

    def __init__(self, base: str) -> None:
        self.http = httpx2.AsyncClient(base_url=base, timeout=90)

    async def login(self, email: str, password: str) -> None:
        response = await self.http.post("/api/auth/login", json={"email": email, "password": password})
        response.raise_for_status()

    async def get(self, path: str, **params: Any) -> Any:
        response = await self.http.get(path, params=params)
        response.raise_for_status()
        return response.json()

    async def send(self, method: str, path: str, body: Any = None) -> httpx2.Response:
        return await self.http.request(method, path, json=body)

    async def servers(self) -> dict[str, dict[str, Any]]:
        return {s["slug"]: s for s in (await self.get("/api/servers"))["servers"]}

    async def wait_status(self, slug: str, status: str, timeout: float = 90) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        server = None
        while time.monotonic() < deadline:
            server = (await self.servers()).get(slug)
            if server and server["status"] == status:
                return server
            await asyncio.sleep(1)
        return None

    async def chat(self, text: str) -> list[dict[str, Any]]:
        response = await self.http.post("/api/chat", json={"messages": [{"role": "user", "content": text}]})
        response.raise_for_status()
        return [json.loads(line) for line in response.text.splitlines() if line.strip()]


async def member(admin: Dashboard, base: str, email: str) -> Dashboard:
    """Invite a member (or re-invite one from an earlier run) and sign in as them."""
    invited = await admin.send("POST", "/api/members/invite", {"email": email, "role": "member"})
    if invited.status_code == 409:
        existing = next(m for m in (await admin.get("/api/members"))["members"] if m["email"] == email)
        await admin.send("PATCH", f"/api/members/{existing['id']}", {"is_active": True, "role": "member"})
        invited = await admin.send("POST", f"/api/members/{existing['id']}/invite")
    invited.raise_for_status()
    token = invited.json()["invite_url"].split("token=")[1]
    person = Dashboard(base)
    accepted = await person.send("POST", "/api/auth/accept-invite", {"token": token, "name": email.split("@")[0], "password": PASSWORD})
    accepted.raise_for_status()
    return person


def mcp_client(gateway: str, api_key: str) -> tuple[httpx2.AsyncClient, Client]:
    """An external MCP client: the gateway URL and one API key, nothing else."""
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {api_key}"}, timeout=90)
    return http, Client(streamable_http_client(f"{gateway}/mcp", http_client=http), read_timeout_seconds=90, cache=None)


async def place_order(store: str, product_id: int) -> dict[str, Any]:
    """Check out as a guest with cash on delivery, through the storefront's own cart and checkout API."""
    async with httpx2.AsyncClient(base_url=store, timeout=90) as shop:
        cart = await shop.get("/wp-json/wc/store/v1/cart")
        cart.raise_for_status()
        session = {"Cart-Token": cart.headers["Cart-Token"], "Nonce": cart.headers.get("Nonce", "")}
        added = await shop.post("/wp-json/wc/store/v1/cart/add-item", json={"id": product_id, "quantity": 1}, headers=session)
        added.raise_for_status()
        address = {
            "first_name": "Smoke",
            "last_name": "Test",
            "address_1": "1 Test Street",
            "city": "San Francisco",
            "state": "CA",
            "postcode": "94103",
            "country": "US",
            "email": "shopper@example.com",
            "phone": "5550100",
        }
        order = await shop.post(
            "/wp-json/wc/store/v1/checkout",
            json={"billing_address": address, "shipping_address": address, "payment_method": "cod"},
            headers=session,
        )
        if order.status_code >= 400:
            raise RuntimeError(f"checkout failed: {order.status_code} {order.text[:300]}")
        return order.json()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gateway", default="http://gateway.localtest.me")
    parser.add_argument("--store", default="http://store.localtest.me")
    parser.add_argument("--gateway-namespace", default="gateway")
    parser.add_argument("--admin-email", default="admin@example.com")
    args = parser.parse_args()
    gateway, store, ns = args.gateway.rstrip("/"), args.store.rstrip("/"), args.gateway_namespace
    stamp = time.strftime("%H%M%S")

    section("Admin: servers and their tools")
    admin_password = base64.b64decode(kubectl("-n", ns, "get", "secret", "gateway-app", "-o", "jsonpath={.data.admin-password}")).decode()
    admin = Dashboard(gateway)
    await admin.login(args.admin_email, admin_password)
    # Leave things as a fresh install would have them, in case an earlier run was interrupted.
    for server in (await admin.servers()).values():
        if not server["enabled"]:
            await admin.send("PATCH", f"/api/servers/{server['id']}", {"enabled": True})
    kubectl("-n", ns, "scale", "deploy/gateway-fx", "--replicas=1")
    for slug in ("woo", "fx", "shipping"):
        await admin.wait_status(slug, "connected")

    servers = await admin.servers()
    check("WooCommerce is registered as the first server", "woo" in servers)
    check("three upstream servers are connected", sum(s["status"] == "connected" for s in servers.values()) >= 3,
          ", ".join(f"{slug}={s['status']}" for slug, s in servers.items()))
    woo_tools = {t["tool"] for t in servers.get("woo", {}).get("tools", [])}
    check("WooCommerce exposes the five required tools",
          {"list_products", "create_product", "list_orders", "get_order", "update_order_status"} <= woo_tools)
    listing = json.dumps(servers)
    check("the API never returns an upstream credential", all(s.get("has_credential") is not None for s in servers.values())
          and "credential_enc" not in listing and '"credential"' not in listing)

    section("Team: two members with their own keys")
    alice = await member(admin, gateway, "alice@example.com")
    bob = await member(admin, gateway, "bob@example.com")
    alice_key = (await alice.send("POST", "/api/keys", {"name": "alice laptop"})).json()
    bob_key = (await bob.send("POST", "/api/keys", {"name": "bob laptop"})).json()
    check("members generate their own API keys", alice_key["secret"].startswith("urm_") and bob_key["secret"].startswith("urm_"))
    check("a member cannot manage servers", (await bob.send("POST", "/api/servers", {"name": "x", "slug": "xx", "url": "https://example.com/mcp"})).status_code == 403)
    check("a member does not see where upstreams live", "url" not in (await bob.servers())["woo"])

    info = (await alice.get("/api/auth/me"))["gateway"]
    product_name = f"Smoke Test Lamp {stamp}"

    section("Chat agent, as user A")
    product: dict[str, Any] | None = None
    if info["chat_ready"]:
        events = await alice.chat(f'Create a product called "{product_name}" priced at 31.50.')
        calls = [e for e in events if e["type"] == "tool_end"]
        check("the agent created the product by calling a tool", any(c["name"] == "woo__create_product" and c["ok"] for c in calls),
              "; ".join(f"{c['name']}:{c['outcome']}" for c in calls) or str(events[-1]))
    else:
        print("  [SKIP] no LLM API key on the gateway; creating the product over MCP as user A instead")
        http, client = mcp_client(gateway, alice_key["secret"])
        async with http, client:
            created = await client.call_tool("woo__create_product", {"name": product_name, "regular_price": 31.5})
            check("user A created a product through the gateway", not created.is_error, text_of(created)[:120])

    http, client = mcp_client(gateway, alice_key["secret"])
    async with http, client:
        found = await client.call_tool("woo__list_products", {"search": product_name})
        products = (found.structured_content or {}).get("products", [])
        product = products[0] if products else None
    check("the product exists in the store", product is not None)
    if product:
        async with httpx2.AsyncClient(timeout=60, follow_redirects=True) as web:
            page = await web.get(product["url"])
        check("the product appears on the storefront", page.status_code == 200 and product_name in page.text, product["url"])

    section("Store: checkout with cash on delivery")
    order: dict[str, Any] = {}
    if product:
        try:
            order = await place_order(store, product["id"])
            check("a guest order was placed with COD", bool(order.get("order_id")), f"order #{order.get('order_id')} {order.get('status')}")
        except Exception as exc:  # noqa: BLE001
            check("a guest order was placed with COD", False, str(exc)[:200])
    order_id = order.get("order_id")

    if info["chat_ready"] and order_id:
        events = await alice.chat("What orders came in today?")
        answer = next((e.get("content", "") for e in events if e["type"] == "answer"), "")
        calls = [e for e in events if e["type"] == "tool_end"]
        check("the agent finds today's order", any(c["name"] == "woo__list_orders" and c["ok"] for c in calls) and str(order_id) in answer + json.dumps(calls),
              answer[:140].replace("\n", " "))

    section("External MCP client, as user B")
    http, client = mcp_client(gateway, bob_key["secret"])
    async with http, client:
        names = {t.name for t in (await client.list_tools()).tools}
        prefixes = {n.split("__")[0] for n in names}
        check("one endpoint lists the tools of every enabled server", {"woo", "fx", "shipping"} <= prefixes, f"{len(names)} tools via MCP {client.protocol_version}")
        check("same-named tools from two servers both appear", {"fx__get_rates", "shipping__get_rates"} <= names)

        today = await client.call_tool("woo__list_orders", {"period": "today"})
        orders = (today.structured_content or {}).get("orders", [])
        check("today's orders include the new order", any(o["id"] == order_id for o in orders), f"{len(orders)} order(s) today")

        # One question that needs two different servers: the order total from
        # the store, converted by the currency server.
        total = next((float(o["total"]) for o in orders if o["id"] == order_id), 31.5)
        converted = await client.call_tool("fx__convert", {"amount": total, "from_currency": "USD", "to_currency": "INR"})
        quote = await client.call_tool("shipping__get_rates", {"destination_country": "US", "weight_kg": 1})
        check("tools from two other servers answer in the same session", not converted.is_error and not quote.is_error,
              f"{total} USD = {(converted.structured_content or {}).get('converted')} INR")

    section("Control: revoke a key")
    revoked = await admin.send("DELETE", f"/api/keys/{bob_key['key']['id']}")
    http, client = mcp_client(gateway, bob_key["secret"])
    rejected = False
    try:
        async with http, client:
            await client.call_tool("woo__list_orders", {"period": "today"})
    except BaseException as exc:  # noqa: BLE001 - the SDK raises an exception group on HTTP 401
        rejected = not isinstance(exc, (KeyboardInterrupt, SystemExit))
    async with httpx2.AsyncClient(timeout=30) as raw:
        direct = await raw.post(f"{gateway}/mcp", headers={"Authorization": f"Bearer {bob_key['secret']}", "Accept": "application/json, text/event-stream"},
                                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "woo__list_orders", "arguments": {}}})
    check("user B's next call is rejected", revoked.status_code == 200 and rejected and direct.status_code == 401, f"HTTP {direct.status_code}")
    bob_key = (await bob.send("POST", "/api/keys", {"name": "bob laptop 2"})).json()

    section("Control: disable a server")
    shipping = (await admin.servers())["shipping"]
    await admin.send("PATCH", f"/api/servers/{shipping['id']}", {"enabled": False})
    http, client = mcp_client(gateway, bob_key["secret"])
    async with http, client:
        names = {t.name for t in (await client.list_tools()).tools}
        gone_for_client = not any(n.startswith("shipping__") for n in names)
        try:
            await client.call_tool("shipping__get_rates", {"destination_country": "US", "weight_kg": 1})
            refused = False
        except MCPError:
            refused = True
    chat_tools = {t["name"] for t in (await bob.get("/api/tools"))["tools"]}
    check("its tools disappear from the external client", gone_for_client and refused)
    check("its tools disappear from the chat agent", not any(n.startswith("shipping__") for n in chat_tools))
    await admin.send("PATCH", f"/api/servers/{shipping['id']}", {"enabled": True})
    check("enabling it brings them back", await admin.wait_status("shipping", "connected") is not None)

    section("Control: kill an upstream server")
    kubectl("-n", ns, "scale", "deploy/gateway-fx", "--replicas=0")
    dead = await admin.wait_status("fx", "unreachable")
    check("the dashboard shows the dead server as unreachable", dead is not None, (dead or {}).get("error") or "")
    # With several gateway replicas, each notices on its own health check, up to one interval later.
    await asyncio.sleep(6)
    http, client = mcp_client(gateway, bob_key["secret"])
    async with http, client:
        started = time.monotonic()
        names = {t.name for t in (await client.list_tools()).tools}
        still = await client.call_tool("woo__list_products", {"per_page": 1})
        check("the other servers keep working", not still.is_error and "woo__list_orders" in names and time.monotonic() - started < 10,
              f"listing and a call took {time.monotonic() - started:.1f}s")
        check("the dead server's tools are no longer offered", not any(n.startswith("fx__") for n in names))
    kubectl("-n", ns, "scale", "deploy/gateway-fx", "--replicas=1")
    check("it reconnects by itself when it returns", await admin.wait_status("fx", "connected") is not None)

    section("Audit log")
    entries = (await admin.get("/api/audit", limit=300))["entries"]
    by_user = {e["user_email"] for e in entries}
    check("calls are attributed to the right users", {"alice@example.com", "bob@example.com"} <= by_user)
    check("each entry has user, server, tool, time, duration and outcome",
          all(e["user_email"] and e["tool"] and e["ts"] and e["duration_ms"] >= 0 and e["outcome"] for e in entries))
    check("the rejected call is recorded against user B", any(e["outcome"] == "rejected" and e["user_email"] == "bob@example.com" for e in entries))
    if info["chat_ready"]:
        check("chat calls are recorded under the signed-in user", any(e["source"] == "chat" and e["user_email"] == "alice@example.com" for e in entries))
    own = (await alice.get("/api/audit", limit=300))["entries"]
    check("a member sees only their own calls", bool(own) and {e["user_email"] for e in own} == {"alice@example.com"})

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
