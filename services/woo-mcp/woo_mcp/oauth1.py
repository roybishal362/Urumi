"""One-legged OAuth 1.0a request signing, as the WooCommerce REST API expects it.

WooCommerce accepts the consumer key and secret as HTTP Basic credentials only
when the request arrives over TLS. Inside the cluster this server talks to
WordPress over plain HTTP, so it uses WooCommerce's other documented scheme:
each request carries an HMAC-SHA256 signature computed with the consumer
secret. The secret itself never travels over the network.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote


def _encode(value: str) -> str:
    """Percent-encode per RFC 3986, which is what PHP's rawurlencode does."""
    return quote(value, safe="-._~")


def signature(method: str, url: str, params: dict[str, str], consumer_secret: str) -> str:
    """Return the base64 HMAC-SHA256 signature for one request.

    `url` is the request URL without its query string. `params` is every query
    parameter that will be sent, OAuth ones included, except the signature.
    """
    normalized = "&".join(f"{_encode(k)}={_encode(v)}" for k, v in sorted(params.items()))
    base_string = "&".join([method.upper(), _encode(url), _encode(normalized)])
    # One-legged OAuth has no token secret, so the key ends with a bare "&".
    key = f"{consumer_secret}&".encode()
    digest = hmac.new(key, base_string.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def sign_request(
    method: str,
    url: str,
    params: dict[str, str],
    consumer_key: str,
    consumer_secret: str,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Return `params` plus the OAuth parameters, ready to send as the query string."""
    signed = dict(params)
    signed.update(
        {
            "oauth_consumer_key": consumer_key,
            "oauth_signature_method": "HMAC-SHA256",
            "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
            "oauth_nonce": nonce or secrets.token_hex(16),
        }
    )
    signed["oauth_signature"] = signature(method, url, signed, consumer_secret)
    return signed
