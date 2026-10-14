"""A small async client for the WooCommerce REST API (wc/v3)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx2

from .oauth1 import sign_request

API_PREFIX = "/wp-json/wc/v3"
_TIMEZONE_TTL_SECONDS = 300


class WooError(Exception):
    """A failure worth showing to the caller in plain words."""


def _as_query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class WooClient:
    def __init__(
        self,
        internal_url: str,
        public_url: str,
        consumer_key: str,
        consumer_secret: str,
        timeout: float = 20.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._internal_url = internal_url.rstrip("/")
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        # WooCommerce verifies the signature against the URL it builds from the
        # site's own address, not from the Host header we connect to. Over a
        # non-TLS connection it always uses the http scheme for that URL.
        self._signing_origin = f"http://{urlsplit(public_url).netloc}"
        self._http = httpx2.AsyncClient(timeout=httpx2.Timeout(timeout, connect=5.0), transport=transport)
        self._tz: tzinfo | None = None
        self._tz_fetched_at = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> tuple[Any, httpx2.Headers]:
        """Call one wc/v3 endpoint and return (decoded JSON, response headers)."""
        api_path = f"{API_PREFIX}/{path.lstrip('/')}"
        query = {k: _as_query_value(v) for k, v in (params or {}).items() if v is not None and v != ""}
        signed = sign_request(
            method, self._signing_origin + api_path, query, self._consumer_key, self._consumer_secret
        )
        try:
            response = await self._http.request(method, self._internal_url + api_path, params=signed, json=json)
        except httpx2.TimeoutException as exc:
            raise WooError("The store did not answer in time.") from exc
        except httpx2.HTTPError as exc:
            raise WooError(f"The store could not be reached ({type(exc).__name__}).") from exc

        if response.status_code >= 400:
            raise WooError(_describe_failure(response))
        try:
            return response.json(), response.headers
        except ValueError as exc:
            raise WooError("The store returned a response that was not JSON.") from exc

    async def store_timezone(self) -> tzinfo:
        """The timezone the store reports dates in. Cached for a few minutes."""
        if self._tz is not None and time.monotonic() - self._tz_fetched_at < _TIMEZONE_TTL_SECONDS:
            return self._tz
        try:
            response = await self._http.get(f"{self._internal_url}/wp-json/")
            response.raise_for_status()
            info = response.json()
        except (httpx2.HTTPError, ValueError) as exc:
            if self._tz is not None:
                return self._tz
            raise WooError(f"The store could not be reached ({type(exc).__name__}).") from exc

        resolved: tzinfo = timezone.utc
        name = info.get("timezone_string") or ""
        if name:
            try:
                resolved = ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                resolved = timezone.utc
        elif info.get("gmt_offset") not in (None, ""):
            resolved = timezone(timedelta(hours=float(info["gmt_offset"])))
        self._tz, self._tz_fetched_at = resolved, time.monotonic()
        return resolved

    async def store_now(self) -> datetime:
        return datetime.now(await self.store_timezone())


def _describe_failure(response: httpx2.Response) -> str:
    """Turn a WooCommerce error response into one readable sentence."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict) and body.get("message"):
        return f"WooCommerce rejected the request ({response.status_code} {body.get('code', '')}): {body['message']}".replace(
            " )", ")"
        )
    if response.status_code in (502, 503, 504):
        return f"The store is not available right now (HTTP {response.status_code})."
    return f"WooCommerce returned HTTP {response.status_code}."
