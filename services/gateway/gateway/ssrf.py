"""Checks on upstream URLs, so the gateway cannot be pointed at things it should not reach.

An admin adds a server by URL, and the gateway then makes requests to it from
inside the cluster. Without checks, a URL like http://169.254.169.254/ (the
cloud metadata service) or http://some-db.other-namespace.svc/ would turn the
gateway into a way of reaching internal systems.

Rules:
  - http or https only, and no user:password@ in the URL
  - loopback, link-local (which includes cloud metadata) and other special
    ranges are always refused
  - private addresses are refused unless the hostname ends with a suffix the
    operator listed in INTERNAL_HOST_SUFFIXES (in-cluster upstreams)
  - public upstreams must use https, so the credential is not sent in the clear

The check runs when a URL is saved and again each time the gateway connects.
A hostname that changes its DNS answer between the check and the connection is
not caught here; the gateway's egress NetworkPolicy blocks the metadata range
as a second layer.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from .config import Settings


class UnsafeUrlError(ValueError):
    """The URL is not allowed as an upstream. The message is safe to show an admin."""


def _is_internal_name(host: str, suffixes: tuple[str, ...]) -> bool:
    for suffix in suffixes:
        bare = suffix.lstrip(".")
        if host == bare or host.endswith("." + bare):
            return True
    return False


async def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        # A literal IP address needs no lookup.
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]


async def check_upstream_url(url: str, settings: Settings) -> None:
    """Raise UnsafeUrlError if `url` must not be used as an upstream."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrlError("The URL is not valid.") from exc
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrlError("The URL must start with http:// or https://.")
    if not parts.hostname:
        raise UnsafeUrlError("The URL has no hostname.")
    if parts.username or parts.password:
        raise UnsafeUrlError("Put credentials in the credential field, not in the URL.")

    host = parts.hostname.lower().rstrip(".")
    internal_name = _is_internal_name(host, settings.internal_host_suffixes)

    try:
        addresses = await _resolve(host, port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        if internal_name:
            # An allowed in-cluster name that does not resolve yet (its Service
            # is not created) is a connection problem, not a safety problem.
            return
        raise UnsafeUrlError(f"The hostname '{host}' does not resolve.") from exc

    for address in addresses:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
            raise UnsafeUrlError(
                f"'{host}' resolves to {address}, a loopback or link-local address. "
                "That range includes cloud metadata services and is never allowed."
            )
        if not address.is_global and not internal_name:
            allowed = ", ".join(settings.internal_host_suffixes) or "none configured"
            raise UnsafeUrlError(
                f"'{host}' resolves to the private address {address}. Internal upstreams are only "
                f"allowed for hostnames ending in: {allowed}."
            )

    if parts.scheme == "http" and not internal_name and not settings.allow_plain_http_public:
        raise UnsafeUrlError("Public upstreams must use https so the credential is not sent in clear text.")
