"""Configuration, read once from the environment. Secrets arrive as env vars
populated from Kubernetes Secrets; nothing here has a secret default."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


@dataclass(frozen=True)
class Settings:
    store_name: str
    # Where requests are sent: the WordPress Service inside the cluster.
    internal_url: str
    # The store's own public address, e.g. http://store.localtest.me.
    public_url: str
    consumer_key: str
    consumer_secret: str
    # Bearer token callers must present. Only the gateway knows it.
    auth_token: str
    request_timeout: float
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            store_name=os.environ.get("STORE_NAME", "WooCommerce store"),
            internal_url=_required("WC_INTERNAL_URL").rstrip("/"),
            public_url=_required("STORE_PUBLIC_URL").rstrip("/"),
            consumer_key=_required("WC_CONSUMER_KEY"),
            consumer_secret=_required("WC_CONSUMER_SECRET"),
            auth_token=_required("MCP_AUTH_TOKEN"),
            request_timeout=float(os.environ.get("WC_REQUEST_TIMEOUT", "20")),
            port=int(os.environ.get("PORT", "8000")),
        )
